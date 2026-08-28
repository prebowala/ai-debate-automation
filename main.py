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


# ============================================================
# AI DEBATE ARENA
# FULL TOPIC-ADAPTIVE VERSION - FIXED SUBS + REAL VISUALS
# ============================================================

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"

OUTPUT_FILE = "final_debate_output.mp4"

VIDEO_W = 1920
VIDEO_H = 1080
FPS = 30


# ============================================================
# DEBATE SETTINGS
# ============================================================

ROUNDS = 3
WORDS_PER_SIDE_PER_ROUND = 500
TURNS_PER_SIDE_PER_ROUND = 4
WORDS_PER_TURN = 125
MIN_TURN_WORDS = 105
MAX_TURN_WORDS = 145


# ============================================================
# JUDGING
# ============================================================

MAX_JUDGES = 7
JUDGE_WORKERS = 7


# ============================================================
# VISUALS
# ============================================================

MAX_VISUALS_PER_SEGMENT = 2
MIN_VISUAL_GAP = 2.0
VISUAL_X = 700
VISUAL_Y = 525
VISUAL_W = 520
VISUAL_H = 245


# ============================================================
# TTS VOICES
# ============================================================

VOICES = {
    "Moderator": "en-US-AndrewMultilingualNeural",
    "AI Christian Apologist": "en-US-BrianMultilingualNeural",
    "AI Skeptic": "en-US-AvaMultilingualNeural",
    "AI Judge": "en-US-ChristopherNeural",
    "AI Judge 1": "en-US-ChristopherNeural",
    "AI Judge 2": "en-US-EmmaMultilingualNeural",
    "AI Judge 3": "en-US-GuyNeural",
    "AI Judge 4": "en-GB-RyanNeural",
    "AI Judge 5": "en-AU-WilliamNeural",
    "AI Judge 6": "en-CA-ClaraNeural",
    "AI Judge 7": "en-US-JennyNeural",
}

# Each commenting AI gets its own distinct natural voice - best for edge-tts
JUDGE_VOICES = [
    "en-US-ChristopherNeural",      # Judge 0 - deep male US
    "en-US-EmmaMultilingualNeural", # Judge 1 - warm female US
    "en-US-GuyNeural",              # Judge 2 - confident male
    "en-GB-RyanNeural",             # Judge 3 - British male
    "en-AU-WilliamNeural",          # Judge 4 - Australian male
    "en-CA-ClaraNeural",            # Judge 5 - Canadian female
    "en-US-JennyNeural",            # Judge 6 - bright female US
]


# ============================================================
# FALLBACK MODELS
# ============================================================

FALLBACK_MODELS = [
    "openai/gpt-4o-mini",
    "google/gemini-2.0-flash-001",
    "anthropic/claude-3.5-haiku",
    "mistralai/mistral-small",
    "meta-llama/llama-3.1-70b-instruct",
    "qwen/qwen-2.5-72b-instruct",
    "deepseek/deepseek-chat",
]


# ============================================================
# PROVIDER NAMES
# ============================================================

PROVIDER_ALIASES = {
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "google": "Google",
    "x-ai": "xAI",
    "xai": "xAI",
    "deepseek": "DeepSeek",
    "mistralai": "Mistral",
    "mistral": "Mistral",
    "meta-llama": "Meta",
    "meta": "Meta",
    "qwen": "Alibaba / Qwen",
    "cohere": "Cohere",
    "perplexity": "Perplexity",
    "microsoft": "Microsoft",
    "amazon": "Amazon",
    "nvidia": "NVIDIA",
    "moonshotai": "Moonshot AI",
    "moonshot": "Moonshot AI",
    "01-ai": "01.AI",
    "ai21": "AI21",
    "writer": "Writer",
    "nousresearch": "Nous Research",
    "rekaai": "Reka",
    "reka": "Reka",
    "databricks": "Databricks",
}


def provider_from_model(model_id):
    if not model_id:
        return "Unknown"
    prefix = model_id.split("/", 1)[0].lower().strip()
    return PROVIDER_ALIASES.get(prefix, prefix.replace("-", " ").title())


# ============================================================
# CLEANUP
# ============================================================

def cleanup_cache():
    print("🧹 Cleaning temporary files...")
    patterns = ["*.mp4", "*.mp3", "*.ass", "*.png", "*.gif", "*_list.txt"]
    protected = {OUTPUT_FILE, "background.png", "topic.txt"}
    for pattern in patterns:
        for filename in glob.glob(pattern):
            if filename in protected:
                continue
            try:
                os.remove(filename)
            except Exception:
                pass
    print("✨ Workspace cleaned.")


# ============================================================
# BASIC UTILITIES
# ============================================================

def count_words(text):
    return len(re.findall(r"\b[\w'-]+\b", text or ""))

def clean_for_speech(text):
    text = re.sub(r"\([^)]*\)", "", text or "")
    replacements = {"*": "", "#": "", "_": "", "`": "", "–": "-", "—": "-", '"': "", ":": " ", ";": " ", "&": "and"}
    for old, new in replacements.items():
        text = text.replace(old, new)
    text = re.sub(r"\s+", " ", text)
    return text.strip()

def clamp_score(value):
    try:
        value = float(value)
    except Exception:
        value = 50.0
    return max(0.0, min(100.0, value))

def load_font(size, bold=False):
    if bold:
        paths = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
            "C:\\Windows\\Fonts\\arialbd.ttf",
        ]
    else:
        paths = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "C:\\Windows\\Fonts\\arial.ttf",
        ]
    for path in paths:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()

def hex_to_rgba(hex_str, alpha):
    h = hex_str.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), alpha)


# ============================================================
# OPENROUTER
# ============================================================

def openrouter_headers():
    return {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://openrouter.ai/",
        "X-Title": "AI Debate Arena",
    }

def discover_models():
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY is missing.")
    try:
        response = requests.get(OPENROUTER_MODELS_URL, headers=openrouter_headers(), timeout=20)
        if response.status_code != 200:
            print(f"⚠️ Model discovery returned HTTP {response.status_code}")
            return []
        data = response.json()
        models = []
        for item in data.get("data", []):
            model_id = item.get("id")
            if not model_id:
                continue
            lowered = model_id.lower()
            excluded = ["embed", "tts", "whisper", "audio", "image", "vision", "moderation", "guard"]
            if any(x in lowered for x in excluded):
                continue
            models.append(model_id)
        return list(dict.fromkeys(models))
    except Exception as exc:
        print(f"⚠️ Model discovery failed: {str(exc)[:200]}")
        return []

def query_openrouter(prompt, model_id, timeout=60, max_tokens=1200, temperature=0.7):
    if not OPENROUTER_API_KEY:
        return None
    payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    for attempt in range(3):
        try:
            response = requests.post(OPENROUTER_URL, headers=openrouter_headers(), json=payload, timeout=timeout)
            if response.status_code == 200:
                data = response.json()
                choices = data.get("choices", [])
                if choices:
                    content = choices[0].get("message", {}).get("content", "")
                    if content and len(content.strip()) > 10:
                        return content.strip()
            else:
                print(f"⚠️ {provider_from_model(model_id)} returned HTTP {response.status_code}")
        except Exception as exc:
            print(f"⚠️ Request failed for {provider_from_model(model_id)}: {str(exc)[:120]}")
        if attempt < 2:
            time.sleep(1.5 * (attempt + 1))
    return None


