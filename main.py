import os
import sys
import asyncio
import requests
import subprocess
import re
import math
import concurrent.futures
import json
import random
import glob
import time
import html
from statistics import mean
from pathlib import Path

import edge_tts
from PIL import Image, ImageDraw, ImageFont, ImageFilter


# ============================================================
# AI DEBATE ARENA
# Future-proof multi-model Christian Apologist vs AI Skeptic
#
# Major features:
#   - Dynamic OpenRouter model discovery
#   - Up to 100 judges
#   - 3 judging categories
#   - Automatic removal of unavailable/duplicate models
#   - Long-form skeptic rebuttal with validation + continuation
#   - Multi-round contextual debate
#   - No model names advertised as debaters
#   - Dynamic judge count throughout narration/graphics
#   - Paragraph-style subtitles
#   - Word highlighting inside the paragraph
#   - Better subtitle positioning
#   - Dynamic speaker waveform
#   - No card/name overlap
#   - Robust TTS fallbacks
#   - FFmpeg failure detection
#   - Automatic retries
# ============================================================


# ============================================================
# CONFIGURATION
# ============================================================

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
OPENROUTER_CHAT_URL = f"{OPENROUTER_BASE}/chat/completions"
OPENROUTER_MODELS_URL = f"{OPENROUTER_BASE}/models"

MAX_JUDGES = 100

# How many judges must successfully answer before we accept a round.
MIN_JUDGES = 5

# Parallel requests. 20 is a good compromise for a large panel.
JUDGE_WORKERS = 20

# Model-generation retries.
MODEL_RETRIES = 3

# Debate length.
APOLOGIST_MIN_WORDS = 260
APOLOGIST_TARGET_WORDS = 400

SKEPTIC_MIN_WORDS = 500
SKEPTIC_TARGET_WORDS = 650

COMMENTARY_MIN_WORDS = 35
COMMENTARY_MAX_WORDS = 65

# Subtitle block size.
# Larger blocks make tiny timing discrepancies much less distracting.
SUBTITLE_WORDS_PER_BLOCK = 14

# Maximum subtitle lines.
SUBTITLE_MAX_LINES = 3

# TTS speed.
TTS_RATE = "+0%"

# ============================================================
# TTS VOICES
#
# These are selected for natural conversational English.
# The role mapping deliberately does NOT identify the underlying
# LLM used for the debate.
# ============================================================

VOICES = {
    "Moderator": "en-US-AndrewMultilingualNeural",
    "AI Christian Apologist": "en-US-BrianMultilingualNeural",
    "AI Skeptic": "en-US-AvaMultilingualNeural",
    "Panelist 1": "en-US-ChristopherMultilingualNeural",
    "Panelist 2": "en-US-EmmaMultilingualNeural",
}

VOICE_FALLBACKS = [
    "en-US-AndrewMultilingualNeural",
    "en-US-AvaMultilingualNeural",
    "en-US-BrianMultilingualNeural",
    "en-US-ChristopherMultilingualNeural",
    "en-US-EmmaMultilingualNeural",
]


# ============================================================
# VISUAL CONFIG
# ============================================================

VIDEO_W = 1920
VIDEO_H = 1080

TOPIC_FONT_SIZE = 34

CARD_W = 610
CARD_H = 105

CARD_Y = 810

# Subtitle area deliberately central rather than bottom.
SUBTITLE_CENTER_Y = 520

# ============================================================
# JUDGING CATEGORIES
# ============================================================

JUDGING_CATEGORIES = [
    {
        "key": "logic",
        "name": "Logical Strength",
        "description": (
            "How coherent, consistent and logically persuasive the argument is."
        ),
    },
    {
        "key": "evidence",
        "name": "Evidence & Explanatory Power",
        "description": (
            "How well the side supports its claims and explains the issue."
        ),
    },
    {
        "key": "rebuttal",
        "name": "Rebuttal & Persuasiveness",
        "description": (
            "How effectively the side responds to the opponent and advances its own case."
        ),
    },
]


# ============================================================
# PREFERRED GENERATION MODELS
#
# These are preferences only. If unavailable, the script
# dynamically searches the current OpenRouter catalogue.
# ============================================================

PREFERRED_APOLOGIST_MODELS = [
    "openai/gpt-5",
    "openai/gpt-4o",
    "anthropic/claude-sonnet-4",
    "anthropic/claude-3.7-sonnet",
    "google/gemini-2.5-pro",
    "google/gemini-2.0-flash",
]

PREFERRED_SKEPTIC_MODELS = [
    "anthropic/claude-sonnet-4",
    "openai/gpt-5",
    "openai/gpt-4o",
    "google/gemini-2.5-pro",
    "x-ai/grok-3",
    "deepseek/deepseek-chat",
]

PREFERRED_MODERATOR_MODELS = [
    "openai/gpt-5",
    "anthropic/claude-sonnet-4",
    "google/gemini-2.5-pro",
]


# ============================================================
# BASIC UTILITIES
# ============================================================

def require_api_key():
    if not OPENROUTER_API_KEY:
        raise RuntimeError(
            "OPENROUTER_API_KEY environment variable is not set."
        )


def safe_filename(text):
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", text)
    return text[:120]


def word_count(text):
    return len(re.findall(r"\b[\w'’-]+\b", text or ""))


def clean_for_speech(text):
    if not text:
        return ""

    cleaned = re.sub(r"\([^)]*\)", "", text)
    cleaned = re.sub(r"\[[^\]]*\]", "", cleaned)

    cleaned = cleaned.replace("**", "")
    cleaned = cleaned.replace("__", "")
    cleaned = cleaned.replace("`", "")
    cleaned = cleaned.replace("#", "")

    cleaned = cleaned.replace("—", ", ")
    cleaned = cleaned.replace("–", ", ")
    cleaned = cleaned.replace(";", ". ")
    cleaned = cleaned.replace(":", ". ")

    cleaned = cleaned.replace("&", "and")
    cleaned = cleaned.replace('"', "")

    cleaned = re.sub(r"\s+", " ", cleaned)

    return cleaned.strip()


def escape_ass_text(text):
    """
    ASS escaping.
    """
    if text is None:
        return ""

    text = str(text)
    text = text.replace("\\", r"\\")
    text = text.replace("{", r"\{")
    text = text.replace("}", r"\}")
    text = text.replace("\n", " ")

    return text


def hex_to_rgba(hex_str, alpha):
    hex_str = hex_str.lstrip("#")
    return (
        int(hex_str[0:2], 16),
        int(hex_str[2:4], 16),
        int(hex_str[4:6], 16),
        alpha,
    )


# ============================================================
# FONT LOADER
# ============================================================

