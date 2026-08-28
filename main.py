import os
import re
import json
import math
import glob
import random
import asyncio
import requests
import subprocess
import concurrent.futures
import time
from typing import List, Dict, Optional
from urllib.parse import quote
from io import BytesIO
import edge_tts
from PIL import Image, ImageDraw, ImageFont, ImageFilter

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
OUTPUT_FILE = "final_debate_output.mp4"
VIDEO_W = 1920
VIDEO_H = 1080
FPS = 30

# ============================================================
# DEBATE SETTINGS - FREE MODE REDUCES CALLS
# ============================================================
FREE_MODE = True  # True = ~60% fewer OpenRouter calls for free tier
ROUNDS = 3
WORDS_PER_SIDE_PER_ROUND = 500
TURNS_PER_SIDE_PER_ROUND = 3 if FREE_MODE else 4
WORDS_PER_TURN = 125
MIN_TURN_WORDS = 105
MAX_TURN_WORDS = 145

MAX_JUDGES = 3 if FREE_MODE else 7
JUDGE_WORKERS = 7
PANEL_COMMENTS_PER_ROUND = 1 if FREE_MODE else 2

MAX_VISUALS_PER_SEGMENT = 0 if FREE_MODE else 1  # 0 = no visual LLM calls, 1 = minimal, 2 = full
MIN_VISUAL_GAP = 2.0
VISUAL_X = 700
VISUAL_Y = 525
VISUAL_W = 520
VISUAL_H = 245
MAX_EMOJIS_PER_SEGMENT = 4
EMOJI_W = 180
EMOJI_H = 180

# ============================================================
# TTS VOICES - LOCKED AS REQUESTED (male moderator + constant debaters)
# ============================================================
VOICE_POOL = [
    "en-US-AndrewMultilingualNeural",  # 0 - Male Moderator LOCKED (user wants this)
    "en-US-BrianMultilingualNeural",   # 1 - Apologist LOCKED
    "en-US-AvaMultilingualNeural",     # 2 - Skeptic LOCKED (from your pasted file)
    "en-GB-SoniaNeural",
    "en-AU-NatashaNeural",
    "en-US-JennyNeural",
    "en-GB-RyanNeural",
    "en-US-GuyNeural",
    "en-GB-LibbyNeural",
    "en-US-DavisNeural",
    "en-AU-WilliamNeural",
    "en-CA-ClaraNeural",
]

VOICES = {
    "Moderator": "en-US-AndrewMultilingualNeural",  # male narrator from your file
    "AI Christian Apologist": "en-US-BrianMultilingualNeural",
    "AI Skeptic": "en-US-AvaMultilingualNeural",
    "AI Judge 1": "en-US-ChristopherNeural",
    "AI Judge 2": "en-US-EmmaMultilingualNeural",
    "AI Judge 3": "en-US-GuyNeural",
    "AI Judge 4": "en-US-JennyNeural",
}

JUDGE_VOICES = [
    "en-US-ChristopherNeural",
    "en-US-EmmaMultilingualNeural",
    "en-US-GuyNeural",
    "en-US-JennyNeural",
    "en-GB-RyanNeural",
    "en-AU-WilliamNeural",
    "en-CA-ClaraNeural",
]

CAST_VOICE_ASSIGNMENT = {}
JUDGE_VOICE_MAP = {}

def assign_unique_voices():
    global CAST_VOICE_ASSIGNMENT, JUDGE_VOICE_MAP
    CAST_VOICE_ASSIGNMENT = {
        "AI Christian Apologist": "en-US-BrianMultilingualNeural",
        "AI Skeptic": "en-US-AvaMultilingualNeural",
        "Moderator": "en-US-AndrewMultilingualNeural",
        "MODERATOR": "en-US-AndrewMultilingualNeural",
    }
    JUDGE_VOICE_MAP = {}
    print(f"Voice cast LOCKED: Moderator=Andrew(male), Apologist=Brian, Skeptic=Ava, Judges vary")
    return CAST_VOICE_ASSIGNMENT

# ============================================================
# FALLBACK MODELS - FREE ONLY
# ============================================================
FALLBACK_MODELS = [
    "openai/gpt-4o-mini:free",
    "google/gemini-2.0-flash-001:free",
    "anthropic/claude-3-haiku:free",
    "mistralai/mistral-small:free",
    "meta-llama/llama-3.1-70b-instruct:free",
    "qwen/qwen-2.5-72b-instruct:free",
    "deepseek/deepseek-chat:free",
]

PROVIDER_ALIASES = {
    "openai": "OpenAI", "anthropic": "Anthropic", "google": "Google",
    "x-ai": "xAI", "xai": "xAI", "deepseek": "DeepSeek",
    "mistralai": "Mistral", "mistral": "Mistral",
    "meta-llama": "Meta", "meta": "Meta",
    "qwen": "Alibaba / Qwen", "cohere": "Cohere",
}

def provider_from_model(model_id):
    if not model_id: return "Unknown"
    prefix = model_id.split("/", 1)[0].lower().strip()
    return PROVIDER_ALIASES.get(prefix, prefix.replace("-", " ").title())

def cleanup_cache():
    print("🧹 Cleaning temporary files...")
    patterns = ["*.mp4", "*.mp3", "*.ass", "*.png", "*.gif", "*_list.txt"]
    protected = {OUTPUT_FILE, "background.png", "topic.txt"}
    for pattern in patterns:
        for filename in glob.glob(pattern):
            if filename in protected: continue
            try: os.remove(filename)
            except: pass
    print("✨ Workspace cleaned.")

def count_words(text): return len(re.findall(r"\b[\w'-]+\b", text or ""))
def clean_for_speech(text):
    text = re.sub(r"\([^)]*\)", "", text or "")
    replacements = {"*": "", "#": "", "_": "", "`": "", "–": "-", "—": "-", '"': "", ":": " ", ";": " ", "&": "and"}
    for old, new in replacements.items(): text = text.replace(old, new)
    text = re.sub(r"\s+", " ", text)
    return text.strip()
def clamp_score(value):
    try: value = float(value)
    except: value = 50.0
    return max(0.0, min(100.0, value))
def load_font(size, bold=False):
    if bold: paths = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf", "C:\\Windows\\Fonts\\arialbd.ttf"]
    else: paths = ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf", "/System/Library/Fonts/Supplemental/Arial.ttf", "C:\\Windows\\Fonts\\arial.ttf"]
    for path in paths:
        try: return ImageFont.truetype(path, size)
        except: continue
    return ImageFont.load_default()
def hex_to_rgba(hex_str, alpha):
    h = hex_str.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), alpha)