# ============================================================
# MODEL SELECTION
# ============================================================

def choose_primary_models(available_models):
    preference = [
        "openai/gpt-4o", "openai/gpt-4o-mini", "openai/gpt-4.1-mini",
        "anthropic/claude-3.5-sonnet", "anthropic/claude-3.5-haiku",
        "google/gemini-2.5-flash", "google/gemini-2.0-flash-001",
        "deepseek/deepseek-chat", "qwen/qwen-2.5-72b-instruct",
    ]
    found = [m for m in preference if m in set(available_models)]
    if len(found) >= 2:
        return found[0], found[1]
    if len(found) == 1:
        remaining = [m for m in available_models if m != found[0]]
        if remaining:
            return found[0], remaining[0]
    if len(available_models) >= 2:
        return (available_models[0], available_models[1])
    return (FALLBACK_MODELS[0], FALLBACK_MODELS[1])

def choose_judges(available_models, primary_models):
    excluded = set(primary_models)
    candidates = [m for m in available_models if m not in excluded]
    groups = {}
    for model in candidates:
        provider = provider_from_model(model)
        groups.setdefault(provider, []).append(model)
    preferred_keywords = ["gpt", "claude", "gemini", "grok", "deepseek", "mistral", "llama", "qwen", "command", "nemotron"]
    selected = []
    for provider, models in groups.items():
        models.sort(key=lambda m: (0 if any(k in m.lower() for k in preferred_keywords) else 1, len(m)))
        selected.append((provider, models[0]))
    priority = ["OpenAI", "Anthropic", "Google", "xAI", "DeepSeek", "Mistral", "Meta", "Alibaba / Qwen", "Cohere", "Perplexity"]
    selected.sort(key=lambda x: (priority.index(x[0]) if x[0] in priority else 999, x[0]))
    return [model for _, model in selected[:MAX_JUDGES]]


# ============================================================
# DEBATE GENERATION
# ============================================================

def generate_turn(side, topic, round_num, turn_num, previous_exchange, model, full_history=""):
    if side == "A":
        side_name = "AI Christian Apologist"
        side_label = "FOR the existence of God / that God told the truth"
        opponent = "AI Skeptic"
        opponent_label = "AGAINST"
    else:
        side_name = "AI Skeptic"
        side_label = "AGAINST the existence of God / that the serpent told the truth"
        opponent = "AI Christian Apologist"
        opponent_label = "FOR"

    # Extract opponent's last argument for specific rebuttal
    last_opponent_arg = ""
    if previous_exchange:
        # Get last 400 chars of previous exchange for immediate rebuttal target
        last_opponent_arg = previous_exchange.strip()[-800:]

    if round_num == 1 and turn_num == 1:
        instruction = f"""OPENING - Round {round_num} Turn {turn_num}: This is your first argument about "{topic}". 
You must:
1. State your core position clearly about this EXACT topic, not generic debate talk
2. Give ONE specific piece of evidence - if topic is about Genesis/God/serpent, cite specific verses like Genesis 2:17, 3:4, 3:22, 5:5, Hebrew words like 'moth tamuth', 'yom'. If topic is about creator/universe, cite specific cosmology, fine-tuning, Big Bang evidence.
3. Explain WHY that evidence matters for this topic
Do NOT say "I am affirmative" - argue the actual point."""
    elif round_num == 1:
        instruction = f"""OPENING BUILD - Round {round_num} Turn {turn_num} about "{topic}":
Add a SECOND distinct specific argument with concrete evidence for this exact topic. Must be different from your previous points. If Genesis topic, bring in Tree of Life, cherubim, dust to dust, shame, exile. If other topic, bring new data, study, or logical argument. Reference the topic by name."""
    else:
        instruction = f"""REBUTTAL - Round {round_num} Turn {turn_num} about "{topic}":
Opponent just argued: "{last_opponent_arg[:400]}"
You MUST:
1. Directly quote or paraphrase ONE specific claim opponent made about {topic}
2. Show specifically why that claim fails with counter-evidence (verse, data, logic) about {topic}
3. Add one NEW specific point about {topic} that opponent hasn't addressed
Do NOT restart debate. Do NOT be generic. Be surgical about {topic}. If they said "yom means 24 hours", rebut with Genesis 2:4 usage. If they said "fine-tuning", rebut with multiverse or necessity argument etc."""

    prompt = f"""
You are the {side_name} arguing {side_label} in a serious public YouTube debate.
Topic: "{topic}" - You must stay laser-focused on this topic, not generic debate.
Opponent: {opponent} arguing {opponent_label}

{instruction}

Previous debate context for continuity (do NOT repeat, use for awareness):
{full_history[-1200:] if full_history else "None - opening."}

Immediate previous exchange to rebut (if any):
{previous_exchange[-600:] if previous_exchange else "None - you are opening."}

Write ONLY your spoken contribution.
Target {WORDS_PER_TURN} words, range {MIN_TURN_WORDS}-{MAX_TURN_WORDS}.
Use natural conversational speech with contractions, like a real person debating.
MUST be specific to "{topic}" - mention topic keywords, specific verses if biblical, specific evidence if scientific.
No headings. No numbered lists. No bullet points. No meta commentary. No "As an AI". Do not mention AI models or companies.
If biblical topic, you MUST reference at least one specific chapter:verse or Hebrew word in this turn.
If cosmological topic, MUST reference specific scientific concept, data, or philosopher.
"""

    for attempt_temp in [0.85, 0.92, 0.78]:
        response = query_openrouter(prompt, model, max_tokens=550, temperature=attempt_temp)
        if response and len(response.split()) >= 70:
            # Filter out generic filler
            low = response.lower()
            if "affirmative" in low and len(low) < 200:
                continue
            if "as an ai" in low:
                continue
            # Ensure topic-specific
            if topic.lower().split()[0] not in low and "god" not in low and "genesis" not in low and "universe" not in low and "creator" not in low:
                # Too generic, retry
                if attempt_temp == 0.78:
                    continue
            return response.strip()
    
    # Fallback - topic-specific, not generic
    if side == "A":
        if "genesis" in topic.lower() or "god" in topic.lower() and "serpent" in topic.lower():
            return f"Look at what the text actually says about {topic}. Genesis 2 verse 17 says 'moth tamuth' - dying you shall die, emphatic certainty in Hebrew. The serpent in 3:4 says 'lo moth temuthun' - you shall not surely die, directly negating God. What happens that very day? Genesis 3:10 Adam hides in fear, that's relational death, separation from God. Verse 22 says lest he take the tree of life and live forever, and verse 24 blocks it with cherubim. On that day they lost access to everlasting life. That's death beginning that day, which is exactly what God warned about in {topic}."
        else:
            return f"When we look at {topic}, the key is what best explains what we actually observe. The universe had a beginning - Borde-Guth-Vilenkin theorem shows inflationary spacetime is past-incomplete. That means something beyond space and time. And consciousness - you can map neurons firing but you never find the taste of coffee or the feeling of love in the chemistry. That subjective 'what it's like' points beyond mere matter for {topic}."
    else:
        if "genesis" in topic.lower() or "god" in topic.lower() and "serpent" in topic.lower():
            return f"Read the plain narrative of {topic}. Genesis 2:17 says in the day you eat you shall die - natural reading is same day. Genesis 5:5 says Adam lived 930 years then died. He didn't die that day. The serpent in 3:4 says you shall not surely die, and that matches - they didn't die that day. He also says in 3:5 your eyes shall be opened and you shall be as gods knowing good and evil, and 3:7 says their eyes were opened, and God Himself confirms in 3:22 behold man has become as one of us to know good and evil. Two predictions from serpent both happen that day, one threat from God doesn't happen as stated that day for {topic}."
        else:
            return f"On {topic}, we have to ask what the evidence actually demands. Quantum mechanics shows events without deterministic cause at that level - virtual particles. And fine-tuning might be observer selection - if there are many universes with different constants, we obviously find ourselves in one where we can exist. Plus suffering - a deer burning for days in a forest fire with no one learning anything - if you could stop it easily and you cared, you would. That tension is central to {topic}."

