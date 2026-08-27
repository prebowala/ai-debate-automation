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

# === UNIQUE VOICE CAST - UPGRADED FROM OLDER BUILD - NO DUPLICATES ===
VOICE_POOL = [
    "en-US-BrianMultilingualNeural",
    "en-GB-SoniaNeural",
    "en-AU-NatashaNeural",
    "en-US-JennyNeural",
    "en-GB-RyanNeural",
    "en-US-GuyNeural",
    "en-GB-LibbyNeural",
    "en-US-DavisNeural",
    "en-AU-WilliamNeural",
    "en-CA-ClaraNeural",
    "en-US-AriaNeural",
    "en-US-AndrewMultilingualNeural",
]

VOICES = {
    "Moderator": "en-AU-NatashaNeural",
    "AI Christian Apologist": "en-US-BrianMultilingualNeural",
    "AI Skeptic": "en-GB-SoniaNeural",
    "AI Judge 1": "en-US-JennyNeural",
    "AI Judge 2": "en-GB-RyanNeural",
    "AI Judge 3": "en-US-GuyNeural",
    "AI Judge 4": "en-GB-LibbyNeural",
}

JUDGE_VOICES = [
    "en-US-JennyNeural",
    "en-GB-RyanNeural",
    "en-US-GuyNeural",
    "en-GB-LibbyNeural",
    "en-US-DavisNeural",
    "en-AU-WilliamNeural",
    "en-CA-ClaraNeural",
]

CAST_VOICE_ASSIGNMENT = {}
JUDGE_VOICE_MAP = {}

def assign_unique_voices():
    global CAST_VOICE_ASSIGNMENT, JUDGE_VOICE_MAP
    CAST_VOICE_ASSIGNMENT = {
        "AI Christian Apologist": VOICE_POOL[0],
        "AI Skeptic": VOICE_POOL[1],
        "Moderator": VOICE_POOL[2],
    }
    JUDGE_VOICE_MAP = {}
    print(f"Voice cast: Apologist={VOICE_POOL[0]}, Skeptic={VOICE_POOL[1]}, Moderator={VOICE_POOL[2]}")
    return CAST_VOICE_ASSIGNMENT



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