# ============================================================
# OPENROUTER
# ============================================================
def openrouter_headers():
    return {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json", "HTTP-Referer": "https://openrouter.ai/", "X-Title": "AI Debate Arena"}
def discover_models():
    if not OPENROUTER_API_KEY: raise RuntimeError("OPENROUTER_API_KEY is missing.")
    try:
        response = requests.get(OPENROUTER_MODELS_URL, headers=openrouter_headers(), timeout=20)
        if response.status_code != 200:
            print(f"⚠️ Model discovery returned HTTP {response.status_code}")
            return []
        data = response.json()
        models = []
        for item in data.get("data", []):
            model_id = item.get("id")
            if not model_id: continue
            lowered = model_id.lower()
            excluded = ["embed", "tts", "whisper", "audio", "image", "vision", "moderation", "guard"]
            if any(x in lowered for x in excluded): continue
            models.append(model_id)
        return list(dict.fromkeys(models))
    except Exception as exc:
        print(f"⚠️ Model discovery failed: {str(exc)[:200]}")
        return []
def query_openrouter(prompt, model_id, timeout=60, max_tokens=1200, temperature=0.7):
    if not OPENROUTER_API_KEY: return None
    payload = {"model": model_id, "messages": [{"role": "user", "content": prompt}], "temperature": temperature, "max_tokens": max_tokens}
    for attempt in range(2):  # reduced retries to save calls
        try:
            response = requests.post(OPENROUTER_URL, headers=openrouter_headers(), json=payload, timeout=timeout)
            if response.status_code == 200:
                data = response.json()
                choices = data.get("choices", [])
                if choices:
                    content = choices[0].get("message", {}).get("content", "")
                    if content and len(content.strip()) > 10: return content.strip()
            else:
                print(f"⚠️ {provider_from_model(model_id)} returned HTTP {response.status_code}")
                if response.status_code == 402:  # free limit hit, don't retry same model
                    return None
        except Exception as exc:
            print(f"⚠️ Request failed for {provider_from_model(model_id)}: {str(exc)[:120]}")
        if attempt < 1: time.sleep(1.0)
    return None

def choose_primary_models(available_models):
    preference = ["openai/gpt-4o-mini:free", "google/gemini-2.0-flash-001:free", "anthropic/claude-3-haiku:free", "mistralai/mistral-small:free"]
    found = [m for m in preference if m in set(available_models)]
    if len(found) >= 2: return found[0], found[1]
    if len(found) == 1:
        remaining = [m for m in available_models if m != found[0]]
        if remaining: return found[0], remaining[0]
    if len(available_models) >= 2: return (available_models[0], available_models[1])
    return (FALLBACK_MODELS[0], FALLBACK_MODELS[1])
def choose_judges(available_models, primary_models):
    excluded = set(primary_models)
    candidates = [m for m in available_models if m not in excluded]
    groups = {}
    for model in candidates:
        provider = provider_from_model(model)
        groups.setdefault(provider, []).append(model)
    preferred_keywords = ["gpt", "claude", "gemini", "grok", "deepseek", "mistral", "llama"]
    selected = []
    for provider, models in groups.items():
        models.sort(key=lambda m: (0 if any(k in m.lower() for k in preferred_keywords) else 1, len(m)))
        selected.append((provider, models[0]))
    priority = ["OpenAI", "Anthropic", "Google", "xAI", "DeepSeek", "Mistral", "Meta"]
    selected.sort(key=lambda x: (priority.index(x[0]) if x[0] in priority else 999, x[0]))
    return [model for _, model in selected[:MAX_JUDGES]]

# ============================================================
# REAL DEBATE GENERATION - SPECIFIC ARGUMENTS
# ============================================================
USED_OPENERS = set()
USED_ARGUMENTS = set()

def generate_fallback_debate(role_label, topic, round_num, turn_num, opponent_last):
    """Fallback with specific arguments when LLM fails - not nonsense"""
    topic_low = topic.lower()
    is_for = "APOLOGIST" in role_label.upper() or "FOR" in role_label.upper()
    
    # Specific argument banks
    if "god" in topic_low and "exist" in topic_low:
        if is_for:
            args = [
                "The fine-tuning of the cosmological constant is 1 in 10^120. If it varied by that much, no life. That's not random - that's precision like finding a single atom in the universe.",
                "Consciousness itself - subjective experience, qualia - has no material explanation. Brain scans show activity but not why red feels red. Materialism can't account for the observer.",
                "The universe began - Borde-Guth-Vilenkin theorem shows past-eternal inflation is impossible. Something that begins needs a cause outside time and space.",
                "Objective moral values exist - torture is wrong whether or not society says so. If no God, morality is just preference, but we live as if it's binding.",
                "The resurrection has historical grounding - 1 Corinthians 15 creed dates to within 2-5 years of event, enemy attestation, women as witnesses in patriarchal culture wouldn't be invented.",
            ]
        else:
            args = [
                "Suffering is gratuitous - a fawn dying slowly in forest fire for days with no human to learn from. An omnipotent loving God could prevent that without losing greater good.",
                "Divine hiddenness - if God wants relationship, why is evidence so ambiguous? Why do sincere seekers in other cultures find different gods? Non-resistant non-believers exist.",
                "The problem of parsimony - we don't need God to explain universe. Quantum fluctuations, multiverse, or brute fact are simpler. Adding God multiplies entities without necessity.",
                "Inconsistent revelations - thousands of contradictory religions, each claiming divine truth. If one God revealed, why so much confusion? That looks like human invention.",
                "Evolution explains apparent design without designer. The watchmaker argument fails when you have natural selection producing complex structures over billions of years.",
            ]
    elif "jesus" in topic_low or "resurrect" in topic_low:
        if is_for:
            args = ["The minimal facts approach - even skeptical scholars grant Jesus died, disciples believed they saw risen, Paul converted, James converted, tomb empty. Best explanation is resurrection, not hallucinations."]
        else:
            args = ["Hallucinations don't produce group appearances to 500 people, and Jewish context wouldn't produce resurrection belief - they expected general resurrection at end of time, not one man in middle of history."]
    else:
        if is_for:
            args = [f"Consider {topic} - the cumulative case matters. One piece alone might not convince, but together cosmological, moral, consciousness, and experiential evidence points one direction."]
        else:
            args = [f"On {topic}, the burden of proof is on claimant. Extraordinary claims need extraordinary evidence, and anecdotal experience isn't enough when cognitive biases explain it."]
    
    idx = (round_num + turn_num) % len(args)
    base = args[idx]
    if opponent_last and round_num > 1:
        return f"On what you just said about {opponent_last[:60]} - that misses the key point. {base} That directly answers your objection because if that premise fails, your whole chain fails."
    return base

def generate_turn(side, topic, round_num, turn_num, previous_exchange, model):
    global USED_OPENERS, USED_ARGUMENTS
    if side == "A":
        side_name = "AI Christian Apologist"
        opponent = "AI Skeptic"
        side_short = "for"
        opp_short = "against"
    else:
        side_name = "AI Skeptic"
        opponent = "AI Christian Apologist"
        side_short = "against"
        opp_short = "for"
    opponent_last = previous_exchange[-800:] if previous_exchange else ""
    banned_openers = [
        "I hear you about the problem of suffering",
        "I hear you on the suffering point",
        "You make a fair point about suffering",
        "I understand the concern about suffering",
        "Suffering is a powerful concern",
        "I hear you about",
    ]
    if round_num == 1 and turn_num == 1:
        instruction = f"""Opening. Establish strong foundation without delivering entire debate.
- Do NOT say "I hear you" - no opponent yet.
- Give 2-3 SPECIFIC reasons with numbers, examples, names.
- Topic is {topic}. Be concrete, not vague.
- Example: instead of "universe is fine-tuned", say "cosmological constant 1 in 10^120"."""
    else:
        instruction = f"""Turn {turn_num} round {round_num} - DEEP REBUTTAL REQUIRED.
Opponent's last: "{opponent_last[:500]}"
BANNED: {', '.join(banned_openers)} - do NOT use generic openers.
YOUR TASK:
1. State opponent's core claim in own words (1 sentence).
2. Show why it fails FROM YOUR SIDE with direct counter-evidence (3-4 sentences, specific).
3. Add ONE fresh specific point you haven't used.
Do NOT recycle: {'; '.join(list(USED_ARGUMENTS)[-3:])}
Be specific - use numbers, names, thought experiments."""

    prompt = f"""You are {side_name} arguing {side_short} on: {topic}
Opponent: {opponent}
{instruction}
Previous (don't repeat): {previous_exchange[-800:] if previous_exchange else "None"}
Write ONLY spoken contribution. Target {WORDS_PER_TURN} words, {MIN_TURN_WORDS}-{MAX_TURN_WORDS}. Natural speech, YouTube audience, specific examples, no headings, no lists, no AI mentions, no banned openers."""

    for attempt in range(2):  # reduced attempts to save calls
        temp = 0.8 + attempt*0.1
        response = query_openrouter(prompt, model, max_tokens=400, temperature=temp)
        if not response: continue
        low = response.lower()
        has_banned = any(b.lower() in low for b in banned_openers)
        is_repeat = any(len(arg)>25 and arg.lower() in low for arg in list(USED_ARGUMENTS)[-5:])
        if has_banned:
            retry = f"Banned phrase used. Banned: {banned_openers}. Opponent: {opponent_last[:250]}. Rewrite starting with specific counter, not generic: {response[:350]}"
            r2 = query_openrouter(retry, model, max_tokens=380, temperature=0.85)
            if r2 and not any(b.lower() in r2.lower() for b in banned_openers):
                response = r2
                low = response.lower()
                is_repeat = any(len(arg)>25 and arg.lower() in low for arg in list(USED_ARGUMENTS)[-5:])
        if not is_repeat or attempt==1:
            for sent in response.split('. ')[:2]:
                if len(sent)>20: USED_ARGUMENTS.add(sent[:60])
            return response
    # Fallback with specific arguments, not nonsense
    return generate_fallback_debate(side_name, topic, round_num, turn_num, opponent_last)

def build_round_exchanges(topic, round_num, apologist_model, skeptic_model, previous_history):
    apologist_turns = []
    skeptic_turns = []
    exchange_history = previous_history
    for turn_num in range(1, TURNS_PER_SIDE_PER_ROUND + 1):
        apologist = generate_turn("A", topic, round_num, turn_num, exchange_history, apologist_model)
        apologist_turns.append(apologist)
        exchange_history = "AI Christian Apologist:\n" + apologist + "\n\n"
        skeptic = generate_turn("B", topic, round_num, turn_num, exchange_history, skeptic_model)
        skeptic_turns.append(skeptic)
        exchange_history += "AI Skeptic:\n" + skeptic + "\n\n"
    return (apologist_turns, skeptic_turns, exchange_history)

# ============================================================
# JUDGING
# ============================================================
def neutral_judge(model):
    return {"model": model, "provider": provider_from_model(model), "A_argument": 50, "A_rebuttal": 50, "A_clarity": 50, "A_total": 50, "B_argument": 50, "B_rebuttal": 50, "B_clarity": 50, "B_total": 50, "winner": "A"}

def judge_round(model, topic, round_num, apologist, skeptic):
    prompt = f"""You are impartial judge evaluating round {round_num} on: {topic}
FOR: {apologist[:1000]}
AGAINST: {skeptic[:1000]}
Score: argument strength (specific evidence?), rebuttal quality (direct counter or generic "I hear you"? Generic=0-30, direct=70-100), clarity.
Return ONLY JSON: {{"A_argument":0,"A_rebuttal":0,"A_clarity":0,"A_total":0,"B_argument":0,"B_rebuttal":0,"B_clarity":0,"B_total":0}}"""
    response = query_openrouter(prompt, model, timeout=30, max_tokens=250, temperature=0.1)
    if not response: return neutral_judge(model)
    try:
        match = re.search(r"\{{.*\}}", response, re.DOTALL)
        if not match: return neutral_judge(model)
        data = json.loads(match.group(0))
        aa = clamp_score(data.get("A_argument", 50)); ar = clamp_score(data.get("A_rebuttal", 50)); ac = clamp_score(data.get("A_clarity", 50))
        ba = clamp_score(data.get("B_argument", 50)); br = clamp_score(data.get("B_rebuttal", 50)); bc = clamp_score(data.get("B_clarity", 50))
        at = (aa+ar+ac)/3; bt = (ba+br+bc)/3
        return {"model": model, "provider": provider_from_model(model), "A_argument": aa, "A_rebuttal": ar, "A_clarity": ac, "A_total": round(at,2), "B_argument": ba, "B_rebuttal": br, "B_clarity": bc, "B_total": round(bt,2), "winner": "A" if at>bt else "B"}
    except: return neutral_judge(model)

def evaluate_round(judges, topic, round_num, apologist, skeptic):
    results = []
    print(f"⚖️ Asking {len(judges)} judges (free mode)…")
    def worker(model): return judge_round(model, topic, round_num, apologist, skeptic)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(JUDGE_WORKERS, len(judges)))) as executor:
        futures = {executor.submit(worker, m): m for m in judges}
        for future in concurrent.futures.as_completed(futures):
            try:
                r = future.result()
                results.append(r)
                print(f"   ✓ {r['provider']} {r['A_total']:.1f} vs {r['B_total']:.1f}")
            except Exception as exc: print(f"   ✗ {str(exc)[:80]}")
    if not results: results = [neutral_judge("fallback")]
    return results