def build_round_exchanges(topic, round_num, apologist_model, skeptic_model, previous_history):
    apologist_turns = []
    skeptic_turns = []
    exchange_history = previous_history  # Full accumulated history
    last_exchange = ""  # Immediate previous turn for rebuttal
    
    for turn_num in range(1, TURNS_PER_SIDE_PER_ROUND + 1):
        # Apologist turn - gets full history + last exchange
        apologist = generate_turn("A", topic, round_num, turn_num, last_exchange, apologist_model, exchange_history)
        apologist_turns.append(apologist)
        exchange_history += f"\nAI Christian Apologist (Round {round_num} Turn {turn_num}):\n{apologist}\n"
        last_exchange = apologist  # For skeptic to rebut
        
        # Skeptic turn - gets full history + apologist's just-made argument
        skeptic = generate_turn("B", topic, round_num, turn_num, last_exchange, skeptic_model, exchange_history)
        skeptic_turns.append(skeptic)
        exchange_history += f"\nAI Skeptic (Round {round_num} Turn {turn_num}):\n{skeptic}\n"
        last_exchange = skeptic  # For next apologist turn to rebut
    
    return (apologist_turns, skeptic_turns, exchange_history)


# ============================================================
# JUDGING
# ============================================================

def neutral_judge(model):
    return {"model": model, "provider": provider_from_model(model), "A_argument": 50, "A_rebuttal": 50, "A_clarity": 50, "A_total": 50, "B_argument": 50, "B_rebuttal": 50, "B_clarity": 50, "B_total": 50, "winner": "A"}

def judge_round(model, topic, round_num, apologist, skeptic):
    prompt = f"""
You are an independent and impartial debate judge.
Topic: {topic}
Round: {round_num}
SIDE A — AI CHRISTIAN APOLOGIST:
{apologist}
SIDE B — AI SKEPTIC:
{skeptic}
Evaluate both sides independently.
Score: 1. Argument strength 2. Rebuttal quality 3. Clarity and reasoning
Score every category from 0 to 100.
Return ONLY valid JSON:
{{
"A_argument": 0, "A_rebuttal": 0, "A_clarity": 0, "A_total": 0,
"B_argument": 0, "B_rebuttal": 0, "B_clarity": 0, "B_total": 0
}}
"""
    response = query_openrouter(prompt, model, timeout=35, max_tokens=250, temperature=0.1)
    if not response:
        return neutral_judge(model)
    try:
        match = re.search(r"\{.*\}", response, re.DOTALL)
        if not match:
            return neutral_judge(model)
        data = json.loads(match.group(0))
        aa = clamp_score(data.get("A_argument", 50))
        ar = clamp_score(data.get("A_rebuttal", 50))
        ac = clamp_score(data.get("A_clarity", 50))
        ba = clamp_score(data.get("B_argument", 50))
        br = clamp_score(data.get("B_rebuttal", 50))
        bc = clamp_score(data.get("B_clarity", 50))
        at = (aa + ar + ac) / 3
        bt = (ba + br + bc) / 3
        return {"model": model, "provider": provider_from_model(model), "A_argument": aa, "A_rebuttal": ar, "A_clarity": ac, "A_total": round(at, 2), "B_argument": ba, "B_rebuttal": br, "B_clarity": bc, "B_total": round(bt, 2), "winner": "A" if at > bt else "B"}
    except Exception:
        return neutral_judge(model)

def evaluate_round(judges, topic, round_num, apologist, skeptic):
    results = []
    print(f"⚖️ Asking {len(judges)} independent AI judges...")
    def worker(model):
        return judge_round(model, topic, round_num, apologist, skeptic)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(JUDGE_WORKERS, len(judges)))) as executor:
        futures = {executor.submit(worker, model): model for model in judges}
        completed = 0
        for future in concurrent.futures.as_completed(futures):
            model = futures[future]
            try:
                result = future.result()
                results.append(result)
                completed += 1
                print(f"   ✓ Judge {completed}/{len(judges)} — {result['provider']}")
            except Exception as exc:
                print(f"   ✗ Judge failed {provider_from_model(model)}: {str(exc)[:100]}")
    if not results:
        results = [neutral_judge("fallback/fallback")]
    return results

def calculate_round_average(results):
    a = sum(r["A_total"] for r in results) / len(results)
    b = sum(r["B_total"] for r in results) / len(results)
    return round(a, 2), round(b, 2)


# ============================================================
# TTS - FIXED FOR MISSING WORD TIMING
# ============================================================

async def generate_audio_async(text, voice, filename, rate="+2%"):
    # Use natural rate and pitch for best quality
    communicate = edge_tts.Communicate(text, voice, rate=rate, volume="+0%")
    audio = b""
    words = []
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio += chunk["data"]
        elif chunk["type"] == "WordBoundary":
            start = chunk["offset"] / 10_000_000
            duration = chunk["duration"] / 10_000_000
            words.append({"text": chunk["text"], "start": start, "duration": duration, "end": start + duration})
    with open(filename, "wb") as file:
        file.write(audio)

    # FIX: if edge-tts gave no WordBoundary, estimate so subs never disappear
    if not words:
        clean = clean_for_speech(text)
        t = 0.0
        for token in clean.split():
            if not token: continue
            dur = 0.38
            words.append({"text": token, "start": t, "duration": dur, "end": t+dur})
            t += dur + 0.05
    return words