def load_font(size, bold=True):
    if bold:
        filenames = [
            "DejaVuSans-Bold.ttf",
            "LiberationSans-Bold.ttf",
            "Arial Bold.ttf",
            "arialbd.ttf",
        ]
    else:
        filenames = [
            "DejaVuSans.ttf",
            "LiberationSans-Regular.ttf",
            "Arial.ttf",
            "arial.ttf",
        ]

    paths = [
        "/usr/share/fonts/truetype/dejavu/",
        "/usr/share/fonts/truetype/liberation/",
        "C:\\Windows\\Fonts\\",
        "/System/Library/Fonts/Supplemental/",
        "",
    ]

    for directory in paths:
        for filename in filenames:
            path = os.path.join(directory, filename)

            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass

    return ImageFont.load_default()


# ============================================================
# CLEANUP
# ============================================================

def cleanup_cache():
    print("🧹 Cleaning workspace...")

    patterns = [
        "*.mp4",
        "*.mp3",
        "*.ass",
        "*.png",
        "*_list.txt",
        "model_cache.json",
    ]

    protected = {
        "final_debate_output.mp4",
        "background.png",
        "topic.txt",
    }

    for pattern in patterns:
        for file in glob.glob(pattern):
            if os.path.basename(file) in protected:
                continue

            try:
                os.remove(file)
            except Exception:
                pass

    print("✨ Workspace clean.")


# ============================================================
# OPENROUTER
# ============================================================

def openrouter_headers():
    return {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://youtube.com/",
        "X-Title": "AI Debate Arena",
    }


def fetch_available_models():
    """
    Dynamically retrieves the current OpenRouter catalogue.

    We do NOT assume the old hard-coded model list still exists.
    """

    require_api_key()

    try:
        response = requests.get(
            OPENROUTER_MODELS_URL,
            headers=openrouter_headers(),
            timeout=30,
        )

        response.raise_for_status()

        data = response.json()

        models = data.get("data", [])

        valid = []

        for model in models:
            model_id = model.get("id")

            if not model_id:
                continue

            # Chat models only.
            architecture = model.get("architecture", {})
            input_modalities = architecture.get("input_modalities", [])

            if input_modalities and "text" not in input_modalities:
                continue

            valid.append(model)

        print(f"🌐 OpenRouter currently reports {len(valid)} usable text models.")

        return valid

    except Exception as exc:
        print(f"⚠️ Could not retrieve live model catalogue: {exc}")

        return []


def model_supports_text(model):
    architecture = model.get("architecture", {})

    modalities = architecture.get("input_modalities")

    if not modalities:
        return True

    return "text" in modalities


def model_is_reasonable_judge(model):
    """
    Filters out obvious specialist/non-chat models.

    The filter is intentionally conservative because OpenRouter's
    catalogue changes over time.
    """

    model_id = model.get("id", "").lower()
    name = model.get("name", "").lower()

    if not model_supports_text(model):
        return False

    blocked_terms = [
        "embedding",
        "rerank",
        "moderation",
        "whisper",
        "tts",
        "speech",
        "image",
        "vision-only",
        "audio",
        "transcription",
        "code-only",
    ]

    combined = f"{model_id} {name}"

    if any(term in combined for term in blocked_terms):
        return False

    return True


def deduplicate_models(models):
    seen = set()
    output = []

    for model in models:
        model_id = model.get("id")

        if not model_id:
            continue

        if model_id in seen:
            continue

        seen.add(model_id)
        output.append(model)

    return output


def choose_preferred_model(available_models, preferences):
    available_ids = {
        m.get("id")
        for m in available_models
    }

    for preferred in preferences:
        if preferred in available_ids:
            return preferred

    # Try to find a strong current model by name.
    keywords = [
        "gpt-5",
        "claude-sonnet",
        "gemini-2.5-pro",
        "gemini-3",
        "grok-3",
        "deepseek",
    ]

    for keyword in keywords:
        for model in available_models:
            model_id = model.get("id", "").lower()

            if keyword in model_id:
                return model.get("id")

    # Last resort.
    if available_models:
        return available_models[0].get("id")

    return None


def build_judge_panel(available_models):
    """
    Creates up to 100 independent judges.

    The same provider/model is only used once.
    """

    candidates = [
        m for m in available_models
        if model_is_reasonable_judge(m)
    ]

    candidates = deduplicate_models(candidates)

    # Prefer a broad mixture of providers.
    provider_buckets = {}

    for model in candidates:
        model_id = model.get("id", "")

        provider = model_id.split("/")[0]

        provider_buckets.setdefault(provider, []).append(model)

    selected = []
    used = set()

    # Round-robin across providers.
    while len(selected) < MAX_JUDGES:

        made_progress = False

        for provider in list(provider_buckets.keys()):

            bucket = provider_buckets[provider]

            while bucket:
                model = bucket.pop(0)

                model_id = model.get("id")

                if model_id in used:
                    continue

                used.add(model_id)
                selected.append(model)
                made_progress = True
                break

            if len(selected) >= MAX_JUDGES:
                break

        if not made_progress:
            break

    print(f"⚖️ Dynamic judging panel: {len(selected)} AI judges.")

    return selected


# ============================================================
# OPENROUTER QUERY
# ============================================================