# Global memory to prevent verbal loops
USED_OPENERS = set()
USED_ARGUMENTS = set()

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

    # Extract opponent last argument
    opponent_last = ""
    if previous_exchange:
        # Get last 600 chars of opponent
        opponent_last = previous_exchange[-800:]

    # Banned generic loop phrases
    banned_openers = [
        "I hear you about the problem of suffering",
        "I hear you on the suffering point",
        "You make a fair point about suffering",
        "I understand the concern about suffering",
        "Suffering is a powerful concern",
        "I hear you about",
        "You raise an important point about suffering",
    ]

    if round_num == 1 and turn_num == 1:
        instruction = f"""This is the opening exchange. Establish a strong foundation without trying to deliver the entire debate at once.
- Do NOT say "I hear you" - there is no opponent yet.
- Give 2-3 specific reasons for {side_short}.
- Be concrete, not generic."""
    else:
        # For all rebuttals - force deep engagement, not loop
        instruction = f"""This is turn {turn_num} of round {round_num} - DEEP REBUTTAL REQUIRED.

Opponent's last argument (you must dismantle this, not just mention it):
"{opponent_last[:500]}"

RULES TO AVOID VERBAL LOOP:
- BANNED OPENERS (do NOT use): {', '.join(banned_openers)}
- Do NOT start with "I hear you about the problem of suffering" or any variant. That phrase is banned.
- Do NOT start with "I hear you on" or "You make a fair point about" - too generic and repetitive.
- Instead start with specific reasoning: "The problem with that suffering argument is...", "That would work if suffering were gratuitous, but...", "What that misses about fine-tuning is..."

YOUR TASK:
1. In ONE sentence, state opponent's core claim in your own words (not quote).
2. In 3-4 sentences, show why that claim fails or is incomplete FROM YOUR SIDE with direct counter-evidence. This must target their claim, not unrelated topic.
3. Add ONE fresh point FOR your side you haven't used.

- Do NOT recycle previous arguments. Check: {'; '.join(list(USED_ARGUMENTS)[-5:])}
- Fresh angle required, not same line as before.
- Do not say "in this round" or "as an AI"."""

    prompt = f"""
You are the {side_name} in a serious public debate arguing {side_short}.
Topic: {topic}
Opponent: {opponent} arguing {opp_short}
{instruction}
Previous exchange (for context, do NOT repeat verbatim):
{previous_exchange[-1000:] if previous_exchange else "None - opening exchange."}

Write ONLY your spoken contribution.
Target approximately {WORDS_PER_TURN} words.
Aim for {MIN_TURN_WORDS}-{MAX_TURN_WORDS} words.
Use natural conversational speech. Suitable for a general YouTube audience.
Be specific. Use examples or analogies when useful.
Start with substantive reasoning, NOT with generic acknowledgement.
No headings. No numbered lists. No bullet points. No meta commentary. Do not mention AI models. Do not mention companies.
No banned openers.
"""
    for attempt in range(3):
        temp = 0.78 + (attempt * 0.1) + random.uniform(0, 0.1)
        response = query_openrouter(prompt, model, max_tokens=430, temperature=temp)
        if not response:
            continue
        low = response.lower()
        # Check for banned loop phrase
        has_banned = any(b.lower() in low for b in banned_openers)
        # Check if repeats opening
        is_repeat = any(len(arg)>30 and arg.lower() in low for arg in USED_ARGUMENTS)
        if has_banned:
            # Retry with stricter instruction
            prompt_retry = f"""Your last response used a banned generic opener that causes verbal loops. Banned: {banned_openers}. Opponent said: {opponent_last[:300]}. You must start with specific counter-reasoning, not "I hear you about suffering". Example good start: "The problem with that is suffering actually presupposes objective good..." or "Fine-tuning doesn't force a designer because...". Rewrite: {response[:400]}"""
            response2 = query_openrouter(prompt_retry, model, max_tokens=430, temperature=0.85)
            if response2 and not any(b.lower() in response2.lower() for b in banned_openers):
                response = response2
                low = response.lower()
                is_repeat = any(len(arg)>30 and arg.lower() in low for arg in USED_ARGUMENTS)
        if not is_repeat or attempt==2:
            # Track used
            for sent in response.split('. ')[:2]:
                if len(sent)>25:
                    USED_ARGUMENTS.add(sent[:70])
                    USED_OPENERS.add(sent[:40].lower())
            return response
    return "The important question is not simply whether a conclusion sounds plausible. We need to ask whether the evidence actually supports it, what assumptions are being made, and whether alternative explanations have been properly considered."


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
    prompt = f"""
You are an independent and impartial debate judge evaluating round {round_num} on: {topic}
SIDE A — FOR (Christian Apologist):
{apologist[:1200]}
SIDE B — AGAINST (Skeptic):
{skeptic[:1200]}

Score critically:
1. Argument strength - specific evidence vs vague claims?
2. Rebuttal quality - did they actually dismantle opponent's last point with direct counter-reasoning, or just do generic segue like "I hear you about suffering, but..."? Generic segue = low score (0-30). Deep direct counter = high score (70-100).
3. Clarity and reasoning - is it a summarised clash or just listing what was said?

Penalize verbal loops and generic openers.
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

async def generate_audio_async(text, voice, filename):
    communicate = edge_tts.Communicate(text, voice, rate="+0%", volume="+0%")
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
    if role == "AI Judge":
        if judge_voice_index is None:
            judge_voice_index = 0
        voice = JUDGE_VOICES[judge_voice_index % len(JUDGE_VOICES)]
    else:
        voice = VOICES.get(role, VOICES["Moderator"])
    clean_text = clean_for_speech(text)
    try:
        return asyncio.run(generate_audio_async(clean_text, voice, filename))
    except Exception as exc:
        print(f"⚠️ TTS failed using {voice}: {str(exc)[:150]}")
        return asyncio.run(generate_audio_async(clean_text, VOICES["Moderator"], filename))


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
    # CLEAN ARTWORK - IMAGE ONLY, NO DESCRIPTION TEXT (user request)
    filename = f"visual_{index}.gif"
    frames = []
    num_frames = 30

    real_img = fetch_topic_image(visual)

    # Larger image area - no text to compete with
    ILLUS_W, ILLUS_H = 320, 240
    ILLUS_X, ILLUS_Y = (VISUAL_W - ILLUS_W)//2, (VISUAL_H - ILLUS_H)//2

    for f in range(num_frames):
        image = Image.new("RGBA", (VISUAL_W, VISUAL_H), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        # Clean border - no label/description inside
        draw.rounded_rectangle((4, 4, VISUAL_W - 4, VISUAL_H - 4), radius=22, fill=(12, 18, 35, 220), outline=(255, 215, 0, 200), width=3)

        progress = math.sin(math.pi * (f / num_frames))
        bob_y = int(4 * math.sin(2 * math.pi * f / num_frames))

        if real_img:
            scale = 1.0 + 0.15 * progress
            sz_w = int(ILLUS_W * scale)
            sz_h = int(ILLUS_H * scale)
            scaled = real_img.resize((sz_w, sz_h), Image.LANCZOS)
            left = (sz_w - ILLUS_W) // 2
            top = (sz_h - ILLUS_H) // 2
            cropped = scaled.crop((left, top, left + ILLUS_W, top + ILLUS_H))
            mask = Image.new("L", (ILLUS_W, ILLUS_H), 0)
            ImageDraw.Draw(mask).rounded_rectangle((0, 0, ILLUS_W, ILLUS_H), radius=16, fill=255)
            image.paste(cropped, (ILLUS_X, ILLUS_Y + bob_y), mask)
        else:
            draw.ellipse((ILLUS_X+40, ILLUS_Y+20 + bob_y, ILLUS_X+140, ILLUS_Y+120 + bob_y), fill=(235, 190, 150, 255))
            draw.rectangle((ILLUS_X+10, ILLUS_Y+110, ILLUS_X+200, ILLUS_Y+200), fill=(115, 80, 50, 255))

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
    filter_parts.append("[2:a]showwaves=s=300x58:mode=cline:colors=0x" + glow + ":rate=30[wave];")
    filter_parts.append("[bg][ui]overlay=0:0[base];")

    wave_x = card_x + 330
    wave_y = card_y + 47
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
    if side == "A":
        pref_spoken = "the case for"
        other_spoken = "the case against"
    else:
        pref_spoken = "the case against"
        other_spoken = "the case for"

    recent = "\n".join(previous_comments[-6:])
    def trim_for_prompt(txt, max_words=200):
        wl = txt.split()
        return txt if len(wl) <= max_words else " ".join(wl[-max_words:])

    def extract_core(txt):
        low=txt.lower()
        if "evil" in low or "suffer" in low:
            return "suffering and whether it undercuts God"
        if "hidden" in low:
            return "why God isn't more obvious"
        if "fine tuning" in low or "tuned" in low:
            return "fine-tuning of constants"
        if "cause" in low or "began" in low:
            return "whether universe needs cause"
        if "moral" in low:
            return "objective moral values"
        return "central disagreement this round"

    ap_core = extract_core(apologist)
    sk_core = extract_core(skeptic)

    prompt = f"""