def calculate_round_average(results):
    a = sum(r["A_total"] for r in results)/len(results)
    b = sum(r["B_total"] for r in results)/len(results)
    return round(a,2), round(b,2)

# ============================================================
# TTS
# ============================================================
async def generate_audio_async(text, voice, filename):
    communicate = edge_tts.Communicate(text, voice, rate="+0%", volume="+0%")
    audio = b""; words = []
    async for chunk in communicate.stream():
        if chunk["type"] == "audio": audio += chunk["data"]
        elif chunk["type"] == "WordBoundary":
            start = chunk["offset"]/10_000_000
            duration = chunk["duration"]/10_000_000
            words.append({"text": chunk["text"], "start": start, "duration": duration, "end": start+duration})
    with open(filename, "wb") as file: file.write(audio)
    if not words:
        clean = clean_for_speech(text); t=0.0
        for token in clean.split():
            if not token: continue
            dur=0.38
            words.append({"text": token, "start": t, "duration": dur, "end": t+dur})
            t+=dur+0.05
    return words

def generate_audio(text, role, filename, judge_voice_index=None):
    if role == "AI Judge":
        voice = JUDGE_VOICES[(judge_voice_index or 0) % len(JUDGE_VOICES)]
    else:
        voice = VOICES.get(role, VOICES["Moderator"])
    clean_text = clean_for_speech(text)
    try: return asyncio.run(generate_audio_async(clean_text, voice, filename))
    except Exception as exc:
        print(f"⚠️ TTS failed {voice}: {str(exc)[:120]}")
        return asyncio.run(generate_audio_async(clean_text, VOICES["Moderator"], filename))