def query_openrouter(
    prompt,
    model_id,
    timeout=60,
    max_tokens=1200,
    temperature=0.7,
):
    if not model_id:
        return None

    headers = openrouter_headers()

    payload = {
        "model": model_id,
        "messages": [
            {
                "role": "user",
                "content": prompt,
            }
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    for attempt in range(MODEL_RETRIES):

        try:
            response = requests.post(
                OPENROUTER_CHAT_URL,
                headers=headers,
                json=payload,
                timeout=timeout,
            )

            if response.status_code != 200:
                print(
                    f"⚠️ {model_id} returned "
                    f"{response.status_code} "
                    f"(attempt {attempt + 1})"
                )

                time.sleep(1.5)
                continue

            data = response.json()

            choices = data.get("choices", [])

            if not choices:
                continue

            message = choices[0].get("message", {})

            content = message.get("content")

            if isinstance(content, list):
                content = "".join(
                    item.get("text", "")
                    for item in content
                    if isinstance(item, dict)
                )

            if not content:
                continue

            content = str(content).strip()

            if len(content) > 10:
                return content

        except Exception as exc:
            print(
                f"⚠️ Query failed for {model_id}: "
                f"{exc}"
            )

            time.sleep(1)

    return None


# ============================================================
# LONG-FORM GENERATION
# ============================================================

def ensure_minimum_length(
    text,
    minimum_words,
    model_id,
    continuation_context,
    max_tokens=1000,
):
    """
    If a model ignores the requested length, ask it to continue.

    This specifically prevents the previous problem where the
    skeptic sometimes produced only a few lines.
    """

    if not text:
        return None

    if word_count(text) >= minimum_words:
        return text

    print(
        f"⚠️ Response only {word_count(text)} words. "
        f"Requesting continuation..."
    )

    remaining = minimum_words - word_count(text)

    continuation_prompt = f"""
Continue the response below.

IMPORTANT:
- Do NOT restart.
- Do NOT say you are continuing.
- Do NOT summarize what was already said.
- Add genuinely new reasoning.
- Directly develop the argument further.
- Write at least {max(180, remaining)} additional words.
- Use natural conversational language.
- No headings.
- No meta-commentary.

Previous response:
{text}

Additional context:
{continuation_context}
"""

    continuation = query_openrouter(
        continuation_prompt,
        model_id,
        timeout=75,
        max_tokens=max_tokens,
        temperature=0.65,
    )

    if continuation:
        text = text.rstrip() + " " + continuation.strip()

    return text


def generate_apologist(topic, round_num, history, model_id):
    previous = history[-1] if history else "No previous round."

    prompt = f"""
You are the Christian Apologist in a serious long-form AI debate.

Topic:
{topic}

This is Round {round_num}.

Your task is to advance the Christian case.

Do NOT announce what you are doing.
Do NOT say "in this round".
Do NOT mention artificial intelligence.
Do NOT mention your underlying model.
Do NOT repeat your previous round word-for-word.

Instead, continue naturally from the previous exchange.

Previous debate context:
{previous}

Requirements:
- Introduce genuinely new reasoning.
- Address the strongest issue raised previously.
- Use clear everyday language.
- Use concrete examples or analogies when useful.
- Do not use academic jargon unless absolutely necessary.
- Sound like a confident human debater.
- Do not simply list points.
- Build a connected argument.
- Avoid exaggerated claims.
- No headings.
- No meta-commentary.

Write approximately {APOLOGIST_TARGET_WORDS} words.
"""

    text = query_openrouter(
        prompt,
        model_id,
        timeout=90,
        max_tokens=850,
        temperature=0.72,
    )

    if not text:
        return (
            "The strongest Christian case begins with the question of "
            "whether the basic features of reality are better explained "
            "by something beyond the universe itself."
        )

    return ensure_minimum_length(
        text,
        APOLOGIST_MIN_WORDS,
        model_id,
        previous,
        max_tokens=500,
    )


def generate_skeptic(topic, round_num, apologist_text, previous_skeptic, model_id):
    prompt = f"""
You are the AI Skeptic in a serious long-form debate.

Topic:
{topic}

Round:
{round_num}

Your job is to give a COMPLETE and FORCEFUL rebuttal to the Christian Apologist.

The previous version of this system sometimes produced only a few sentences.
That is unacceptable.

You MUST produce a substantial rebuttal of at least {SKEPTIC_MIN_WORDS} words.

You have two jobs:

1. Identify the strongest claims in the Apologist's argument.
2. Answer them directly with fresh reasoning.

Do NOT merely say you disagree.

Do NOT write a short response.

Do NOT use headings.

Do NOT say:
"I am the AI Skeptic"
"the model"
"as an AI"
"here is my rebuttal"
"the previous argument"

Do not explain what you are about to do.

Do not repeat the same argument from the previous round unless you are
specifically showing why it fails under the new argument.

The debate must progress.

Use:
- clear conversational English
- concrete examples
- simple analogies
- direct counterarguments
- careful distinctions
- natural transitions

Avoid:
- academic jargon
- filler
- generic skepticism
- repeating the Apologist's wording
- fake quotations

Previous Skeptic response:
{previous_skeptic if previous_skeptic else "None. This is the first round."}

Current Apologist response:
{apologist_text}

Now write the full rebuttal.
Aim for approximately {SKEPTIC_TARGET_WORDS} words.
"""

    text = query_openrouter(
        prompt,
        model_id,
        timeout=120,
        max_tokens=1300,
        temperature=0.72,
    )

    if not text:
        # Second attempt with a simpler prompt.
        retry_prompt = f"""
Write a long-form skeptical rebuttal to this Christian argument.

Topic:
{topic}

Argument:
{apologist_text}

Write at least {SKEPTIC_MIN_WORDS} words.

Address the argument point-by-point, but use natural conversational prose.
Introduce new reasoning and examples.
Do not mention AI or the instructions.
"""

        text = query_openrouter(
            retry_prompt,
            model_id,
            timeout=120,
            max_tokens=1300,
            temperature=0.68,
        )

    if not text:
        return (
            "The argument needs to establish more than the possibility of "
            "a creator. It needs to show why that explanation is actually "
            "better than the alternatives. A possibility is not automatically "
            "an explanation, and that distinction matters when we compare "
            "competing accounts of the universe."
        )

    text = ensure_minimum_length(
        text,
        SKEPTIC_MIN_WORDS,
        model_id,
        apologist_text,
        max_tokens=900,
    )

    # Final emergency continuation if still short.
    if word_count(text) < SKEPTIC_MIN_WORDS:
        continuation_prompt = f"""
Continue this skeptical debate response naturally.

You MUST add at least 250 words of NEW reasoning.

Do not restart.
Do not summarize.
Do not mention the instructions.
Do not say "continuing".
Directly develop the rebuttal.

Response so far:
{text}
"""

        continuation = query_openrouter(
            continuation_prompt,
            model_id,
            timeout=120,
            max_tokens=700,
            temperature=0.65,
        )

        if continuation:
            text += " " + continuation

    return text


# ============================================================
# COMMENTARY GENERATION
# ============================================================

def generate_unique_commentary(
    topic,
    round_num,
    winner_side,
    judge_name,
    debate_context,
    model_id,
):
    category = (
        "logical structure"
        if winner_side == "A"
        else "practical explanatory strength"
    )

    prompt = f"""
You are one independent judge on a Christian Apologist versus AI Skeptic debate.

Topic:
{topic}

Round:
{round_num}

You favoured:
{"the Christian Apologist" if winner_side == "A" else "the AI Skeptic"}

Your role is NOT to recap the debate.

Give a short, original observation about {category}.

IMPORTANT:
- Do not repeat either debater's wording.
- Do not quote them.
- Do not summarize their arguments.
- Do not say who you are.
- Do not mention the model name.
- Do not use phrases like "the apologist argued" or "the skeptic said".
- Give one genuinely fresh insight.
- 2–3 natural sentences.
- Approximately {COMMENTARY_MIN_WORDS}–{COMMENTARY_MAX_WORDS} words.
- Conversational language.
- No jargon.

Debate context:
{debate_context}
"""

    result = query_openrouter(
        prompt,
        model_id,
        timeout=50,
        max_tokens=150,
        temperature=0.9,
    )

    if not result:
        if winner_side == "A":
            return (
                "The stronger case was the one that connected its conclusions "
                "more consistently from one idea to the next. That makes the "
                "overall explanation easier to follow and harder to dismiss."
            )

        return (
            "The stronger case was the one that demanded fewer assumptions "
            "before reaching its conclusion. That gives the explanation a "
            "useful advantage when competing ideas are being compared."
        )

    return result


# ============================================================
# JUDGING
# ============================================================

def extract_json_object(text):
    if not text:
        return None

    # First try fenced JSON.
    fenced = re.search(
        r"```(?:json)?\s*(\{.*?\})\s*```",
        text,
        re.DOTALL | re.IGNORECASE,
    )

    if fenced:
        try:
            return json.loads(fenced.group(1))
        except Exception:
            pass

    # Then normal object.
    match = re.search(r"\{.*\}", text, re.DOTALL)

    if match:
        try:
            return json.loads(match.group(0))
        except Exception:
            pass

    return None


def clamp_score(value):
    try:
        value = float(value)
    except Exception:
        return None

    return max(0.0, min(100.0, value))


def evaluate_single_judge(
    judge,
    topic,
    round_num,
    apologist_text,
    skeptic_text,
):
    judge_id = judge.get("id")
    judge_name = judge.get("name", judge_id)

    prompt = f"""
You are an independent judge evaluating a debate.

Topic:
{topic}

Round:
{round_num}

There are THREE judging categories.

CATEGORY 1 — Logical Strength
How coherent, consistent and logically persuasive is the argument?

CATEGORY 2 — Evidence & Explanatory Power
How well does it support its claims and explain the issue?

CATEGORY 3 — Rebuttal & Persuasiveness
How effectively does it answer the opposing case and persuade?

Score BOTH sides independently from 0 to 100 in each category.

Do NOT automatically give similar scores.
Do NOT favour either side because of the subject matter.
Judge the actual arguments presented.

Return ONLY valid JSON in exactly this structure:

{{
  "A": {{
    "logical_strength": 0,
    "evidence_explanatory_power": 0,
    "rebuttal_persuasiveness": 0
  }},
  "B": {{
    "logical_strength": 0,
    "evidence_explanatory_power": 0,
    "rebuttal_persuasiveness": 0
  }}
}}

Side A — Christian Apologist:
{apologist_text}

Side B — AI Skeptic:
{skeptic_text}
"""

    response = query_openrouter(
        prompt,
        judge_id,
        timeout=35,
        max_tokens=300,
        temperature=0.15,
    )

    parsed = extract_json_object(response)

    if not parsed:
        return None

    try:
        a = parsed["A"]
        b = parsed["B"]

        a_scores = [
            clamp_score(a["logical_strength"]),
            clamp_score(a["evidence_explanatory_power"]),
            clamp_score(a["rebuttal_persuasiveness"]),
        ]

        b_scores = [
            clamp_score(b["logical_strength"]),
            clamp_score(b["evidence_explanatory_power"]),
            clamp_score(b["rebuttal_persuasiveness"]),
        ]

        if any(x is None for x in a_scores + b_scores):
            return None

        overall_a = mean(a_scores)
        overall_b = mean(b_scores)

        return {
            "name": judge_name,
            "id": judge_id,
            "categories_a": a_scores,
            "categories_b": b_scores,
            "score_a": overall_a,
            "score_b": overall_b,
            "favored": "A" if overall_a >= overall_b else "B",
        }

    except Exception:
        return None


def run_judging_panel(
    judges,
    topic,
    round_num,
    apologist_text,
    skeptic_text,
):
    print(
        f"⚖️ Round {round_num}: "
        f"asking {len(judges)} AI judges..."
    )

    results = []

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=JUDGE_WORKERS
    ) as executor:

        futures = [
            executor.submit(
                evaluate_single_judge,
                judge,
                topic,
                round_num,
                apologist_text,
                skeptic_text,
            )
            for judge in judges
        ]

        for future in concurrent.futures.as_completed(futures):
            try:
                result = future.result()

                if result:
                    results.append(result)

            except Exception as exc:
                print(f"⚠️ Judge failed: {exc}")

    if len(results) < MIN_JUDGES:
        raise RuntimeError(
            f"Only {len(results)} judges returned valid scores. "
            f"At least {MIN_JUDGES} are required."
        )

    avg_a = mean(j["score_a"] for j in results)
    avg_b = mean(j["score_b"] for j in results)

    print(
        f"📊 Round {round_num}: "
        f"A={avg_a:.2f} | B={avg_b:.2f} "
        f"| Valid judges={len(results)}"
    )

    return results, avg_a, avg_b


# ============================================================
# AUDIO + WORD TIMINGS
# ============================================================

async def _generate_audio_and_words(
    text,
    voice,
    audio_filename,
):
    communicate = edge_tts.Communicate(
        text,
        voice,
        rate=TTS_RATE,
    )

    audio_data = b""
    words = []

    async for chunk in communicate.stream():

        if chunk["type"] == "audio":
            audio_data += chunk["data"]

        elif chunk["type"] == "WordBoundary":

            words.append(
                {
                    "text": chunk["text"],
                    "start": chunk["offset"] / 10_000_000,
                    "duration": chunk["duration"] / 10_000_000,
                    "end": (
                        chunk["offset"] + chunk["duration"]
                    ) / 10_000_000,
                }
            )

    with open(audio_filename, "wb") as f:
        f.write(audio_data)

    return words


def estimate_word_timings(text, audio_filename):
    """
    Fallback if Edge does not provide word boundaries.

    The fallback distributes words proportionally rather than using
    the old fixed 0.35 seconds per word.
    """

    try:
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                audio_filename,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )

        duration = float(probe.stdout.strip())

    except Exception:
        duration = max(1.0, word_count(text) * 0.35)

    raw_words = text.split()

    if not raw_words:
        return []

    # Slightly weighted timing based on word length.
    weights = [
        max(1.0, len(re.sub(r"\W", "", w)))
        for w in raw_words
    ]

    total_weight = sum(weights)

    words = []
    current = 0.0

    for raw, weight in zip(raw_words, weights):

        portion = duration * (weight / total_weight)

        words.append(
            {
                "text": raw,
                "start": current,
                "duration": portion,
                "end": current + portion,
            }
        )

        current += portion

    return words