def generate_audio(text, role, filename, judge_voice_index=None):
    # FIXED: Ensure each role gets its own distinct natural voice, no moderator bleed
    if role == "AI Judge":
        if judge_voice_index is None:
            judge_voice_index = 0
        voice = JUDGE_VOICES[judge_voice_index % len(JUDGE_VOICES)]
        fallback_voices = [v for v in JUDGE_VOICES if v != voice]
    elif role == "AI Christian Apologist":
        voice = VOICES["AI Christian Apologist"]  # Brian - deep male, best for apologist
        fallback_voices = ["en-US-ChristopherNeural", "en-US-GuyNeural", "en-US-AndrewMultilingualNeural"]
    elif role == "AI Skeptic":
        voice = VOICES["AI Skeptic"]  # Ava - clear female, best for skeptic
        fallback_voices = ["en-US-EmmaMultilingualNeural", "en-US-JennyNeural", "en-GB-SoniaNeural"]
    elif role == "Moderator":
        voice = VOICES["Moderator"]  # Andrew - neutral moderator
        fallback_voices = ["en-US-GuyNeural", "en-US-BrianMultilingualNeural"]
    else:
        voice = VOICES.get(role, VOICES["Moderator"])
        fallback_voices = [VOICES["Moderator"]]

    clean_text = clean_for_speech(text)
    if not clean_text or len(clean_text) < 5:
        clean_text = text[:500]

    # Try primary voice with slightly faster rate for natural debate pacing
    try:
        return asyncio.run(generate_audio_async(clean_text, voice, filename, rate="+8%"))
    except Exception as exc:
        print(f"⚠️ TTS failed primary {voice} for {role}: {str(exc)[:150]} - trying fallback")
        for fb_voice in fallback_voices:
            try:
                return asyncio.run(generate_audio_async(clean_text, fb_voice, filename, rate="+5%"))
            except:
                continue
        # Last resort moderator
        print(f"⚠️ All TTS fallbacks failed for {role}, using moderator")
        return asyncio.run(generate_audio_async(clean_text, VOICES["Moderator"], filename, rate="+0%"))


# ============================================================
# SUBTITLES - FIXED ANIMATED + NEVER MISSING
# ============================================================

def format_ass_time(seconds):
    seconds = max(0.0, float(seconds))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f"{hours}:{minutes:02d}:{secs:05.2f}"

def ass_escape(text):
    text = str(text)
    text = text.replace("\\", r"\\")
    text = text.replace("{", r"\{")
    text = text.replace("}", r"\}")
    text = text.replace("\n", " ")
    return text

def generate_subtitles(words, filename, scorecard=False):
    margin_v = 90 if scorecard else 230
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: DebateSub,DejaVu Sans,44,&H00FFFFFF,&H0000D7FF,&H00000000,&HCC000000,1,0,0,0,100,100,0,0,1,4,1.5,2,280,280,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    if not words:
        with open(filename, "w", encoding="utf-8") as file:
            file.write(header)
        return

    chunks = []
    current_chunk = []
    MAX_CHUNK_WORDS = 8
    MIN_CHUNK_WORDS = 5
    for w in words:
        current_chunk.append(w)
        is_boundary = str(w["text"]).strip().endswith(('.', '?', '!', ','))
        if (len(current_chunk) >= MIN_CHUNK_WORDS and is_boundary) or len(current_chunk) >= MAX_CHUNK_WORDS:
            chunks.append(current_chunk)
            current_chunk = []
    if current_chunk:
        chunks.append(current_chunk)

    events = []
    for chunk in chunks:
        if not chunk:
            continue
        # IMPORTANT: do not add +0.45 here, or -shortest cuts the last line
        chunk_end = float(chunk[-1]["end"])

        if scorecard:
            start = float(chunk[0]["start"])
            end = chunk_end + 0.25
            text = "{\\an2\\pos(960,840)\\q2\\fad(100,150)}" + " ".join(ass_escape(w["text"]) for w in chunk)
            events.append(f"Dialogue: 0,{format_ass_time(start)},{format_ass_time(end)},DebateSub,,0,0,0,,{text}")
            continue

        # Animated per-word cumulative reveal
        for i, w in enumerate(chunk):
            w_start = float(w["start"])
            if i + 1 < len(chunk):
                w_end = float(chunk[i+1]["start"])
            else:
                w_end = chunk_end
            if w_end - w_start < 0.15:
                w_end = w_start + 0.15

            prev_text = " ".join(ass_escape(c["text"]) for c in chunk[:i])
            curr_text = ass_escape(w["text"])

            if i == 0:
                line = f"{{\\an2\\pos(960,840)\\q2\\fad(80,0)\\move(960,850,960,840,0,180)\\fscx0\\fscy0\\t(0,160,\\fscx100\\fscy100)}}{prev_text} {{\\c&H00D7FF&\\b1\\fscx120\\fscy120\\t(0,130,\\fscx105\\fscy105)\\t(130,320,\\fscx100\\fscy100\\c&H00FFFFFF&\\b0)}}{curr_text}"
            else:
                base = (prev_text + " ") if prev_text else ""
                if i == len(chunk)-1:
                    line = f"{{\\an2\\pos(960,840)\\q2}}{base}{{\\c&H00D7FF&\\b1\\fscx120\\fscy120\\t(0,130,\\fscx105\\fscy105)\\t(130,300,\\fscx100\\fscy100\\c&H00FFFFFF&\\b0)\\fad(0,180)}}{curr_text}"
                else:
                    line = f"{{\\an2\\pos(960,840)\\q2}}{base}{{\\c&H00D7FF&\\b1\\fscx120\\fscy120\\t(0,130,\\fscx105\\fscy105)\\t(130,300,\\fscx100\\fscy100\\c&H00FFFFFF&\\b0)}}{curr_text}"

            events.append(f"Dialogue: 0,{format_ass_time(w_start)},{format_ass_time(w_end)},DebateSub,,0,0,0,,{line}")

    with open(filename, "w", encoding="utf-8") as file:
        file.write(header + "\n".join(events) + "\n")
    print(f" 📝 Subs: {len(events)} events -> {filename}")


# ============================================================
# TOPIC-ADAPTIVE VISUAL PLANNER - FIXED FOR REAL IMAGES
# ============================================================

def plan_visuals(text, model):
    prompt = f"""
You are a visual director for a YouTube debate.

Read this spoken section:
{text}

Identify up to {MAX_VISUALS_PER_SEGMENT} concrete moments that MUST be shown visually.
Prefer visual nouns: people eating, objects, places, historical events, biblical scenes.
If speech says "Adam ate the apple" you MUST return Adam eating apple, not generic "person".
If it says "Garden of Eden", "Noah's Ark", "Moses parting sea" - return exactly that.

Return ONLY valid JSON:
[
  {{
    "phrase": "exact phrase from speech",
    "label": "SHORT 2-5 WORD LABEL",
    "description": "detailed image prompt, e.g. 'Adam and Eve in Garden of Eden, Adam biting red apple, serpent in tree, cinematic'",
    "kind": "person|place|object|process|concept|history|comparison"
  }}
]
Rules:
- phrase MUST appear verbatim in speech
- label is for card title
- description MUST be a full Midjourney-style prompt, not one word
- Max {MAX_VISUALS_PER_SEGMENT} items
"""
    response = query_openrouter(prompt, model, timeout=35, max_tokens=600, temperature=0.2)
    if not response:
        return []
    try:
        match = re.search(r"\[.*\]", response, re.DOTALL)
        if not match:
            return []
        data = json.loads(match.group(0))
        if not isinstance(data, list):
            return []
        output = []
        for item in data:
            if not isinstance(item, dict):
                continue
            phrase = str(item.get("phrase", "")).strip()
            label = str(item.get("label", "")).strip()
            description = str(item.get("description", "")).strip()
            kind = str(item.get("kind", "concept")).strip().lower()
            if not phrase or not label:
                continue
            if phrase.lower() not in text.lower():
                continue
            output.append({"phrase": phrase, "label": label[:35], "description": description[:220], "kind": kind})
            if len(output) >= MAX_VISUALS_PER_SEGMENT:
                break
        return output
    except Exception:
        return []