You are {provider}, independent AI debate judge for round {round_num} on: {topic}
FOR: {trim_for_prompt(apologist)}
AGAINST: {trim_for_prompt(skeptic)}
You leaned {pref_spoken} this round.

TASK - Summarise, don't list:
- In ONE sentence, summarise central clash: {ap_core} vs {sk_core}
- In ONE sentence, explain why {pref_spoken} handled that clash better - specific reasoning quality
- Do NOT list "for said X, against said Y". Synthesise.
- Do NOT use banned phrases: "raises an important point", "makes a fair point", "I hear you"
- Use short names: "{pref_spoken}" and "{other_spoken}"
- 2 sentences total, natural spoken, insightful.

Previous (avoid repeat):
{recent}
"""
    for attempt in range(2):
        resp = query_openrouter(prompt, model, timeout=40, max_tokens=220, temperature=0.85 if attempt==0 else 0.9)
        if resp and count_words(resp)>=12:
            low=resp.lower()
            is_listing = "for said" in low or "against said" in low or "apologist said" in low
            if is_listing:
                retry = f"Summarise clash, don't list. Round {round_num}: clash is {ap_core} vs {sk_core}. You leaned {pref_spoken}. Why did {pref_spoken} handle it better? 2 sentences: {resp[:300]}"
                r2 = query_openrouter(retry, model, timeout=40, max_tokens=220, temperature=0.88)
                if r2:
                    resp=r2
            return resp
    return f"Round {round_num} came down to {ap_core} versus {sk_core}. For me, {pref_spoken} edged it because it directly answered the other side's strongest push instead of just adding a new point."



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
            a_results = [r for r in results if r["winner"] == "A"]
            b_results = [r for r in results if r["winner"] == "B"]
            if not a_results: a_results = results
            if not b_results: b_results = results
            judge_a = random.choice(a_results)
            judge_b = random.choice(b_results)
            comment_a = generate_panel_commentary(judge_a["model"], "A", topic, round_num, apologist_full, skeptic_full, panel_comments)
            panel_comments.append(comment_a)
            add_segment(comment_a, "AI Judge", "AI JUDGE — " + judge_a["provider"].upper(), "center", "#3399FF", judge_voice_index=0)
            comment_b = generate_panel_commentary(judge_b["model"], "B", topic, round_num, apologist_full, skeptic_full, panel_comments)
            panel_comments.append(comment_b)
            add_segment(comment_b, "AI Judge", "AI JUDGE — " + judge_b["provider"].upper(), "center", "#3399FF", judge_voice_index=1)

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