def generate_edge_audio_and_subs(
    text,
    role_key,
    output_audio,
    output_ass,
):
    voice = VOICES.get(
        role_key,
        VOICES["Moderator"],
    )

    safe_text = clean_for_speech(text)

    words = []

    try:
        words = asyncio.run(
            _generate_audio_and_words(
                safe_text,
                voice,
                output_audio,
            )
        )

    except Exception as exc:
        print(
            f"⚠️ TTS {voice} failed: {exc}"
        )

        for fallback_voice in VOICE_FALLBACKS:

            try:
                words = asyncio.run(
                    _generate_audio_and_words(
                        safe_text,
                        fallback_voice,
                        output_audio,
                    )
                )

                if words:
                    break

            except Exception:
                continue

    if not words:
        words = estimate_word_timings(
            safe_text,
            output_audio,
        )

    generate_paragraph_karaoke_ass(
        words,
        output_ass,
    )

    return words


# ============================================================
# SUBTITLES
# ============================================================

def format_ass_time(seconds):
    seconds = max(0, seconds)

    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60

    return f"{hours}:{minutes:02d}:{secs:05.2f}"


def wrap_words(words, max_chars=65):
    """
    Splits a subtitle paragraph into sensible visual lines.
    """

    lines = []
    current = ""
    current_words = []

    for word in words:

        candidate = (
            f"{current} {word['text']}"
            if current
            else word["text"]
        )

        if len(candidate) <= max_chars:
            current = candidate
            current_words.append(word)

        else:
            if current_words:
                lines.append(current_words)

            current = word["text"]
            current_words = [word]

    if current_words:
        lines.append(current_words)

    return lines