def find_phrase_timing(phrase, words):
    if not phrase or not words:
        return None
    phrase_words = re.findall(r"\b[\w'-]+\b", phrase.lower())
    source_words = [re.sub(r"[^\w'-]", "", str(w["text"]).lower()) for w in words]
    phrase_words = [x for x in phrase_words if x]
    if not phrase_words:
        return None
    for i in range(0, len(source_words) - len(phrase_words) + 1):
        if source_words[i:i + len(phrase_words)] == phrase_words:
            start = float(words[i]["start"])
            end_index = min(len(words) - 1, i + len(phrase_words) - 1)
            end = float(words[end_index]["end"]) + 2.5
            return {"start": max(0.0, start - 0.15), "end": max(start + 2.5, end)}
    for phrase_word in phrase_words:
        if len(phrase_word) < 4:
            continue
        for index, source_word in enumerate(source_words):
            if phrase_word == source_word:
                start = float(words[index]["start"])
                end_index = min(len(words) - 1, index + 12)
                end = float(words[end_index]["end"]) + 1.5
                return {"start": start, "end": end}
    return None

def fallback_visual_timing(index, total, words):
    if not words:
        return None
    last_end = float(words[-1]["end"])
    usable_start = 0.15 * last_end
    usable_end = 0.85 * last_end
    if total <= 1:
        start = usable_start
    else:
        start = usable_start + ((usable_end - usable_start) * index / max(1, total - 1))
    return {"start": max(0.0, start), "end": max(start + 3.0, start + 3.0)}

def create_visual_plan(text, words, model):
    if not words:
        return []
    candidates = plan_visuals(text, model)
    if not candidates:
        return []
    timed = []
    for index, item in enumerate(candidates):
        timing = find_phrase_timing(item["phrase"], words)
        if not timing:
            timing = fallback_visual_timing(index, len(candidates), words)
        if not timing:
            continue
        item = dict(item)
        item.update(timing)
        timed.append(item)
    timed.sort(key=lambda x: x["start"])
    output = []
    for item in timed:
        if any(abs(item["start"] - previous["start"]) < MIN_VISUAL_GAP for previous in output):
            continue
        output.append(item)
        if len(output) >= MAX_VISUALS_PER_SEGMENT:
            break
    return output


# ============================================================
# DYNAMIC ANIMATED VISUAL CARD - REAL TOPIC IMAGES
# ============================================================

def build_visual_prompt(visual):
    label = visual.get("label", "")
    desc = visual.get("description", "")
    kind = visual.get("kind", "")
    base_style = "cinematic illustration, highly detailed, 4k, dramatic lighting, ultra realistic, youtube documentary style, no text, no watermark"
    if kind == "person":
        base_style += ", expressive character portrait"
    elif kind == "place":
        base_style += ", epic landscape, wide angle"
    elif kind == "history":
        base_style += ", historical biblical painting style"
    prompt = f"{desc}. {label}, {base_style}"
    return prompt

def fetch_topic_image(visual):
    try:
        prompt = build_visual_prompt(visual)
        encoded = quote(prompt)
        url = f"https://image.pollinations.ai/prompt/{encoded}?width=512&height=512&model=flux&enhance=true&nologo=true&seed={random.randint(0,999999)}"
        print(f" 🖼️ Generating: {visual.get('label')} -> {prompt[:90]}...")
        r = requests.get(url, timeout=30)
        if r.status_code == 200 and len(r.content) > 10000:
            img = Image.open(BytesIO(r.content)).convert("RGB")
            return img
    except Exception as exc:
        print(f" ⚠️ Image fetch failed: {exc}")
    return None