# ============================================================
# SUBTITLES - SIMPLE CLEAN VERSION (no highlighted distracting)
# ============================================================
def format_ass_time(seconds):
    seconds = max(0.0, float(seconds))
    h = int(seconds//3600); m = int((seconds%3600)//60); s = seconds%60
    return f"{h}:{m:02d}:{s:05.2f}"
def ass_escape(text):
    text=str(text); text=text.replace("\\", r"\\"); text=text.replace("{", r"\{"); text=text.replace("}", r"\}"); text=text.replace("\n"," ")
    return text

def generate_subtitles(words, filename, scorecard=False, audio_file=None, full_text=None):
    # Simple clean subtitles like few builds before - no per-word highlight
    header="[Script Info]\nScriptType: v4.00+\nPlayResX: 1920\nPlayResY: 1080\nWrapStyle: 0\n\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\nStyle: DebateSub,DejaVu Sans,42,&H00FFFFFF,&H00FFFFFF,&H00000000,&HAA000000,1,0,0,0,100,100,0,0,1,3,1,2,120,120,80,1\nStyle: ScoreSub,DejaVu Sans,36,&H00FFFFFF,&H00FFFFFF,&H00000000,&HAA000000,1,0,0,0,100,100,0,0,1,2,1,2,80,80,40,1\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    events=[]
    if scorecard and audio_file and full_text:
        dur=get_audio_duration(audio_file) or 6.0
        txt=ass_escape(full_text)
        events.append(f"Dialogue: 0,0:00:00.00,{format_ass_time(dur)},ScoreSub,,0,0,0,,{txt}")
        open(filename,"w",encoding="utf-8").write(header+"\n".join(events)+"\n")
        return
    if not words: open(filename,"w",encoding="utf-8").write(header); return
    if audio_file:
        try:
            actual=get_audio_duration(audio_file)
            if actual>1 and words:
                est=words[-1].get("end",actual)
                if abs(est-actual)>0.5 and est>0:
                    scale=actual/est
                    for w in words: w["start"]=w["start"]*scale; w["end"]=w["end"]*scale
        except: pass
    chunk=[]; last_end=0
    for w in words:
        if not chunk: chunk=[w]; last_end=w["end"]
        elif w["start"]-last_end>0.6 or len(chunk)>=7:
            s=chunk[0]["start"]; e=last_end
            txt_words=[ass_escape(c["text"]) for c in chunk]
            lines=[]
            for i in range(0,len(txt_words),10): lines.append(" ".join(txt_words[i:i+10]))
            if len(lines)>4: lines=lines[:4]
            txt="\\N".join(lines)
            txt_clean=f"{{\\an2\\pos(960,800)\\q2\\fad(120,120)}}{txt}"
            events.append(f"Dialogue: 0,{format_ass_time(s)},{format_ass_time(e)},DebateSub,,0,0,0,,{txt_clean}")
            chunk=[w]; last_end=w["end"]
        else: chunk.append(w); last_end=w["end"]
    if chunk:
        s=chunk[0]["start"]; e=last_end
        txt_words=[ass_escape(c["text"]) for c in chunk]
        lines=[]
        for i in range(0,len(txt_words),10): lines.append(" ".join(txt_words[i:i+10]))
        if len(lines)>4: lines=lines[:4]
        txt="\\N".join(lines)
        txt_clean=f"{{\\an2\\pos(960,800)\\q2\\fad(120,120)}}{txt}"
        events.append(f"Dialogue: 0,{format_ass_time(s)},{format_ass_time(e)},DebateSub,,0,0,0,,{txt_clean}")
    open(filename,"w",encoding="utf-8").write(header+"\n".join(events)+"\n")
    print(f" 📝 Subs: {len(events)} events -> {filename}")

# ============================================================
# EMOJI SYSTEM - RESTORED (few builds before)
# ============================================================
SAFE_EMOJIS = ["🧑","👥","🌿","🌱","🍎","🌳","🐍","👀","🙈","😨","💀","⚔️","👼","💡","🧠","✨","🌌","⭐","🌍","🤔","🔍","✅","⚖️","😇","😈","😣","🔬","🙏","❤️","💥","🎨"]
def create_emoji_plan(text, words):
    if not words: return []
    word_emoji_map={
        "adam":"🧑","man":"🧑","men":"👥","human":"🧑","person":"👤","people":"👥",
        "garden":"🌿","eden":"🌿","plant":"🌱","apple":"🍎","fruit":"🍎","eat":"🍎","tree":"🌳",
        "serpent":"🐍","snake":"🐍","eyes":"👀","eye":"👀","naked":"🙈","shame":"🙈",
        "afraid":"😨","fear":"😨","hide":"😨","death":"💀","die":"💀","sword":"⚔️","angel":"👼",
        "knowledge":"💡","wise":"🧠","god":"✨","lord":"✨","universe":"🌌","cosmos":"🌌","stars":"⭐","world":"🌍","earth":"🌍",
        "exist":"🤔","evidence":"🔍","proof":"🔍","real":"✅","moral":"⚖️","good":"😇","evil":"😈","suffering":"😣","pain":"😣",
        "science":"🔬","faith":"🙏","believe":"🤔","love":"❤️","begin":"🌱","cause":"💥","design":"🎨",
    }
    plan=[]; used_times=[]
    for w in words:
        clean_w = re.sub(r"[^a-z]", "", w["text"].lower())
        if clean_w in word_emoji_map:
            start=float(w["start"]); end=float(w["end"])+3.5
            if any(not (end < s or start > e) for s,e in used_times): continue
            if used_times and start - used_times[-1][1] < 1.0: continue
            emoji_char=word_emoji_map[clean_w]
            if emoji_char not in SAFE_EMOJIS: continue
            if emoji_char in [p["emoji"] for p in plan[-2:]]: continue
            plan.append({"emoji": emoji_char, "start": max(0.0,start), "end": end, "label": clean_w, "word": w["text"]})
            used_times.append((start,end))
            if len(plan)>=MAX_EMOJIS_PER_SEGMENT: break
    return plan

def create_emoji_asset(emoji_char, index):
    filename = f"emoji_{index}.png"
    size = 180
    img = Image.new("RGBA", (size, size), (0,0,0,0))
    draw = ImageDraw.Draw(img)
    # Draw emoji as text with large font
    try:
        font = load_font(120, bold=True)
        # Center
        box = draw.textbbox((0,0), emoji_char, font=font)
        w = box[2]-box[0]; h = box[3]-box[1]
        draw.text(((size-w)//2, (size-h)//2 - 10), emoji_char, font=font, fill=(255,255,255,255))
    except:
        draw.ellipse([20,20,size-20,size-20], fill=(255,215,0,200))
    img.save(filename)
    return filename

# ============================================================
# VISUALS - IMAGE ONLY NO DESCRIPTION
# ============================================================
def plan_visuals(text, model):
    if FREE_MODE and MAX_VISUALS_PER_SEGMENT == 0: return []
    if FREE_MODE:
        # In free mode, only call LLM if strong visual noun present to save calls
        visual_keywords = ["adam","eve","garden","eden","serpent","apple","tree","ark","moses","jesus","cross","heaven","hell","universe","stars","cosmos"]
        if not any(k in text.lower() for k in visual_keywords): return []
    prompt = f"""You are visual director. Text: {text}
Identify up to {MAX_VISUALS_PER_SEGMENT} concrete visual moments.
Return ONLY JSON: [{{"phrase":"exact phrase from speech","label":"SHORT LABEL","description":"detailed prompt","kind":"person|place|object"}}]
phrase MUST appear verbatim."""
    response = query_openrouter(prompt, model, timeout=30, max_tokens=400, temperature=0.2)
    if not response: return []
    try:
        match = re.search(r"\[.*\]", response, re.DOTALL)
        if not match: return []
        data = json.loads(match.group(0))
        output=[]
        for item in data:
            if not isinstance(item, dict): continue
            phrase=str(item.get("phrase","")).strip()
            label=str(item.get("label","")).strip()
            description=str(item.get("description","")).strip()
            kind=str(item.get("kind","concept")).strip().lower()
            if not phrase or not label: continue
            if phrase.lower() not in text.lower(): continue
            output.append({"phrase": phrase, "label": label[:35], "description": description[:180], "kind": kind})
            if len(output)>=MAX_VISUALS_PER_SEGMENT: break
        return output
    except: return []

def find_phrase_timing(phrase, words):
    if not phrase or not words: return None
    phrase_words = re.findall(r"\b[\w'-]+\b", phrase.lower())
    source_words = [re.sub(r"[^\w'-]", "", str(w["text"]).lower()) for w in words]
    phrase_words = [x for x in phrase_words if x]
    if not phrase_words: return None
    for i in range(0, len(source_words)-len(phrase_words)+1):
        if source_words[i:i+len(phrase_words)] == phrase_words:
            start=float(words[i]["start"])
            end_index=min(len(words)-1, i+len(phrase_words)-1)
            end=float(words[end_index]["end"])+2.5
            return {"start": max(0.0, start-0.15), "end": max(start+2.5, end)}
    return None

def fallback_visual_timing(index, total, words):
    if not words: return None
    last_end=float(words[-1]["end"])
    usable_start=0.15*last_end; usable_end=0.85*last_end
    start=usable_start + ((usable_end-usable_start)*index/max(1,total-1)) if total>1 else usable_start
    return {"start": max(0.0,start), "end": max(start+3.0, start+3.0)}

def create_visual_plan(text, words, model):
    if not words: return []
    if FREE_MODE and MAX_VISUALS_PER_SEGMENT==0: return []
    candidates=plan_visuals(text, model)
    if not candidates: return []
    timed=[]
    for index, item in enumerate(candidates):
        timing=find_phrase_timing(item["phrase"], words) or fallback_visual_timing(index, len(candidates), words)
        if not timing: continue
        item=dict(item); item.update(timing); timed.append(item)
    timed.sort(key=lambda x: x["start"])
    output=[]
    for item in timed:
        if any(abs(item["start"]-p["start"])<MIN_VISUAL_GAP for p in output): continue
        output.append(item)
        if len(output)>=MAX_VISUALS_PER_SEGMENT: break
    return output

def build_visual_prompt(visual):
    label=visual.get("label",""); desc=visual.get("description",""); kind=visual.get("kind","")
    base="cinematic illustration, highly detailed, 4k, dramatic lighting, ultra realistic, youtube documentary style, no text, no watermark"
    if kind=="person": base+=", expressive character portrait"
    elif kind=="place": base+=", epic landscape"
    prompt=f"{desc}. {label}, {base}"
    return prompt

def fetch_topic_image(visual):
    try:
        prompt=build_visual_prompt(visual); encoded=quote(prompt)
        url=f"https://image.pollinations.ai/prompt/{encoded}?width=512&height=512&model=flux&enhance=true&nologo=true&seed={random.randint(0,999999)}"
        print(f" 🖼️ Generating: {visual.get('label')} -> {prompt[:70]}...")
        r=requests.get(url, timeout=25)
        if r.status_code==200 and len(r.content)>10000:
            img=Image.open(BytesIO(r.content)).convert("RGB")
            return img
    except Exception as exc: print(f" ⚠️ Image fetch failed: {exc}")
    return None

def create_visual_asset(visual, index):
    filename=f"visual_{index}.gif"; frames=[]; num_frames=30
    real_img=fetch_topic_image(visual)
    ILLUS_W, ILLUS_H = 320, 240
    ILLUS_X, ILLUS_Y = (VISUAL_W-ILLUS_W)//2, (VISUAL_H-ILLUS_H)//2
    for f in range(num_frames):
        image=Image.new("RGBA",(VISUAL_W,VISUAL_H),(0,0,0,0))
        draw=ImageDraw.Draw(image)
        draw.rounded_rectangle((4,4,VISUAL_W-4,VISUAL_H-4), radius=22, fill=(12,18,35,220), outline=(255,215,0,200), width=3)
        progress=math.sin(math.pi*(f/num_frames)); bob_y=int(4*math.sin(2*math.pi*f/num_frames))
        if real_img:
            scale=1.0+0.15*progress; sz_w=int(ILLUS_W*scale); sz_h=int(ILLUS_H*scale)
            scaled=real_img.resize((sz_w,sz_h), Image.LANCZOS)
            left=(sz_w-ILLUS_W)//2; top=(sz_h-ILLUS_H)//2
            cropped=scaled.crop((left,top,left+ILLUS_W,top+ILLUS_H))
            mask=Image.new("L",(ILLUS_W,ILLUS_H),0)
            ImageDraw.Draw(mask).rounded_rectangle((0,0,ILLUS_W,ILLUS_H), radius=16, fill=255)
            image.paste(cropped, (ILLUS_X, ILLUS_Y+bob_y), mask)
        frames.append(image)
    frames[0].save(filename, format='GIF', save_all=True, append_images=frames[1:], duration=33, loop=0, disposal=2)
    return filename

# ============================================================
# BACKGROUND + NAME CARDS (older build you liked) + SOUND BARS
# ============================================================
def create_background(position, glow_color, filename):
    source=os.path.join(os.path.dirname(os.path.abspath(__file__)),"background.png")
    if os.path.exists(source):
        try: image=Image.open(source).convert("RGB").resize((VIDEO_W,VIDEO_H))
        except: image=Image.new("RGB",(VIDEO_W,VIDEO_H),(12,16,32))
    else:
        image=Image.new("RGB",(VIDEO_W,VIDEO_H),(12,16,32))
        draw=ImageDraw.Draw(image)
        for x in range(0,VIDEO_W,60): draw.line([(x,0),(x,VIDEO_H)], fill=(20,26,45), width=2)
        for y in range(0,VIDEO_H,60): draw.line([(0,y),(VIDEO_W,y)], fill=(20,26,45), width=2)
    overlay=Image.new("RGBA",(VIDEO_W,VIDEO_H),(0,0,0,0)); draw=ImageDraw.Draw(overlay)
    cx=400 if position=="left" else 1520 if position=="right" else 960
    for radius in range(700,50,-50):
        alpha=int(15*(1-radius/700))
        draw.ellipse([cx-radius,540-radius,cx+radius,540+radius], fill=hex_to_rgba(glow_color,alpha))
    overlay=overlay.filter(ImageFilter.GaussianBlur(30))
    result=Image.alpha_composite(image.convert("RGBA"),overlay).convert("RGB")
    result.save(filename)

def create_ui_overlay(speaker_name, topic, position, glow_color, filename):
    image=Image.new("RGBA",(VIDEO_W,VIDEO_H),(0,0,0,0)); draw=ImageDraw.Draw(image)
    title_font=load_font(30,bold=True); name_font=load_font(30,bold=True)
    title=f"TOPIC: {topic}"; box=draw.textbbox((0,0),title,font=title_font); width=box[2]-box[0]
    draw.text(((VIDEO_W-width)//2,24),title,fill="white",font=title_font)
    card_width=650; card_height=110; card_y=885
    if position=="left": card_x=75
    elif position=="right": card_x=1195
    else: card_x=(VIDEO_W-card_width)//2
    draw.rounded_rectangle([card_x,card_y,card_x+card_width,card_y+card_height], radius=18, fill=(18,26,46,235), outline=glow_color, width=4)
    draw.ellipse([card_x+22,card_y+27,card_x+47,card_y+52], fill=glow_color)
    draw.text((card_x+65,card_y+22),speaker_name,fill="white",font=name_font)
    image.save(filename)
    return card_x, card_y

def ffmpeg_filter_path(filename):
    path=os.path.abspath(filename); path=path.replace("\\","/"); path=path.replace("'","\'"); path=path.replace(":","\:")
    return path

def render_video_segment(background=None, ui=None, audio=None, subtitles=None, output=None, position=None, glow_color=None, card_x=None, card_y=None, visual_plan=None, bg_path=None, ui_path=None, audio_path=None, subs_path=None, output_path=None, cx=None, cy=None, **kwargs):
    # Compatibility for both calling styles
    if background is None and bg_path is not None: background=bg_path
    if ui is None and ui_path is not None: ui=ui_path
    if audio is None and audio_path is not None: audio=audio_path
    if subtitles is None and subs_path is not None: subtitles=subs_path
    if output is None and output_path is not None: output=output_path
    if card_x is None and cx is not None: card_x=cx
    if card_y is None and cy is not None: card_y=cy
    if visual_plan is None: visual_plan=kwargs.get('visual_plan') or kwargs.get('eplan') or []
    if position is None: position=kwargs.get('position','center')
    if glow_color is None: glow_color=kwargs.get('glow',kwargs.get('glow_color','#FFD700'))
    if card_x is None: card_x=kwargs.get('card_x',kwargs.get('cx',960))
    if card_y is None: card_y=kwargs.get('card_y',kwargs.get('cy',900))
    required=[background,ui,audio,subtitles]
    for p in required:
        if not os.path.exists(p): raise FileNotFoundError(f"Missing {os.path.abspath(p)}")
    # Emoji + visual assets combined
    visual_assets=[]
    emoji_assets=[]
    # visual_plan can contain both emoji and visuals - separate
    for idx, v in enumerate(visual_plan or []):
        if "emoji" in v:
            try:
                asset=create_emoji_asset(v["emoji"], idx)
                emoji_assets.append((asset,v))
            except: pass
        else:
            try:
                asset=create_visual_asset(v, idx)
                visual_assets.append((asset,v))
            except Exception as exc: print(f"⚠️ Visual skipped {exc}")
    all_assets = visual_assets + emoji_assets
    glow=glow_color.lstrip("#")
    pan_x="0" if position=="left" else "iw-(iw/zoom)" if position=="right" else "(iw-(iw/zoom))/2"
    filter_parts=[]
    filter_parts.append(f"[0:v]scale=1920:1080,zoompan=z='min(zoom+0.00020,1.05)':x='{pan_x}':y='(ih-(ih/zoom))/2':d=9000:s=1920x1080:fps=30[bg];")
    filter_parts.append("[1:v]scale=1920:1080[ui];")
    filter_parts.append(f"[2:a]showwaves=s=300x58:mode=cline:colors=0x{glow}:rate=30[wave];")
    filter_parts.append("[bg][ui]overlay=0:0[base];")
    wave_x=card_x+330; wave_y=card_y+47
    filter_parts.append(f"[base][wave]overlay={wave_x}:{wave_y}[withwave];")
    current="[withwave]"; input_index=3
    for idx, (asset, visual) in enumerate(all_assets):
        label=f"visual{idx}"
        start=max(0.0,float(visual["start"])); end=max(start+2.0,float(visual["end"]))
        filter_parts.append(f"[{input_index}:v]format=rgba,fade=t=in:st={start}:d=0.4:alpha=1,fade=t=out:st={end-0.4}:d=0.4:alpha=1[{label}_faded];")
        x=(VIDEO_W-VISUAL_W)//2
        drift=15
        y_expr=f"{VISUAL_Y}+20-(t-{start})*{drift}"
        enable=f"between(t,{start:.2f},{end:.2f})"
        filter_parts.append(f"{current}[{label}_faded]overlay={x}:'{y_expr}':enable='{enable}'[v{idx}];")
        current=f"[v{idx}]"; input_index+=1
    subtitle_path=ffmpeg_filter_path(subtitles)
    filter_parts.append(f"{current}ass='{subtitle_path}'[outv]")
    filter_complex="".join(filter_parts)
    command=["ffmpeg","-y","-loop","1","-framerate",str(FPS),"-i",background,"-i",ui,"-i",audio]
    for asset,_ in all_assets:
        if asset.endswith(".gif"): command+=["-ignore_loop","0","-i",asset]
        else: command+=["-loop","1","-i",asset]
    command+=["-filter_complex",filter_complex,"-map","[outv]","-map","2:a","-c:v","libx264","-preset","veryfast","-crf","20","-pix_fmt","yuv420p","-c:a","aac","-b:a","192k","-shortest",output]
    result=subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode!=0:
        print("\n❌ FFmpeg failed:"); print(result.stderr[-7000:])
        raise RuntimeError(f"FFmpeg failed creating {output}")
    for asset,_ in all_assets:
        try: os.remove(asset)
        except: pass

# ============================================================
# NEWER SCOREBOARD (you preferred)
# ============================================================
def get_audio_duration(path):
    try:
        r=subprocess.run(["ffprobe","-v","error","-show_entries","format=duration","-of","default=noprint_wrappers=1:nokey=1",path],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=10)
        return float(r.stdout.strip())
    except: return 0.0

def generate_scoreboard(round_num, results, round_a, round_b, cumulative_a, cumulative_b, filename, roles=None):
    W=VIDEO_W; H=VIDEO_H
    source=os.path.join(os.path.dirname(os.path.abspath(__file__)),"background.png")
    if os.path.exists(source):
        try: base=Image.open(source).convert("RGB").resize((W,H),Image.LANCZOS)
        except: base=Image.new("RGB",(W,H),(12,16,32))
    else: base=Image.new("RGB",(W,H),(12,16,32))
    overlay=Image.new("RGBA",(W,H),(0,0,0,180))
    img=Image.alpha_composite(base.convert("RGBA"),overlay).convert("RGB")
    draw=ImageDraw.Draw(img)
    font_title=load_font(48,bold=True); font_sub=load_font(28,bold=True); font_head=load_font(22,bold=True); font_row=load_font(24)
    title=f"ROUND {round_num} SCORES"
    draw.text((W//2,50),title,font=font_title,fill=(255,215,0,255),anchor="mt")
    if roles:
        roles_text=f"{roles.get('side_a_label','FOR')}  vs  {roles.get('side_b_label','AGAINST')}"
        draw.text((W//2,115),roles_text,font=font_sub,fill=(255,255,255,230),anchor="mt")
    header_y=190; col_judge_x=120; col_a_x=750; col_b_x=1050; col_winner_x=1350
    short_a="APOLOGIST"; short_b="SKEPTIC"
    if roles:
        short_a=roles.get('side_a_label','APOLOGIST').split()[0][:12]
        short_b=roles.get('side_b_label','SKEPTIC').split()[0][:12]
    draw.rectangle([60,header_y-10,W-60,header_y+45],fill=(25,35,70,255),outline=(255,215,0,180),width=2)
    draw.text((col_judge_x,header_y),"Judge",font=font_head,fill=(255,255,255,230))
    draw.text((col_a_x,header_y),short_a,font=font_head,fill=(0,255,204,255))
    draw.text((col_b_x,header_y),short_b,font=font_head,fill=(255,120,255,255))
    draw.text((col_winner_x,header_y),"Winner",font=font_head,fill=(255,215,0,255))
    y=header_y+65
    for idx,res in enumerate(results):
        if idx%2==0: draw.rectangle([60,y-8,W-60,y+42],fill=(20,28,50,255))
        else: draw.rectangle([60,y-8,W-60,y+42],fill=(15,22,40,255))
        judge_text=f"{res.get('provider','Judge')}"
        if len(judge_text)>32: judge_text=judge_text[:30]+".."
        draw.text((col_judge_x,y),judge_text,font=font_row,fill=(255,255,255,240))
        draw.text((col_a_x,y),f"{res['A_total']:.1f}",font=font_row,fill=(0,255,204,255))
        draw.text((col_b_x,y),f"{res['B_total']:.1f}",font=font_row,fill=(255,120,255,255))
        win_label=short_a if res['winner']=="A" else short_b
        win_color=(0,255,204,255) if res['winner']=="A" else (255,120,255,255)
        draw.text((col_winner_x,y),win_label,font=font_row,fill=win_color)
        y+=58
    draw.line([(60,y+5),(W-60,y+5)],fill=(255,255,255,60),width=2)
    y+=25
    avg_text=f"Round Avg: {round_a:.1f} vs {round_b:.1f}"
    cum_text=f"Cumulative: {cumulative_a:.1f} vs {cumulative_b:.1f}"
    draw.text((W//2,y),avg_text,font=font_sub,fill=(255,255,255,255),anchor="mt")
    draw.text((W//2,y+45),cum_text,font=font_sub,fill=(255,215,0,255),anchor="mt")
    img.save(filename)

def render_scorecard_video(scorecard, audio, subtitles, output):
    for path in [scorecard, audio, subtitles]:
        if not os.path.exists(path): raise FileNotFoundError(f"Scorecard missing {os.path.abspath(path)}")
    subtitle_path=ffmpeg_filter_path(subtitles)
    filter_complex=f"[0:v]scale=1920:1080[base];[base]ass='{subtitle_path}'[outv]"
    command=["ffmpeg","-y","-loop","1","-framerate",str(FPS),"-i",scorecard,"-i",audio,"-filter_complex",filter_complex,"-map","[outv]","-map","1:a","-c:v","libx264","-preset","veryfast","-crf","20","-pix_fmt","yuv420p","-c:a","aac","-b:a","192k","-shortest",output]
    result=subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode!=0:
        print(result.stderr[-7000:])
        raise RuntimeError("Scorecard render failed.")

def create_segment(text, role, speaker_name, topic, segment_id, model_for_visuals, position=None, glow=None, judge_voice_index=None):
    if position is None:
        if role=="AI Christian Apologist": position="left"
        elif role=="AI Skeptic": position="right"
        else: position="center"
    if glow is None:
        if role=="AI Christian Apologist": glow="#00FFCC"
        elif role=="AI Skeptic": glow="#FF00FF"
        elif role=="AI Judge": glow="#3399FF"
        else: glow="#FFD700"
    af=f"audio_{segment_id}.mp3"; sf=f"subs_{segment_id}.ass"; bf=f"bg_{segment_id}.png"; uf=f"ui_{segment_id}.png"; vf=f"segment_{segment_id}.mp4"
    words=generate_audio(text, role, af, judge_voice_index)
    # Clean subtitles (no highlight)
    try: generate_subtitles(words, sf, scorecard=False, audio_file=af, full_text=text)
    except TypeError: generate_subtitles(words, sf)
    # Visual + emoji plan combined
    vplan=[]
    eplan=[]
    try:
        if not FREE_MODE or MAX_VISUALS_PER_SEGMENT>0:
            vplan=create_visual_plan(clean_for_speech(text), words, model_for_visuals)
    except Exception as e: print(f"Visual planning skipped: {e}")
    try:
        eplan=create_emoji_plan(clean_for_speech(text), words)
        if eplan: print(f"   {len(eplan)} emoji(s): {', '.join(v['emoji']+'('+v['word']+')' for v in eplan)}")
    except Exception as e: print(f"Emoji planning skipped: {e}")
    combined = vplan + eplan
    create_background(position, glow, bf)
    cx,cy=create_ui_overlay(speaker_name, topic, position, glow, uf)
    render_video_segment(background=bf, ui=uf, audio=af, subtitles=sf, output=vf, position=position, glow_color=glow, card_x=cx, card_y=cy, visual_plan=combined)
    return vf

def generate_panel_commentary(model, side, topic, round_num, apologist, skeptic, previous_comments):
    provider=provider_from_model(model)
    pref_spoken="the case for" if side=="A" else "the case against"
    other_spoken="the case against" if side=="A" else "the case for"
    recent="\n".join(previous_comments[-4:])
    def trim(t,mw=180):
        t=t.strip()
        if len(t.split())<=mw: return t
        sents=t.split('. ')
        if len(sents)>=2: return sents[0][:150]+" ... "+sents[-1][:150]
        return " ".join(t.split()[:mw])
    def extract_core(txt):
        low=txt.lower()
        if "evil" in low or "suffer" in low: return "suffering and whether it undercuts God"
        if "hidden" in low: return "why God isn't more obvious"
        if "fine tuning" in low or "tuned" in low: return "fine-tuning"
        if "cause" in low or "began" in low: return "whether universe needs cause"
        if "moral" in low: return "objective moral values"
        return "central clash"
    ap_core=extract_core(apologist); sk_core=extract_core(skeptic)
    prompt=f"""You are {provider}, judge for round {round_num} on: {topic}
FOR: {trim(apologist)}
AGAINST: {trim(skeptic)}
You leaned {pref_spoken}.
TASK - Summarise, don't list: 1 sentence clash ({ap_core} vs {sk_core}), 1 sentence why {pref_spoken} handled better - specific reasoning quality.
Do NOT list "for said X, against said Y". Use short names.
2 sentences total.
Previous: {recent}"""
    for attempt in range(2):
        resp=query_openrouter(prompt, model, timeout=30, max_tokens=200, temperature=0.85 if attempt==0 else 0.9)
        if resp and count_words(resp)>=12:
            low=resp.lower()
            if "for said" in low or "against said" in low:
                retry=f"Summarise clash, don't list. Round {round_num}: {ap_core} vs {sk_core}. You leaned {pref_spoken}. 2 sentences: {resp[:250]}"
                r2=query_openrouter(retry, model, timeout=30, max_tokens=200, temperature=0.88)
                if r2: resp=r2
            return resp
    return f"Round {round_num} came down to {ap_core} versus {sk_core}. For me, {pref_spoken} edged it because it directly answered the other side's push."

def build_intro(topic, judge_count):
    return f"Welcome to the AI Debate Arena. Today, an AI Christian Apologist faces an AI Skeptic on: {topic}. The debate will unfold over three rounds with equal speaking time. An independent panel of {judge_count} AI systems will score argument strength, rebuttal quality, and clarity. Let's begin."

def build_outro(judge_count, cumulative_a, cumulative_b):
    if math.isclose(cumulative_a,cumulative_b,abs_tol=0.01): result="a draw"
    elif cumulative_a>cumulative_b: result="the AI Christian Apologist"
    else: result="the AI Skeptic"
    return f"After three rounds, our panel of {judge_count} AI judges gave the Apologist {cumulative_a:.1f} versus {cumulative_b:.1f} for the Skeptic. The final result is {result}. But the final verdict is still yours."

def stitch_segments(segments, output):
    list_file="concat_list.txt"
    with open(list_file,"w",encoding="utf-8") as file:
        for segment in segments:
            path=os.path.abspath(segment); path=path.replace("'","'\\''")
            file.write(f"file '{path}'\n")
    print("🎬 Stitching final video...")
    command=["ffmpeg","-y","-f","concat","-safe","0","-i",list_file,"-c","copy",output]
    result=subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode!=0:
        print(result.stderr[-7000:])
        raise RuntimeError("Final FFmpeg concatenation failed.")

def run_debate_pipeline():
    cleanup_cache()
    if not OPENROUTER_API_KEY: raise RuntimeError("OPENROUTER_API_KEY missing.")
    if not os.path.exists("topic.txt"): open("topic.txt","w",encoding="utf-8").write("Does the universe require a creator?")
    topic=open("topic.txt","r",encoding="utf-8").read().strip() or "Does the universe require a creator?"
    print("\n"+"="*70+"\nAI DEBATE ARENA\n"+"="*70+f"\n\nTOPIC: {topic}\n")
    assign_unique_voices()
    available_models=discover_models()
    if not available_models:
        print("⚠️ Using fallback models")
        available_models=FALLBACK_MODELS.copy()
    apologist_model, skeptic_model = choose_primary_models(available_models)
    print(f"🎤 Debate engines: {provider_from_model(apologist_model)} vs {provider_from_model(skeptic_model)}")
    judges=choose_judges(available_models,(apologist_model,skeptic_model))
    if not judges:
        judges=FALLBACK_MODELS[:MAX_JUDGES]
    print(f"⚖️ Judges: {len(judges)} (FREE_MODE={FREE_MODE})")
    for m in judges: print(f"   • {provider_from_model(m)} — {m.split('/',1)[-1][:28]}")
    segments=[]; segment_id=0
    def add_segment(text,role,name,position=None,glow=None,judge_voice_index=None):
        nonlocal segment_id
        vm=skeptic_model if role=="AI Skeptic" else apologist_model
        video=create_segment(text,role,name,topic,segment_id,vm,position,glow,judge_voice_index)
        segments.append(video); segment_id+=1
    add_segment(build_intro(topic,len(judges)),"Moderator","MODERATOR")
    previous_history=""; cumulative_a=0.0; cumulative_b=0.0; panel_comments=[]
    roles={"side_a_label":"APOLOGIST","side_b_label":"SKEPTIC"}
    for round_num in range(1, ROUNDS+1):
        print("\n"+"="*70+f"\nROUND {round_num}\n"+"="*70)
        apologist_turns, skeptic_turns, previous_history = build_round_exchanges(topic, round_num, apologist_model, skeptic_model, previous_history)
        for turn_index in range(TURNS_PER_SIDE_PER_ROUND):
            ap_text=apologist_turns[turn_index]; sk_text=skeptic_turns[turn_index]
            print(f"   Exchange {turn_index+1}: A={count_words(ap_text)} words | B={count_words(sk_text)} words")
            add_segment(ap_text,"AI Christian Apologist","AI CHRISTIAN APOLOGIST","left","#00FFCC")
            add_segment(sk_text,"AI Skeptic","AI SKEPTIC","right","#FF00FF")
        ap_full="\n".join(apologist_turns); sk_full="\n".join(skeptic_turns)
        print(f"   Round total: A={count_words(ap_full)} words | B={count_words(sk_full)} words")
        results=evaluate_round(judges, topic, round_num, ap_full, sk_full)
        round_a, round_b = calculate_round_average(results)
        cumulative_a+=round_a; cumulative_b+=round_b
        print(f"📊 Round {round_num}: A {round_a:.1f} vs B {round_b:.1f}")
        print(f"📊 Cumulative: A {cumulative_a:.1f} vs B {cumulative_b:.1f}")
        scoreboard_file=f"scoreboard_r{round_num}.png"
        generate_scoreboard(round_num, results, round_a, round_b, cumulative_a, cumulative_b, scoreboard_file, roles)
        score_text=f"Round {round_num} is complete. The {len(results)} independent AI judges gave the Apologist an average score of {round_a:.1f}, and the Skeptic an average score of {round_b:.1f}. The cumulative score is {cumulative_a:.1f} to {cumulative_b:.1f}."
        score_audio=f"score_audio_r{round_num}.mp3"; score_subs=f"score_subs_r{round_num}.ass"; score_video=f"score_video_r{round_num}.mp4"
        score_words=generate_audio(score_text,"Moderator",score_audio)
        generate_subtitles(score_words, score_subs, scorecard=True, audio_file=score_audio, full_text=score_text)
        render_scorecard_video(scoreboard_file, score_audio, score_subs, score_video)
        segments.append(score_video)
        if results:
            if PANEL_COMMENTS_PER_ROUND==1:
                wj=random.choice(results)
                comment=generate_panel_commentary(wj["model"], wj["winner"], topic, round_num, ap_full, sk_full, panel_comments)
                panel_comments.append(comment)
                add_segment(comment,"AI Judge","AI JUDGE — "+wj["provider"].upper(),"center","#3399FF", judge_voice_index=0)
            else:
                a_results=[r for r in results if r["winner"]=="A"]; b_results=[r for r in results if r["winner"]=="B"]
                if not a_results: a_results=results
                if not b_results: b_results=results
                ja=random.choice(a_results); jb=random.choice(b_results)
                ca=generate_panel_commentary(ja["model"],"A",topic,round_num,ap_full,sk_full,panel_comments); panel_comments.append(ca)
                add_segment(ca,"AI Judge","AI JUDGE — "+ja["provider"].upper(),"center","#3399FF", judge_voice_index=0)
                cb=generate_panel_commentary(jb["model"],"B",topic,round_num,ap_full,sk_full,panel_comments); panel_comments.append(cb)
                add_segment(cb,"AI Judge","AI JUDGE — "+jb["provider"].upper(),"center","#3399FF", judge_voice_index=1)
    add_segment(build_outro(len(judges),cumulative_a,cumulative_b),"Moderator","MODERATOR")
    stitch_segments(segments, OUTPUT_FILE)
    print("\n"+"="*70+"\n✅ DEBATE COMPLETE\n"+"="*70)
    print(f"🎥 Output: {OUTPUT_FILE}")
    print(f"⚖️ Judges: {len(judges)}")
    print(f"🏆 Final: Apologist {cumulative_a:.1f} vs Skeptic {cumulative_b:.1f}")
    cleanup_cache()

if __name__=="__main__":
    try: run_debate_pipeline()
    except KeyboardInterrupt: print("\n⛔ Cancelled.")
    except Exception as exc:
        print("\n❌ PIPELINE FAILED"); print(str(exc)); raise