def generate_paragraph_karaoke_ass(words, ass_filename):
    """
    Important subtitle redesign.

    Instead of displaying one sentence at a time, we keep a
    paragraph/block on screen.

    The block remains stable while the currently spoken word
    changes colour.

    This makes small TTS timestamp errors much less visually
    distracting.
    """

    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Subtitle,DejaVu Sans,42,&H00FFFFFF,&H0000FFFF,&H00000000,&H99000000,1,0,0,0,100,100,0,0,1,3,1,5,120,120,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    if not words:
        with open(ass_filename, "w", encoding="utf-8") as f:
            f.write(header)

        return

    lines = []

    block_start = 0

    while block_start < len(words):

        block_end = min(
            block_start + SUBTITLE_WORDS_PER_BLOCK,
            len(words),
        )

        block = words[block_start:block_end]

        block_lines = wrap_words(block, max_chars=65)

        # Keep a maximum of 3 lines.
        while len(block_lines) > SUBTITLE_MAX_LINES:
            block_end -= 1

            if block_end <= block_start:
                break

            block = words[block_start:block_end]
            block_lines = wrap_words(block, max_chars=65)

        if not block:
            break

        block_start = block_end

        block_start_time = block[0]["start"]

        if block_start < len(words):
            block_end_time = words[block_start]["start"]
        else:
            block_end_time = block[-1]["end"] + 0.15

        # Each word gets its own stable block event.
        for i, current_word in enumerate(block):

            start = current_word["start"]

            if i + 1 < len(block):
                end = block[i + 1]["start"]
            else:
                end = block_end_time

            # Build complete block with current word highlighted.
            formatted_lines = []

            for line in block_lines:

                line_text = []

                for word in line:

                    if word is current_word:
                        line_text.append(
                            r"{\c&H00FFFF&}"
                            + escape_ass_text(word["text"])
                            + r"{\c&HFFFFFF&}"
                        )
                    else:
                        line_text.append(
                            escape_ass_text(word["text"])
                        )

                formatted_lines.append(
                    " ".join(line_text)
                )

            subtitle_text = r"\N".join(formatted_lines)

            # Centre of screen.
            lines.append(
                "Dialogue: 10,"
                f"{format_ass_time(start)},"
                f"{format_ass_time(end)},"
                f"Subtitle,,0,0,0,,"
                f"{subtitle_text}"
            )

    with open(ass_filename, "w", encoding="utf-8") as f:
        f.write(header)
        f.write("\n".join(lines))
        f.write("\n")


# ============================================================
# BACKGROUND
# ============================================================

def create_background(
    pos,
    glow_color,
    bg_out,
):
    script_dir = os.path.dirname(
        os.path.abspath(__file__)
    )

    background_path = os.path.join(
        script_dir,
        "background.png",
    )

    if os.path.exists(background_path):

        try:
            base_img = (
                Image.open(background_path)
                .convert("RGB")
                .resize((VIDEO_W, VIDEO_H))
            )

        except Exception:
            base_img = Image.new(
                "RGB",
                (VIDEO_W, VIDEO_H),
                (12, 16, 32),
            )

    else:

        base_img = Image.new(
            "RGB",
            (VIDEO_W, VIDEO_H),
            (12, 16, 32),
        )

        draw = ImageDraw.Draw(base_img)

        for x in range(0, VIDEO_W, 60):
            draw.line(
                [(x, 0), (x, VIDEO_H)],
                fill=(20, 26, 45),
                width=2,
            )

        for y in range(0, VIDEO_H, 60):
            draw.line(
                [(0, y), (VIDEO_W, y)],
                fill=(20, 26, 45),
                width=2,
            )

    overlay = Image.new(
        "RGBA",
        base_img.size,
        (0, 0, 0, 0),
    )

    draw = ImageDraw.Draw(overlay)

    if pos == "left":
        cx = 400

    elif pos == "right":
        cx = 1520

    else:
        cx = 960

    for radius in range(700, 50, -50):

        alpha = int(
            14 * (
                1.0 -
                radius / 700.0
            )
        )

        draw.ellipse(
            [
                cx - radius,
                540 - radius,
                cx + radius,
                540 + radius,
            ],
            fill=hex_to_rgba(
                glow_color,
                alpha,
            ),
        )

    img = Image.alpha_composite(
        base_img.convert("RGBA"),
        overlay.filter(
            ImageFilter.GaussianBlur(30)
        ),
    ).convert("RGB")

    img.save(bg_out)


# ============================================================
# UI OVERLAY
# ============================================================

def create_ui_overlay(
    speaker_name,
    role_label,
    topic,
    pos,
    glow_color,
    ui_out,
):
    ui_img = Image.new(
        "RGBA",
        (VIDEO_W, VIDEO_H),
        (0, 0, 0, 0),
    )

    draw = ImageDraw.Draw(ui_img)

    # Smaller title.
    font_title = load_font(
        TOPIC_FONT_SIZE,
        bold=True,
    )

    font_name = load_font(
        28,
        bold=True,
    )

    font_role = load_font(
        21,
        bold=False,
    )

    # Shorten topic if necessary.
    display_topic = topic

    if len(display_topic) > 90:
        display_topic = display_topic[:87] + "..."

    title = f"TOPIC: {display_topic}"

    bbox = draw.textbbox(
        (0, 0),
        title,
        font=font_title,
    )

    title_x = (
        VIDEO_W -
        (bbox[2] - bbox[0])
    ) // 2

    # Moved slightly higher and smaller.
    draw.text(
        (title_x, 28),
        title,
        fill="white",
        font=font_title,
    )

    # Speaker cards moved away from subtitle area.
    if pos == "left":
        card_x = 90

    elif pos == "right":
        card_x = VIDEO_W - CARD_W - 90

    else:
        card_x = (
            VIDEO_W - CARD_W
        ) // 2

    card_y = CARD_Y

    draw.rounded_rectangle(
        [
            card_x,
            card_y,
            card_x + CARD_W,
            card_y + CARD_H,
        ],
        radius=18,
        fill=(18, 26, 46, 235),
        outline=glow_color,
        width=3,
    )

    draw.ellipse(
        [
            card_x + 28,
            card_y + 39,
            card_x + 48,
            card_y + 59,
        ],
        fill=glow_color,
    )

    # Name width is controlled so it cannot run into the waveform.
    display_name = speaker_name

    if len(display_name) > 32:
        display_name = display_name[:29] + "..."

    draw.text(
        (
            card_x + 70,
            card_y + 21,
        ),
        display_name,
        fill="white",
        font=font_name,
    )

    display_role = role_label.upper()

    draw.text(
        (
            card_x + 70,
            card_y + 61,
        ),
        display_role,
        fill=glow_color,
        font=font_role,
    )

    ui_img.save(ui_out)

    return card_x


# ============================================================
# SCOREBOARD IMAGE
# ============================================================