def create_visual_asset(visual, index):
    filename = f"visual_{index}.gif"
    frames = []
    num_frames = 30

    label_font = load_font(27, bold=True)
    desc_font = load_font(17)

    label = visual.get("label", "KEY IDEA").upper()
    description = visual.get("description", "")

    real_img = fetch_topic_image(visual)

    # word wrap for description
    dummy_img = Image.new("RGBA", (1, 1))
    dummy_draw = ImageDraw.Draw(dummy_img)
    words = description.split()
    lines = []
    current = ""
    for word in words:
        candidate = (current + " " + word).strip()
        box = dummy_draw.textbbox((0, 0), candidate, font=desc_font)
        if box[2] - box[0] > 250:
            if current:
                lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)

    ILLUS_W, ILLUS_H = 180, 180
    ILLUS_X, ILLUS_Y = 25, 32

    for f in range(num_frames):
        image = Image.new("RGBA", (VISUAL_W, VISUAL_H), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((4, 4, VISUAL_W - 4, VISUAL_H - 4), radius=28, fill=(12, 18, 35, 255), outline=(255, 215, 0, 255), width=4)

        progress = math.sin(math.pi * (f / num_frames))
        bob_y = int(5 * math.sin(2 * math.pi * f / num_frames))

        if real_img:
            scale = 1.0 + 0.18 * progress
            sz = int(ILLUS_W * scale)
            scaled = real_img.resize((sz, sz), Image.LANCZOS)
            left = (sz - ILLUS_W) // 2
            top = (sz - ILLUS_H) // 2
            cropped = scaled.crop((left, top, left + ILLUS_W, top + ILLUS_H))
            mask = Image.new("L", (ILLUS_W, ILLUS_H), 0)
            ImageDraw.Draw(mask).rounded_rectangle((0, 0, ILLUS_W, ILLUS_H), radius=18, fill=255)
            image.paste(cropped, (ILLUS_X, ILLUS_Y + bob_y), mask)
        else:
            # fallback simple icon if download fails
            draw.ellipse((85, 35 + bob_y, 175, 125 + bob_y), fill=(235, 190, 150, 255))
            draw.rectangle((55, 115, 205, 205), fill=(115, 80, 50, 255))

        draw.text((230, 48), label, fill="white", font=label_font)
        for line_index, line in enumerate(lines[:5]):
            draw.text((230, 95 + line_index * 27), line, fill=(215, 220, 235, 255), font=desc_font)

        frames.append(image)

    frames[0].save(filename, format='GIF', save_all=True, append_images=frames[1:], duration=33, loop=0, disposal=2)
    return filename


# ============================================================
# BACKGROUND
# ============================================================

def create_background(position, glow_color, filename):
    source = os.path.join(os.path.dirname(os.path.abspath(__file__)), "background.png")
    if os.path.exists(source):
        try:
            image = Image.open(source).convert("RGB").resize((VIDEO_W, VIDEO_H))
        except Exception:
            image = Image.new("RGB", (VIDEO_W, VIDEO_H), (12, 16, 32))
    else:
        image = Image.new("RGB", (VIDEO_W, VIDEO_H), (12, 16, 32))
        draw = ImageDraw.Draw(image)
        for x in range(0, VIDEO_W, 60):
            draw.line([(x, 0), (x, VIDEO_H)], fill=(20, 26, 45), width=2)
        for y in range(0, VIDEO_H, 60):
            draw.line([(0, y), (VIDEO_W, y)], fill=(20, 26, 45), width=2)

    overlay = Image.new("RGBA", (VIDEO_W, VIDEO_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    if position == "left":
        cx = 400
    elif position == "right":
        cx = 1520
    else:
        cx = 960
    for radius in range(700, 50, -50):
        alpha = int(15 * (1 - radius / 700))
        draw.ellipse([cx - radius, 540 - radius, cx + radius, 540 + radius], fill=hex_to_rgba(glow_color, alpha))
    overlay = overlay.filter(ImageFilter.GaussianBlur(30))
    result = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")
    result.save(filename)


# ============================================================
# SPEAKER CARD
# ============================================================

def create_ui_overlay(speaker_name, topic, position, glow_color, filename):
    image = Image.new("RGBA", (VIDEO_W, VIDEO_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    title_font = load_font(30, bold=True)
    name_font = load_font(30, bold=True)
    title = f"TOPIC: {topic}"
    box = draw.textbbox((0, 0), title, font=title_font)
    width = box[2] - box[0]
    draw.text(((VIDEO_W - width) // 2, 24), title, fill="white", font=title_font)
    card_width = 650
    card_height = 110
    card_y = 885
    if position == "left":
        card_x = 75
    elif position == "right":
        card_x = 1195
    else:
        card_x = (VIDEO_W - card_width) // 2
    draw.rounded_rectangle([card_x, card_y, card_x + card_width, card_y + card_height], radius=18, fill=(18, 26, 46, 235), outline=glow_color, width=4)
    draw.ellipse([card_x + 22, card_y + 27, card_x + 47, card_y + 52], fill=glow_color)
    draw.text((card_x + 65, card_y + 22), speaker_name, fill="white", font=name_font)
    image.save(filename)
    return card_x, card_y


# ============================================================
# FFMPEG PATH
# ============================================================

def ffmpeg_filter_path(filename):
    path = os.path.abspath(filename)
    path = path.replace("\\", "/")
    path = path.replace("'", r"\'")
    path = path.replace(":", r"\:")
    return path


# ============================================================
# VIDEO SEGMENT
# ============================================================

def render_video_segment(background, ui, audio, subtitles, output, position, glow_color, card_x, card_y, visual_plan):
    required = [background, ui, audio, subtitles]
    for path in required:
        if not os.path.exists(path):
            raise FileNotFoundError(f"Required file missing: {os.path.abspath(path)}")

    visual_assets = []
    for index, visual in enumerate(visual_plan or []):
        try:
            asset = create_visual_asset(visual, index)
            visual_assets.append((asset, visual))
        except Exception as exc:
            print(f"⚠️ Visual creation skipped: {str(exc)[:100]}")

    glow = glow_color.lstrip("#")
    if position == "left":
        pan_x = "0"
    elif position == "right":
        pan_x = "iw-(iw/zoom)"
    else:
        pan_x = "(iw-(iw/zoom))/2"

    filter_parts = []
    filter_parts.append("[0:v]scale=1920:1080,zoompan=z='min(zoom+0.00020,1.05)':x='" + pan_x + "':y='(ih-(ih/zoom))/2':d=9000:s=1920x1080:fps=30[bg];")
    filter_parts.append("[1:v]scale=1920:1080[ui];")
    # FIXED: Sound bars inside name cards, not overlapping names - placed below name text inside card
    # Card is 650x110, name at card_x+65, card_y+22. Wave goes at card_y+62 inside card bottom half
    wave_width = 560  # Inside card: card_width 650 minus padding 65+25
    wave_height = 32
    filter_parts.append(f"[2:a]showwaves=s={wave_width}x{wave_height}:mode=cline:colors=0x{glow}:rate=30:draw=full[wave];")
    filter_parts.append("[bg][ui]overlay=0:0[base];")

    # FIXED: Sound bar inside name card, below speaker name, not overlapping
    # Name text is at card_y+22, so wave at card_y+60 sits in lower half of 110px card
    wave_x = card_x + 65  # Aligned with name text start, inside card
    wave_y = card_y + 62  # Below name, inside card (name at 22, wave at 62 = 40px below)
    filter_parts.append(f"[base][wave]overlay={wave_x}:{wave_y}[withwave];")

    current = "[withwave]"
    input_index = 3

    for index, (asset, visual) in enumerate(visual_assets):
        label = f"visual{index}"
        start = max(0.0, float(visual["start"]))
        end = max(start + 2.0, float(visual["end"]))
        filter_parts.append(f"[{input_index}:v]format=rgba,fade=t=in:st={start}:d=0.4:alpha=1,fade=t=out:st={end-0.4}:d=0.4:alpha=1[{label}_faded];")
        x = (VIDEO_W - VISUAL_W) // 2
        drift_speed = 15
        y_expr = f"{VISUAL_Y} + 20 - (t-{start})*{drift_speed}"
        enable = f"between(t,{start:.2f},{end:.2f})"
        filter_parts.append(f"{current}[{label}_faded]overlay={x}:'{y_expr}':enable='{enable}'[v{index}];")
        current = f"[v{index}]"
        input_index += 1

    subtitle_path = ffmpeg_filter_path(subtitles)
    filter_parts.append(f"{current}ass='{subtitle_path}'[outv]")
    filter_complex = "".join(filter_parts)

    command = ["ffmpeg", "-y", "-loop", "1", "-framerate", str(FPS), "-i", background, "-i", ui, "-i", audio]
    for asset, _ in visual_assets:
        if asset.endswith(".gif"):
            command += ["-ignore_loop", "0", "-i", asset]
        else:
            command += ["-loop", "1", "-i", asset]

    command += ["-filter_complex", filter_complex, "-map", "[outv]", "-map", "2:a", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-shortest", output]

    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        print("\n❌ FFmpeg failed:")
        print(result.stderr[-7000:])
        raise RuntimeError(f"FFmpeg failed creating {output}")

    for asset, _ in visual_assets:
        try:
            os.remove(asset)
        except Exception:
            pass


# ============================================================
# SCORECARD IMAGE
# ============================================================

def generate_scoreboard(round_num, results, round_a, round_b, cumulative_a, cumulative_b, filename):
    source = os.path.join(os.path.dirname(os.path.abspath(__file__)), "background.png")
    if os.path.exists(source):
        try:
            image = Image.open(source).convert("RGB").resize((VIDEO_W, VIDEO_H))
        except Exception:
            image = Image.new("RGB", (VIDEO_W, VIDEO_H), (12, 16, 32))
    else:
        image = Image.new("RGB", (VIDEO_W, VIDEO_H), (12, 16, 32))
    overlay = Image.new("RGBA", (VIDEO_W, VIDEO_H), (0, 0, 0, 235))
    image = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(image)
    header = load_font(38, bold=True)
    sub = load_font(22, bold=True)
    small = load_font(20)
    def centred(y, text, font, fill):
        box = draw.textbbox((0, 0), text, font=font)
        width = box[2] - box[0]
        draw.text(((VIDEO_W - width) // 2, y), text, fill=fill, font=font)
    judge_count = len(results)
    centred(24, f"ROUND {round_num} — AI JUDGING PANEL", header, "#FFD700")
    centred(72, f"{judge_count} INDEPENDENT JUDGES", sub, "white")
    centred(112, f"ROUND SCORE   APOLOGIST {round_a:.1f}   VS   SKEPTIC {round_b:.1f}", sub, "white")
    centred(150, f"CUMULATIVE   APOLOGIST {cumulative_a:.1f}   VS   SKEPTIC {cumulative_b:.1f}", sub, "#FFD700")
    draw.text((100, 225), "CATEGORY AVERAGES", fill="#FFD700", font=sub)
    draw.text((500, 265), "APOLOGIST", fill="#00FFCC", font=small)
    draw.text((680, 265), "SKEPTIC", fill="#FF66FF", font=small)
    categories = [("Argument strength", "A_argument", "B_argument"), ("Rebuttal quality", "A_rebuttal", "B_rebuttal"), ("Clarity & reasoning", "A_clarity", "B_clarity")]
    y = 310
    for label, a_key, b_key in categories:
        a = sum(r[a_key] for r in results) / judge_count
        b = sum(r[b_key] for r in results) / judge_count
        draw.text((100, y), label, fill="white", font=small)
        draw.text((500, y), f"{a:.1f}", fill="#00FFCC", font=small)
        draw.text((680, y), f"{b:.1f}", fill="#FF66FF", font=small)
        y += 48
    draw.text((980, 225), "INDIVIDUAL JUDGES", fill="#FFD700", font=sub)
    draw.text((980, 270), "PROVIDER", fill="white", font=small)
    draw.text((1500, 270), "A", fill="#00FFCC", font=small)
    draw.text((1580, 270), "B", fill="#FF66FF", font=small)
    draw.line([(970, 300), (1680, 300)], fill=(100, 110, 140, 255), width=2)
    row_height = 48
    start_y = 320
    for index, result in enumerate(results):
        row_y = start_y + index * row_height
        provider = result.get("provider", "Unknown")
        if len(provider) > 28:
            provider = provider[:25] + "..."
        draw.text((980, row_y), provider, fill="white", font=small)
        draw.text((1500, row_y), f"{result['A_total']:.1f}", fill="#00FFCC", font=small)
        draw.text((1580, row_y), f"{result['B_total']:.1f}", fill="#FF66FF", font=small)
    image.save(filename)


# ============================================================
# SCORECARD VIDEO
# ============================================================

def render_scorecard_video(scorecard, audio, subtitles, output):
    for path in [scorecard, audio, subtitles]:
        if not os.path.exists(path):
            raise FileNotFoundError(f"Scorecard file missing: {os.path.abspath(path)}")
    subtitle_path = ffmpeg_filter_path(subtitles)
    filter_complex = f"[0:v]scale=1920:1080[base];[base]ass='{subtitle_path}'[outv]"
    command = ["ffmpeg", "-y", "-loop", "1", "-framerate", str(FPS), "-i", scorecard, "-i", audio, "-filter_complex", filter_complex, "-map", "[outv]", "-map", "1:a", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-shortest", output]
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        print("\n❌ Scorecard FFmpeg failed:")
        print(result.stderr[-7000:])
        raise RuntimeError("Scorecard rendering failed.")


# ============================================================
# SEGMENT CREATION
# ============================================================

def create_segment(text, role, speaker_name, topic, segment_id, model_for_visuals, position=None, glow=None, judge_voice_index=None):
    if position is None:
        if role == "AI Christian Apologist":
            position = "left"
        elif role == "AI Skeptic":
            position = "right"
        else:
            position = "center"
    if glow is None:
        if role == "AI Christian Apologist":
            glow = "#00FFCC"
        elif role == "AI Skeptic":
            glow = "#FF00FF"
        elif role == "AI Judge":
            glow = "#3399FF"
        else:
            glow = "#FFD700"

    audio_file = f"audio_{segment_id}.mp3"
    subtitle_file = f"subs_{segment_id}.ass"
    background_file = f"bg_{segment_id}.png"
    ui_file = f"ui_{segment_id}.png"
    video_file = f"segment_{segment_id}.mp4"

    words = generate_audio(text, role, audio_file, judge_voice_index)
    generate_subtitles(words, subtitle_file)

    visual_plan = []
    try:
        visual_plan = create_visual_plan(clean_for_speech(text), words, model_for_visuals)
        if visual_plan:
            print(f"   🎨 {len(visual_plan)} adaptive visual cue(s)")
    except Exception as exc:
        print(f"⚠️ Visual planning skipped: {str(exc)[:120]}")

    create_background(position, glow, background_file)
    card_x, card_y = create_ui_overlay(speaker_name, topic, position, glow, ui_file)
    render_video_segment(background_file, ui_file, audio_file, subtitle_file, video_file, position, glow, card_x, card_y, visual_plan)
    return video_file


# ============================================================
# PANEL COMMENTARY
# ============================================================

def generate_panel_commentary(model, side, topic, round_num, apologist, skeptic, previous_comments):
    provider = provider_from_model(model)
    preferred_side = "AI Christian Apologist" if side == "A" else "AI Skeptic"
    recent = "\n".join(previous_comments[-6:])
    def trim_for_prompt(text, max_words=220):
        words_list = text.split()
        if len(words_list) <= max_words:
            return text
        return " ".join(words_list[-max_words:])
    apologist_excerpt = trim_for_prompt(apologist)
    skeptic_excerpt = trim_for_prompt(skeptic)
    prompt = f"""
You are an independent AI debate judge.
Your provider is {provider}.
Topic: {topic}
Round: {round_num}
What the AI Christian Apologist argued this round:
{apologist_excerpt}
What the AI Skeptic argued this round:
{skeptic_excerpt}
You preferred: {preferred_side}
Give a short, specific, insightful observation about the quality of reasoning you just read above - refer to an actual argument or move either side made.
Do not simply say which side was convincing.
Do not summarise the whole debate.
Do not quote either debater word-for-word.
Do not mention your model ID.
Do not mention that you are an AI.
Previous observations:
{recent}
Write 2 or 3 natural spoken sentences.
"""
    response = query_openrouter(prompt, model, timeout=40, max_tokens=220, temperature=0.85)
    if response:
        return response
    return "The important distinction is between a conclusion that sounds plausible and an argument that has actually answered the strongest objection."


# ============================================================
# INTRO / OUTRO
# ============================================================

def build_intro(topic, judge_count):
    return f"Welcome to the AI Debate Arena. Today, an AI Christian Apologist faces an AI Skeptic on the question: {topic}. The debate will unfold over three rounds with equal speaking time for both sides. An independent panel of {judge_count} AI systems will score argument strength, rebuttal quality, and clarity of reasoning. Let's begin."

def build_outro(judge_count, cumulative_a, cumulative_b):
    if math.isclose(cumulative_a, cumulative_b, abs_tol=0.01):
        result = "a draw"
    elif cumulative_a > cumulative_b:
        result = "the AI Christian Apologist"
    else:
        result = "the AI Skeptic"
    return f"After three rounds, our panel of {judge_count} AI judges gave the AI Christian Apologist a cumulative score of {cumulative_a:.1f}, compared with {cumulative_b:.1f} for the AI Skeptic. The final result is {result}. But the final verdict is still yours. Which side do you think actually won?"


# ============================================================
# CONCATENATION
# ============================================================

def stitch_segments(segments, output):
    list_file = "concat_list.txt"
    with open(list_file, "w", encoding="utf-8") as file:
        for segment in segments:
            path = os.path.abspath(segment)
            path = path.replace("'", "'\\''")
            file.write(f"file '{path}'\n")
    print("🎬 Stitching final video...")
    command = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_file, "-c", "copy", output]
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        print(result.stderr[-7000:])
        raise RuntimeError("Final FFmpeg concatenation failed.")


# ============================================================
# MAIN PIPELINE
# ============================================================

def run_debate_pipeline():
    cleanup_cache()
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY environment variable is missing.")

    if not os.path.exists("topic.txt"):
        with open("topic.txt", "w", encoding="utf-8") as file:
            file.write("Does the universe require a creator?")

    with open("topic.txt", "r", encoding="utf-8") as file:
        topic = file.read().strip()
    if not topic:
        topic = "Does the universe require a creator?"

    print("\n" + "="*70 + "\nAI DEBATE ARENA\n" + "="*70 + f"\n\nTOPIC: {topic}\n")

    available_models = discover_models()
    if not available_models:
        print("⚠️ Dynamic discovery failed. Using fallback models.")
        available_models = FALLBACK_MODELS.copy()

    apologist_model, skeptic_model = choose_primary_models(available_models)
    print("🎤 Debate engines:")
    print(f"   Apologist: {provider_from_model(apologist_model)}")
    print(f"   Skeptic: {provider_from_model(skeptic_model)}")

    judges = choose_judges(available_models, (apologist_model, skeptic_model))
    if not judges:
        used = set()
        judges = []
        for model in FALLBACK_MODELS:
            provider = provider_from_model(model)
            if provider in used:
                continue
            if model in (apologist_model, skeptic_model):
                continue
            judges.append(model)
            used.add(provider)
            if len(judges) >= MAX_JUDGES:
                break

    print(f"\n⚖️ Maximum judges: {MAX_JUDGES}")
    print(f"⚖️ Actual judges: {len(judges)}")
    print("⚖️ ONE MODEL PER PROVIDER:")
    for model in judges:
        print(f"   • {provider_from_model(model)} — {model.split('/', 1)[-1][:28]}")

    segments = []
    segment_id = 0

    def add_segment(text, role, name, position=None, glow=None, judge_voice_index=None):
        nonlocal segment_id
        visuals_model = skeptic_model if role == "AI Skeptic" else apologist_model
        video = create_segment(text, role, name, topic, segment_id, visuals_model, position, glow, judge_voice_index)
        segments.append(video)
        segment_id += 1

    add_segment(build_intro(topic, len(judges)), "Moderator", "MODERATOR")

    previous_history = ""
    cumulative_a = 0.0
    cumulative_b = 0.0
    panel_comments = []

    for round_num in range(1, ROUNDS + 1):
        print("\n" + "="*70 + f"\nROUND {round_num}\n" + "="*70)
        apologist_turns, skeptic_turns, previous_history = build_round_exchanges(topic, round_num, apologist_model, skeptic_model, previous_history)

        for turn_index in range(TURNS_PER_SIDE_PER_ROUND):
            apologist_text = apologist_turns[turn_index]
            skeptic_text = skeptic_turns[turn_index]
            print(f"   Exchange {turn_index+1}: A={count_words(apologist_text)} words | B={count_words(skeptic_text)} words")
            add_segment(apologist_text, "AI Christian Apologist", "AI CHRISTIAN APOLOGIST", "left", "#00FFCC")
            add_segment(skeptic_text, "AI Skeptic", "AI SKEPTIC", "right", "#FF00FF")

        apologist_full = "\n".join(apologist_turns)
        skeptic_full = "\n".join(skeptic_turns)
        print(f"   Round total: A={count_words(apologist_full)} words | B={count_words(skeptic_full)} words")

        results = evaluate_round(judges, topic, round_num, apologist_full, skeptic_full)
        round_a, round_b = calculate_round_average(results)
        cumulative_a += round_a
        cumulative_b += round_b
        print(f"📊 Round {round_num}: A {round_a:.1f} vs B {round_b:.1f}")
        print(f"📊 Cumulative: A {cumulative_a:.1f} vs B {cumulative_b:.1f}")

        scoreboard_file = f"scoreboard_r{round_num}.png"
        generate_scoreboard(round_num, results, round_a, round_b, cumulative_a, cumulative_b, scoreboard_file)
        score_text = f"Round {round_num} is complete. The {len(results)} independent AI judges gave the AI Christian Apologist an average score of {round_a:.1f}, and the AI Skeptic an average score of {round_b:.1f}. The cumulative score is {cumulative_a:.1f} to {cumulative_b:.1f}."
        score_audio = f"score_audio_r{round_num}.mp3"
        score_subs = f"score_subs_r{round_num}.ass"
        score_video = f"score_video_r{round_num}.mp4"
        score_words = generate_audio(score_text, "Moderator", score_audio)
        generate_subtitles(score_words, score_subs, scorecard=True)
        render_scorecard_video(scoreboard_file, score_audio, score_subs, score_video)
        segments.append(score_video)

        if results:
            # FIXED: Each commenting AI gets its own distinct voice - map provider to unique voice index
            # Create provider -> voice index mapping from judges list
            provider_to_voice_idx = {}
            for idx, j in enumerate(judges):
                prov = j.get("provider", f"Judge{idx}")
                if prov not in provider_to_voice_idx:
                    provider_to_voice_idx[prov] = idx % len(JUDGE_VOICES)
            
            a_results = [r for r in results if r["winner"] == "A"]
            b_results = [r for r in results if r["winner"] == "B"]
            if not a_results: a_results = results
            if not b_results: b_results = results
            
            # Ensure we pick two judges with different providers/voices
            judge_a = random.choice(a_results)
            # Pick judge_b with different provider if possible
            b_candidates = [r for r in b_results if r["provider"] != judge_a["provider"]]
            if not b_candidates:
                b_candidates = b_results
            judge_b = random.choice(b_candidates)
            
            voice_idx_a = provider_to_voice_idx.get(judge_a["provider"], 0)
            voice_idx_b = provider_to_voice_idx.get(judge_b["provider"], 1)
            # Ensure different voices
            if voice_idx_a == voice_idx_b:
                voice_idx_b = (voice_idx_a + 1) % len(JUDGE_VOICES)
            
            comment_a = generate_panel_commentary(judge_a["model"], "A", topic, round_num, apologist_full, skeptic_full, panel_comments)
            panel_comments.append(comment_a)
            add_segment(comment_a, "AI Judge", "AI JUDGE — " + judge_a["provider"].upper(), "center", "#3399FF", judge_voice_index=voice_idx_a)
            comment_b = generate_panel_commentary(judge_b["model"], "B", topic, round_num, apologist_full, skeptic_full, panel_comments)
            panel_comments.append(comment_b)
            add_segment(comment_b, "AI Judge", "AI JUDGE — " + judge_b["provider"].upper(), "center", "#3399FF", judge_voice_index=voice_idx_b)

    add_segment(build_outro(len(judges), cumulative_a, cumulative_b), "Moderator", "MODERATOR")
    stitch_segments(segments, OUTPUT_FILE)

    print("\n" + "="*70 + "\n✅ DEBATE COMPLETE\n" + "="*70)
    print(f"🎥 Output: {OUTPUT_FILE}")
    print(f"⚖️ AI judges: {len(judges)}")
    print(f"🏆 Final score: Apologist {cumulative_a:.1f} vs Skeptic {cumulative_b:.1f}")
    cleanup_cache()


if __name__ == "__main__":
    try:
        run_debate_pipeline()
    except KeyboardInterrupt:
        print("\n⛔ Pipeline cancelled.")
    except Exception as exc:
        print("\n❌ PIPELINE FAILED")
        print(str(exc))
        raise