def generate_round_breakdown_image(
    round_num,
    judge_results,
    total_a,
    total_b,
    cum_a,
    cum_b,
    img_out,
):
    script_dir = os.path.dirname(
        os.path.abspath(__file__)
    )

    background_path = os.path.join(
        script_dir,
        "background.png",
    )

    if os.path.exists(background_path):

        try:
            img = (
                Image.open(background_path)
                .convert("RGB")
                .resize((VIDEO_W, VIDEO_H))
            )

        except Exception:
            img = Image.new(
                "RGB",
                (VIDEO_W, VIDEO_H),
                (12, 16, 32),
            )

    else:
        img = Image.new(
            "RGB",
            (VIDEO_W, VIDEO_H),
            (12, 16, 32),
        )

    overlay = Image.new(
        "RGBA",
        (VIDEO_W, VIDEO_H),
        (0, 0, 0, 225),
    )

    img = Image.alpha_composite(
        img.convert("RGBA"),
        overlay,
    ).convert("RGB")

    draw = ImageDraw.Draw(img)

    font_header = load_font(
        36,
        bold=True,
    )

    font_sub = load_font(
        22,
        bold=True,
    )

    font_small = load_font(
        17,
        bold=False,
    )

    font_tiny = load_font(
        14,
        bold=False,
    )

    def centered(y, text, font, fill):
        bbox = draw.textbbox(
            (0, 0),
            text,
            font=font,
        )

        x = (
            VIDEO_W -
            (bbox[2] - bbox[0])
        ) // 2

        draw.text(
            (x, y),
            text,
            fill=fill,
            font=font,
        )

    judge_count = len(judge_results)

    centered(
        35,
        f"ROUND {round_num} — AI JUDGING PANEL",
        font_header,
        "#FFD700",
    )

    centered(
        88,
        (
            f"{judge_count} independent AI judges • "
            f"Scores are out of 100"
        ),
        font_sub,
        "#FFFFFF",
    )

    centered(
        125,
        (
            f"Round Average — "
            f"Apologist {total_a:.1f}  |  "
            f"Skeptic {total_b:.1f}"
        ),
        font_sub,
        "#FFFFFF",
    )

    centered(
        160,
        (
            f"Cumulative — "
            f"Apologist {cum_a:.1f}  |  "
            f"Skeptic {cum_b:.1f}"
        ),
        font_sub,
        "#FFD700",
    )

    # --------------------------------------------------------
    # CATEGORY AVERAGES
    # --------------------------------------------------------

    category_a = []
    category_b = []

    for index in range(3):

        category_a.append(
            mean(
                j["categories_a"][index]
                for j in judge_results
            )
        )

        category_b.append(
            mean(
                j["categories_b"][index]
                for j in judge_results
            )
        )

    draw.text(
        (150, 215),
        "CATEGORY AVERAGES",
        fill="#00FFFF",
        font=font_sub,
    )

    category_y = 255

    for index, category in enumerate(
        JUDGING_CATEGORIES
    ):

        draw.text(
            (
                150,
                category_y,
            ),
            category["name"],
            fill="white",
            font=font_small,
        )

        draw.text(
            (
                600,
                category_y,
            ),
            f"{category_a[index]:.1f}",
            fill="#00FFCC",
            font=font_small,
        )

        draw.text(
            (
                760,
                category_y,
            ),
            f"{category_b[index]:.1f}",
            fill="#FF66FF",
            font=font_small,
        )

        category_y += 32

    # --------------------------------------------------------
    # JUDGE BREAKDOWN
    #
    # All judges are displayed, not just three.
    # --------------------------------------------------------

    draw.text(
        (1050, 215),
        f"INDIVIDUAL JUDGES ({judge_count})",
        fill="#FFD700",
        font=font_sub,
    )

    start_y = 250

    # Three columns.
    columns = 3

    per_column = math.ceil(
        judge_count / columns
    )

    column_width = 270

    for i, judge in enumerate(
        judge_results
    ):

        col = i // per_column
        row = i % per_column

        x = 1050 + col * column_width
        y = start_y + row * 22

        if x + 240 > VIDEO_W:
            continue

        name = judge["name"]

        if len(name) > 18:
            name = name[:15] + "..."

        fav = "A" if judge["favored"] == "A" else "S"

        text = (
            f"{i+1:02d} {name} "
            f"{judge['score_a']:.0f}-{judge['score_b']:.0f} {fav}"
        )

        fill = (
            "#00FFCC"
            if judge["favored"] == "A"
            else "#FF66FF"
        )

        draw.text(
            (x, y),
            text,
            fill=fill,
            font=font_tiny,
        )

    # Legend.
    draw.text(
        (150, 370),
        "A = Apologist   S = Skeptic",
        fill="#FFFFFF",
        font=font_small,
    )

    draw.text(
        (150, 410),
        "Each judge scores both sides independently in all three categories.",
        fill="#AAAAAA",
        font=font_small,
    )

    img.save(img_out)


# ============================================================
# FFMPEG
# ============================================================

def render_video_segment(
    bg_path,
    ui_path,
    audio_path,
    ass_path,
    output_path,
    position,
    glow_color,
    card_x,
    zoom_bg=True,
):
    ff_color = (
        "0x" +
        glow_color.lstrip("#")
    )

    safe_ass_path = (
        os.path.abspath(ass_path)
        .replace("\\", "/")
        .replace(":", "\\:")
    )

    if zoom_bg:

        if position == "left":
            pan_x = "0"

        elif position == "right":
            pan_x = "iw-(iw/zoom)"

        else:
            pan_x = "(iw-(iw/zoom))/2"

        pan_y = "(ih-(ih/zoom))/2"

        bg_filter = (
            "[0:v]"
            "scale=1920:1080,"
            "zoompan="
            "z='min(zoom+0.0007,1.15)':"
            f"x='{pan_x}':"
            f"y='{pan_y}':"
            "d=8000:"
            "s=1920x1080:"
            "fps=30"
            "[bg_processed];"
        )

    else:

        bg_filter = (
            "[0:v]"
            "scale=1920:1080"
            "[bg_processed];"
        )

    # Waveform positioned to the right of the card.
    wave_x = card_x + 405
    wave_y = CARD_Y + 27

    filter_complex = (
        bg_filter

        "[1:v]"
        "scale=1920:1080"
        "[ui];"

        f"[2:a]"
        "showwaves="
        "s=160x45:"
        "mode=cline:"
        f"colors={ff_color}:"
        "rate=30"
        "[wave];"

        "[bg_processed]"
        "[ui]"
        "overlay=0:0"
        "[bg_with_ui];"

        "[bg_with_ui]"
        "[wave]"
        f"overlay={wave_x}:{wave_y}"
        f",ass='{safe_ass_path}'"
        "[outv]"
    )

    cmd = [
        "ffmpeg",
        "-y",

        "-loop",
        "1",
        "-framerate",
        "30",
        "-i",
        bg_path,

        "-i",
        ui_path,

        "-i",
        audio_path,

        "-filter_complex",
        filter_complex,

        "-map",
        "[outv]",

        "-map",
        "2:a",

        "-c:v",
        "libx264",

        "-preset",
        "medium",

        "-crf",
        "19",

        "-pix_fmt",
        "yuv420p",

        "-c:a",
        "aac",

        "-b:a",
        "192k",

        "-shortest",

        output_path,
    ]

    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    if result.returncode != 0:

        print(
            "❌ FFmpeg segment failed:\n"
            + result.stderr[-4000:]
        )

        raise RuntimeError(
            f"FFmpeg failed for {output_path}"
        )

    if not os.path.exists(output_path):
        raise RuntimeError(
            f"FFmpeg did not create {output_path}"
        )


# ============================================================
# VIDEO SEGMENT MANAGER
# ============================================================

class VideoBuilder:

    def __init__(self, topic):
        self.topic = topic
        self.final_segments = []
        self.frame_counter = 0

    def add_segment(
        self,
        text,
        role,
        name,
        topic_str=None,
        glow=None,
    ):
        if not text:
            return

        if topic_str is None:
            topic_str = self.topic

        if glow is None:

            if role == "AI Christian Apologist":
                glow = "#00FFCC"

            elif role == "AI Skeptic":
                glow = "#FF00FF"

            elif "Panelist" in role:
                glow = "#3399FF"

            else:
                glow = "#FFD700"

        if role == "AI Christian Apologist":
            position = "left"

        elif role == "AI Skeptic":
            position = "right"

        else:
            position = "center"

        index = self.frame_counter

        audio = f"aud_{index}.mp3"
        bg = f"bg_{index}.png"
        ui = f"ui_{index}.png"
        ass = f"ass_{index}.ass"
        video = f"seg_{index}.mp4"

        print(
            f"🎬 Rendering segment {index}: "
            f"{name}"
        )

        generate_edge_audio_and_subs(
            text,
            role,
            audio,
            ass,
        )

        create_background(
            position,
            glow,
            bg,
        )

        card_x = create_ui_overlay(
            name,
            role,
            topic_str,
            position,
            glow,
            ui,
        )

        render_video_segment(
            bg,
            ui,
            audio,
            ass,
            video,
            position,
            glow,
            card_x,
            zoom_bg=True,
        )

        self.final_segments.append(video)

        self.frame_counter += 1


# ============================================================
# INTRODUCTION
# ============================================================

def create_intro_text(topic, judge_count):
    return (
        "Welcome to the Ultimate AI Debate Arena. "
        "Today, a Christian Apologist and an AI Skeptic will debate one of "
        "humanity's biggest questions. "
        f"The debate will be evaluated by up to {judge_count} independent AI judges, "
        "using three categories: logical strength, evidence and explanatory power, "
        "and rebuttal and persuasiveness. "
        "There are no model names on the debating stage. "
        "The arguments stand on their own. "
        "Let's begin."
    )


def create_score_narration(
    round_num,
    judge_count,
    avg_a,
    avg_b,
    cumulative_a,
    cumulative_b,
):
    if avg_a > avg_b:
        leader = "The Apologist leads this round."
    elif avg_b > avg_a:
        leader = "The Skeptic leads this round."
    else:
        leader = "The round is tied."

    return (
        f"Round {round_num} has been judged by "
        f"{judge_count} independent AI judges. "
        f"The Apologist averaged {avg_a:.1f} out of 100, "
        f"while the Skeptic averaged {avg_b:.1f}. "
        f"{leader} "
        f"The cumulative score is now "
        f"{cumulative_a:.1f} for the Apologist "
        f"and {cumulative_b:.1f} for the Skeptic."
    )


# ============================================================
# MAIN PIPELINE
# ============================================================

def run_debate_pipeline():

    cleanup_cache()

    require_api_key()

    # --------------------------------------------------------
    # Topic
    # --------------------------------------------------------

    if not os.path.exists("topic.txt"):

        with open(
            "topic.txt",
            "w",
            encoding="utf-8",
        ) as f:
            f.write(
                "Does the universe require a creator?"
            )

    with open(
        "topic.txt",
        "r",
        encoding="utf-8",
    ) as f:
        topic = f.read().strip()

    if not topic:
        raise RuntimeError(
            "topic.txt is empty."
        )

    print(
        f"\n🎯 TOPIC:\n{topic}\n"
    )

    # --------------------------------------------------------
    # Discover models
    # --------------------------------------------------------

    available_models = fetch_available_models()

    if not available_models:
        raise RuntimeError(
            "No OpenRouter models were discovered."
        )

    # --------------------------------------------------------
    # Choose debate models
    # --------------------------------------------------------

    apologist_model = choose_preferred_model(
        available_models,
        PREFERRED_APOLOGIST_MODELS,
    )

    skeptic_model = choose_preferred_model(
        available_models,
        PREFERRED_SKEPTIC_MODELS,
    )

    moderator_model = choose_preferred_model(
        available_models,
        PREFERRED_MODERATOR_MODELS,
    )

    if not apologist_model:
        raise RuntimeError(
            "Could not find an Apologist generation model."
        )

    if not skeptic_model:
        raise RuntimeError(
            "Could not find a Skeptic generation model."
        )

    if not moderator_model:
        moderator_model = apologist_model

    print(
        f"🧠 Debate generation models selected."
    )

    # --------------------------------------------------------
    # Dynamic judge panel
    # --------------------------------------------------------

    judges = build_judge_panel(
        available_models
    )

    if len(judges) < MIN_JUDGES:
        raise RuntimeError(
            f"Only {len(judges)} usable judges found."
        )

    judge_count = len(judges)

    # --------------------------------------------------------
    # Builder
    # --------------------------------------------------------

    builder = VideoBuilder(topic)

    # --------------------------------------------------------
    # Intro
    # --------------------------------------------------------

    builder.add_segment(
        create_intro_text(
            topic,
            judge_count,
        ),
        "Moderator",
        "Moderator",
        topic,
        "#FFD700",
    )

    builder.add_segment(
        f"Today's question is: {topic}. "
        "The Christian Apologist will present the case first, "
        "and the AI Skeptic will respond. "
        "Each round will build directly on the last.",
        "Moderator",
        "Moderator",
        topic,
        "#FFD700",
    )

    # --------------------------------------------------------
    # Round state
    # --------------------------------------------------------

    cumulative_a = 0.0
    cumulative_b = 0.0

    apologist_history = []
    skeptic_history = []

    # --------------------------------------------------------
    # Three rounds
    # --------------------------------------------------------

    for round_num in range(1, 4):

        print(
            f"\n============================"
            f"\nROUND {round_num}"
            f"\n============================"
        )

        if round_num == 1:

            builder.add_segment(
                "Round one begins. The Apologist has the floor.",
                "Moderator",
                "Moderator",
                topic,
                "#FFD700",
            )

        else:

            builder.add_segment(
                f"Round {round_num} continues the argument.",
                "Moderator",
                "Moderator",
                topic,
                "#FFD700",
            )

        # ----------------------------------------------------
        # Apologist
        # ----------------------------------------------------

        previous_context = ""

        if apologist_history:
            previous_context = (
                "\nPrevious Apologist argument:\n"
                + apologist_history[-1]
            )

        if skeptic_history:
            previous_context += (
                "\nPrevious Skeptic response:\n"
                + skeptic_history[-1]
            )

        apologist_text = generate_apologist(
            topic,
            round_num,
            apologist_history + skeptic_history,
            apologist_model,
        )

        apologist_history.append(
            apologist_text
        )

        builder.add_segment(
            apologist_text,
            "AI Christian Apologist",
            "Christian Apologist",
            topic,
            "#00FFCC",
        )

        # ----------------------------------------------------
        # Skeptic
        # ----------------------------------------------------

        skeptic_text = generate_skeptic(
            topic,
            round_num,
            apologist_text,
            skeptic_history[-1]
            if skeptic_history
            else None,
            skeptic_model,
        )

        skeptic_history.append(
            skeptic_text
        )

        builder.add_segment(
            skeptic_text,
            "AI Skeptic",
            "AI Skeptic",
            topic,
            "#FF00FF",
        )

        # ----------------------------------------------------
        # Judge
        # ----------------------------------------------------

        judge_results, avg_a, avg_b = run_judging_panel(
            judges,
            topic,
            round_num,
            apologist_text,
            skeptic_text,
        )

        cumulative_a += avg_a
        cumulative_b += avg_b

        # ----------------------------------------------------
        # Scoreboard
        # ----------------------------------------------------

        score_image = (
            f"score_bg_r{round_num}.png"
        )

        score_ui = (
            f"score_ui_r{round_num}.png"
        )

        score_audio = (
            f"score_r{round_num}.mp3"
        )

        score_ass = (
            f"score_r{round_num}.ass"
        )

        score_video = (
            f"score_vid_{round_num}.mp4"
        )

        generate_round_breakdown_image(
            round_num,
            judge_results,
            avg_a,
            avg_b,
            cumulative_a,
            cumulative_b,
            score_image,
        )

        score_narration = create_score_narration(
            round_num,
            len(judge_results),
            avg_a,
            avg_b,
            cumulative_a,
            cumulative_b,
        )

        generate_edge_audio_and_subs(
            score_narration,
            "Moderator",
            score_audio,
            score_ass,
        )

        card_x = create_ui_overlay(
            "Moderator",
            "Moderator",
            topic,
            "center",
            "#FFD700",
            score_ui,
        )

        render_video_segment(
            score_image,
            score_ui,
            score_audio,
            score_ass,
            score_video,
            "center",
            "#FFD700",
            card_x,
            zoom_bg=False,
        )

        builder.final_segments.append(
            score_video
        )

        # ----------------------------------------------------
        # Independent commentary
        #
        # Choose judges based on actual results.
        # Commentary explicitly receives the debate text so it
        # can avoid repeating phrases.
        # ----------------------------------------------------

        winner_pool_a = [
            j for j in judge_results
            if j["favored"] == "A"
        ]

        winner_pool_b = [
            j for j in judge_results
            if j["favored"] == "B"
        ]

        if winner_pool_a:
            judge_a = random.choice(
                winner_pool_a
            )
        else:
            judge_a = random.choice(
                judge_results
            )

        if winner_pool_b:
            judge_b = random.choice(
                winner_pool_b
            )
        else:
            judge_b = random.choice(
                judge_results
            )

        context_for_commentary = (
            f"Topic: {topic}\n"
            f"Apologist argument: {apologist_text}\n"
            f"Skeptic argument: {skeptic_text}\n"
        )

        commentary_a = generate_unique_commentary(
            topic,
            round_num,
            "A",
            judge_a["name"],
            context_for_commentary,
            judge_a["id"],
        )

        commentary_b = generate_unique_commentary(
            topic,
            round_num,
            "B",
            judge_b["name"],
            context_for_commentary,
            judge_b["id"],
        )

        builder.add_segment(
            commentary_a,
            "Panelist 1",
            "AI Judge",
            topic,
            "#3399FF",
        )

        builder.add_segment(
            commentary_b,
            "Panelist 2",
            "AI Judge",
            topic,
            "#3399FF",
        )

    # ========================================================
    # FINAL RESULT
    # ========================================================

    if cumulative_a > cumulative_b:
        winner = "Christian Apologist"

    elif cumulative_b > cumulative_a:
        winner = "AI Skeptic"

    else:
        winner = "Draw"

    final_text = (
        "The three rounds are complete. "
        f"The Christian Apologist finished with "
        f"{cumulative_a:.1f} cumulative points, "
        f"while the AI Skeptic finished with "
        f"{cumulative_b:.1f}. "
    )

    if winner == "Draw":

        final_text += (
            "The final result is a draw."
        )

    else:

        final_text += (
            f"The winner of this debate is the {winner}."
        )

    builder.add_segment(
        final_text,
        "Moderator",
        "Moderator",
        topic,
        "#FFD700",
    )

    # --------------------------------------------------------
    # Outro
    # --------------------------------------------------------

    builder.add_segment(
        "That concludes today's AI Debate Arena. "
        "The arguments have been presented and independently judged, "
        "but the final verdict is yours. "
        "Which side do you think made the stronger case? "
        "Let us know in the comments, and subscribe for the next debate.",
        "Moderator",
        "Moderator",
        topic,
        "#FFD700",
    )

    # ========================================================
    # CONCATENATE
    # ========================================================

    concat_file = "concat_list.txt"

    with open(
        concat_file,
        "w",
        encoding="utf-8",
    ) as f:

        for segment in builder.final_segments:

            absolute = os.path.abspath(
                segment
            ).replace("\\", "/")

            f.write(
                f"file '{absolute}'\n"
            )

    print(
        "\n🎞️ Stitching final video..."
    )

    result = subprocess.run(
        [
            "ffmpeg",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            concat_file,
            "-c",
            "copy",
            "-y",
            "final_debate_output.mp4",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    if result.returncode != 0:

        print(
            result.stderr[-5000:]
        )

        raise RuntimeError(
            "Final FFmpeg concatenation failed."
        )

    if not os.path.exists(
        "final_debate_output.mp4"
    ):
        raise RuntimeError(
            "Final video was not created."
        )

    print(
        "\n"
        "============================================\n"
        "✅ DEBATE COMPLETE\n"
        "============================================\n"
        f"Judges: {judge_count}\n"
        f"Apologist: {cumulative_a:.1f}\n"
        f"Skeptic: {cumulative_b:.1f}\n"
        f"Winner: {winner}\n"
        "Output: final_debate_output.mp4\n"
        "============================================"
    )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    try:
        run_debate_pipeline()

    except KeyboardInterrupt:

        print(
            "\n⛔ Pipeline cancelled."
        )

        sys.exit(1)

    except Exception as exc:

        print(
            "\n❌ PIPELINE FAILED:"
        )

        print(
            str(exc)
        )

        sys.exit(1)
