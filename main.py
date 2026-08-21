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

import edge_tts
from PIL import Image, ImageDraw, ImageFont, ImageFilter


# ============================================================
# AI DEBATE ARENA
# FULL TOPIC-ADAPTIVE VERSION
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

# Approximately 500 spoken words per side per round.
WORDS_PER_SIDE_PER_ROUND = 500

# Four back-and-forth exchanges per round.
TURNS_PER_SIDE_PER_ROUND = 4

WORDS_PER_TURN = 125

MIN_TURN_WORDS = 105
MAX_TURN_WORDS = 145


# ============================================================
# JUDGING
# ============================================================

# One model per provider/company.
MAX_JUDGES = 7
JUDGE_WORKERS = 7


# ============================================================
# VISUALS
# ============================================================

# Maximum automatically selected visual moments per speech segment.
MAX_VISUALS_PER_SEGMENT = 2

# Minimum gap between visual moments.
MIN_VISUAL_GAP = 2.0

# Visual cards stay above subtitles.
VISUAL_X = 700
VISUAL_Y = 525
VISUAL_W = 520
VISUAL_H = 245


# ============================================================
# TTS VOICES
# ============================================================

# Debate speakers.
VOICES = {
    "Moderator": "en-US-AndrewMultilingualNeural",

    "AI Christian Apologist":
        "en-US-BrianMultilingualNeural",

    "AI Skeptic":
        "en-US-AvaMultilingualNeural",

    # Separate judge voice.
    "AI Judge 1":
        "en-US-ChristopherNeural",

    "AI Judge 2":
        "en-US-EmmaMultilingualNeural",

    "AI Judge 3":
        "en-US-GuyNeural",

    "AI Judge 4":
        "en-US-JennyNeural",
}


JUDGE_VOICES = [
    "en-US-ChristopherNeural",
    "en-US-EmmaMultilingualNeural",
    "en-US-GuyNeural",
    "en-US-JennyNeural",
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

    return PROVIDER_ALIASES.get(
        prefix,
        prefix.replace("-", " ").title()
    )


# ============================================================
# CLEANUP
# ============================================================

def cleanup_cache():
    print("🧹 Cleaning temporary files...")

    patterns = [
        "*.mp4",
        "*.mp3",
        "*.ass",
        "*.png",
        "*.gif",
        "*_list.txt",
    ]

    protected = {
        OUTPUT_FILE,
        "background.png",
        "topic.txt",
    }

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
    return len(
        re.findall(
            r"\b[\w'-]+\b",
            text or ""
        )
    )


def clean_for_speech(text):
    text = re.sub(
        r"\([^)]*\)",
        "",
        text or ""
    )

    replacements = {
        "*": "",
        "#": "",
        "_": "",
        "`": "",
        "–": "-",
        "—": "-",
        '"': "",
        ":": " ",
        ";": " ",
        "&": "and",
    }

    for old, new in replacements.items():
        text = text.replace(old, new)

    text = re.sub(r"\s+", " ", text)

    return text.strip()


def clamp_score(value):
    try:
        value = float(value)
    except Exception:
        value = 50.0

    return max(
        0.0,
        min(100.0, value)
    )


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

    return (
        int(h[0:2], 16),
        int(h[2:4], 16),
        int(h[4:6], 16),
        alpha,
    )


# ============================================================
# OPENROUTER
# ============================================================

def openrouter_headers():

    return {
        "Authorization":
            f"Bearer {OPENROUTER_API_KEY}",

        "Content-Type":
            "application/json",

        "HTTP-Referer":
            "https://openrouter.ai/",

        "X-Title":
            "AI Debate Arena",
    }


def discover_models():

    if not OPENROUTER_API_KEY:
        raise RuntimeError(
            "OPENROUTER_API_KEY is missing."
        )

    try:

        response = requests.get(
            OPENROUTER_MODELS_URL,
            headers=openrouter_headers(),
            timeout=20,
        )

        if response.status_code != 200:
            print(
                f"⚠️ Model discovery returned "
                f"HTTP {response.status_code}"
            )
            return []

        data = response.json()

        models = []

        for item in data.get("data", []):

            model_id = item.get("id")

            if not model_id:
                continue

            lowered = model_id.lower()

            excluded = [
                "embed",
                "tts",
                "whisper",
                "audio",
                "image",
                "vision",
                "moderation",
                "guard",
            ]

            if any(x in lowered for x in excluded):
                continue

            models.append(model_id)

        return list(dict.fromkeys(models))

    except Exception as exc:

        print(
            f"⚠️ Model discovery failed: "
            f"{str(exc)[:200]}"
        )

        return []


def query_openrouter(
    prompt,
    model_id,
    timeout=60,
    max_tokens=1200,
    temperature=0.7,
):

    if not OPENROUTER_API_KEY:
        return None

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

    for attempt in range(3):

        try:

            response = requests.post(
                OPENROUTER_URL,
                headers=openrouter_headers(),
                json=payload,
                timeout=timeout,
            )

            if response.status_code == 200:

                data = response.json()

                choices = data.get(
                    "choices",
                    []
                )

                if choices:

                    content = (
                        choices[0]
                        .get("message", {})
                        .get("content", "")
                    )

                    if content and len(
                        content.strip()
                    ) > 10:

                        return content.strip()

            else:

                print(
                    f"⚠️ {provider_from_model(model_id)} "
                    f"returned HTTP "
                    f"{response.status_code}"
                )

        except Exception as exc:

            print(
                f"⚠️ Request failed for "
                f"{provider_from_model(model_id)}: "
                f"{str(exc)[:120]}"
            )

        if attempt < 2:
            time.sleep(
                1.5 * (attempt + 1)
            )

    return None


# ============================================================
# MODEL SELECTION
# ============================================================

def choose_primary_models(available_models):

    preference = [
        "openai/gpt-4o",
        "openai/gpt-4o-mini",
        "openai/gpt-4.1-mini",
        "anthropic/claude-3.5-sonnet",
        "anthropic/claude-3.5-haiku",
        "google/gemini-2.5-flash",
        "google/gemini-2.0-flash-001",
        "deepseek/deepseek-chat",
        "qwen/qwen-2.5-72b-instruct",
    ]

    found = [
        m for m in preference
        if m in set(available_models)
    ]

    if len(found) >= 2:
        return found[0], found[1]

    if len(found) == 1:

        remaining = [
            m for m in available_models
            if m != found[0]
        ]

        if remaining:
            return found[0], remaining[0]

    if len(available_models) >= 2:
        return (
            available_models[0],
            available_models[1],
        )

    return (
        FALLBACK_MODELS[0],
        FALLBACK_MODELS[1],
    )


def choose_judges(
    available_models,
    primary_models,
):

    excluded = set(primary_models)

    candidates = [
        m for m in available_models
        if m not in excluded
    ]

    groups = {}

    for model in candidates:

        provider = provider_from_model(model)

        groups.setdefault(
            provider,
            []
        ).append(model)

    preferred_keywords = [
        "gpt",
        "claude",
        "gemini",
        "grok",
        "deepseek",
        "mistral",
        "llama",
        "qwen",
        "command",
        "nemotron",
    ]

    selected = []

    for provider, models in groups.items():

        models.sort(
            key=lambda m: (
                0 if any(
                    k in m.lower()
                    for k in preferred_keywords
                ) else 1,
                len(m),
            )
        )

        selected.append(
            (provider, models[0])
        )

    priority = [
        "OpenAI",
        "Anthropic",
        "Google",
        "xAI",
        "DeepSeek",
        "Mistral",
        "Meta",
        "Alibaba / Qwen",
        "Cohere",
        "Perplexity",
    ]

    selected.sort(
        key=lambda x: (
            priority.index(x[0])
            if x[0] in priority
            else 999,
            x[0],
        )
    )

    return [
        model
        for _, model in selected[:MAX_JUDGES]
    ]


# ============================================================
# DEBATE GENERATION
# ============================================================

def generate_turn(
    side,
    topic,
    round_num,
    turn_num,
    previous_exchange,
    model,
):

    if side == "A":
        side_name = "AI Christian Apologist"
        opponent = "AI Skeptic"
    else:
        side_name = "AI Skeptic"
        opponent = "AI Christian Apologist"

    if round_num == 1 and turn_num == 1:

        instruction = """
This is the opening exchange.
Establish a strong foundation without trying
to deliver the entire debate at once.
"""

    else:

        instruction = f"""
This is turn {turn_num} of round {round_num}.

Respond directly to the immediately preceding
argument.

Do not restart the debate.
Do not introduce yourself.
Do not explain your task.
Do not say "in this round".
Do not recycle previous arguments.
Add a genuinely useful new point.
"""

    prompt = f"""
You are the {side_name} in a serious public debate.

Topic:
{topic}

Opponent:
{opponent}

{instruction}

Previous exchange:
{previous_exchange or "None - opening exchange."}

Write ONLY your spoken contribution.

Target approximately {WORDS_PER_TURN} words.

Aim for {MIN_TURN_WORDS}-{MAX_TURN_WORDS} words.

Use natural conversational speech.
Suitable for a general YouTube audience.

Be specific.
Use examples or analogies when useful.

No headings.
No numbered lists.
No bullet points.
No meta commentary.
Do not mention AI models.
Do not mention companies.
"""

    response = query_openrouter(
        prompt,
        model,
        max_tokens=430,
        temperature=0.78,
    )

    if response:
        return response

    return (
        "The important question is not simply "
        "whether a conclusion sounds plausible. "
        "We need to ask whether the evidence "
        "actually supports it, what assumptions "
        "are being made, and whether alternative "
        "explanations have been properly considered."
    )


def build_round_exchanges(
    topic,
    round_num,
    apologist_model,
    skeptic_model,
    previous_history,
):

    apologist_turns = []
    skeptic_turns = []

    exchange_history = previous_history

    for turn_num in range(
        1,
        TURNS_PER_SIDE_PER_ROUND + 1
    ):

        apologist = generate_turn(
            "A",
            topic,
            round_num,
            turn_num,
            exchange_history,
            apologist_model,
        )

        apologist_turns.append(
            apologist
        )

        exchange_history = (
            "AI Christian Apologist:\n"
            + apologist
            + "\n\n"
        )

        skeptic = generate_turn(
            "B",
            topic,
            round_num,
            turn_num,
            exchange_history,
            skeptic_model,
        )

        skeptic_turns.append(
            skeptic
        )

        exchange_history += (
            "AI Skeptic:\n"
            + skeptic
            + "\n\n"
        )

    return (
        apologist_turns,
        skeptic_turns,
        exchange_history,
    )


# ============================================================
# JUDGING
# ============================================================

def neutral_judge(model):

    return {
        "model": model,
        "provider": provider_from_model(model),

        "A_argument": 50,
        "A_rebuttal": 50,
        "A_clarity": 50,
        "A_total": 50,

        "B_argument": 50,
        "B_rebuttal": 50,
        "B_clarity": 50,
        "B_total": 50,

        "winner": "A",
    }


def judge_round(
    model,
    topic,
    round_num,
    apologist,
    skeptic,
):

    prompt = f"""
You are an independent and impartial debate judge.

Topic:
{topic}

Round:
{round_num}

SIDE A — AI CHRISTIAN APOLOGIST:
{apologist}

SIDE B — AI SKEPTIC:
{skeptic}

Evaluate both sides independently.

Score:

1. Argument strength
2. Rebuttal quality
3. Clarity and reasoning

Score every category from 0 to 100.

Return ONLY valid JSON:

{{
"A_argument": 0,
"A_rebuttal": 0,
"A_clarity": 0,
"A_total": 0,
"B_argument": 0,
"B_rebuttal": 0,
"B_clarity": 0,
"B_total": 0
}}
"""

    response = query_openrouter(
        prompt,
        model,
        timeout=35,
        max_tokens=250,
        temperature=0.1,
    )

    if not response:
        return neutral_judge(model)

    try:

        match = re.search(
            r"\{.*\}",
            response,
            re.DOTALL,
        )

        if not match:
            return neutral_judge(model)

        data = json.loads(
            match.group(0)
        )

        aa = clamp_score(
            data.get("A_argument", 50)
        )

        ar = clamp_score(
            data.get("A_rebuttal", 50)
        )

        ac = clamp_score(
            data.get("A_clarity", 50)
        )

        ba = clamp_score(
            data.get("B_argument", 50)
        )

        br = clamp_score(
            data.get("B_rebuttal", 50)
        )

        bc = clamp_score(
            data.get("B_clarity", 50)
        )

        at = (aa + ar + ac) / 3
        bt = (ba + br + bc) / 3

        return {
            "model": model,
            "provider": provider_from_model(model),

            "A_argument": aa,
            "A_rebuttal": ar,
            "A_clarity": ac,
            "A_total": round(at, 2),

            "B_argument": ba,
            "B_rebuttal": br,
            "B_clarity": bc,
            "B_total": round(bt, 2),

            "winner": "A" if at > bt else "B",
        }

    except Exception:
        return neutral_judge(model)


def evaluate_round(
    judges,
    topic,
    round_num,
    apologist,
    skeptic,
):

    results = []

    print(
        f"⚖️ Asking {len(judges)} independent AI judges..."
    )

    def worker(model):
        return judge_round(
            model,
            topic,
            round_num,
            apologist,
            skeptic,
        )

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=max(1, min(
            JUDGE_WORKERS,
            len(judges)
        ))
    ) as executor:

        futures = {
            executor.submit(
                worker,
                model
            ): model
            for model in judges
        }

        completed = 0

        for future in concurrent.futures.as_completed(
            futures
        ):

            model = futures[future]

            try:

                result = future.result()

                results.append(result)

                completed += 1

                print(
                    f"   ✓ Judge "
                    f"{completed}/{len(judges)} "
                    f"— {result['provider']}"
                )

            except Exception as exc:

                print(
                    f"   ✗ Judge failed "
                    f"{provider_from_model(model)}: "
                    f"{str(exc)[:100]}"
                )

    if not results:
        results = [
            neutral_judge(
                "fallback/fallback"
            )
        ]

    return results


def calculate_round_average(results):

    a = sum(
        r["A_total"]
        for r in results
    ) / len(results)

    b = sum(
        r["B_total"]
        for r in results
    ) / len(results)

    return round(a, 2), round(b, 2)


# ============================================================
# TTS
# ============================================================

async def generate_audio_async(
    text,
    voice,
    filename,
):

    communicate = edge_tts.Communicate(
        text,
        voice,
        rate="+0%",
        volume="+0%",
    )

    audio = b""
    words = []

    async for chunk in communicate.stream():

        if chunk["type"] == "audio":

            audio += chunk["data"]

        elif chunk["type"] == "WordBoundary":

            start = (
                chunk["offset"]
                / 10_000_000
            )

            duration = (
                chunk["duration"]
                / 10_000_000
            )

            words.append({
                "text": chunk["text"],
                "start": start,
                "duration": duration,
                "end": start + duration,
            })

    with open(
        filename,
        "wb"
    ) as file:

        file.write(audio)

    return words


def generate_audio(
    text,
    role,
    filename,
    judge_voice_index=None,
):

    if role == "AI Judge":

        if judge_voice_index is None:
            judge_voice_index = 0

        voice = JUDGE_VOICES[
            judge_voice_index
            % len(JUDGE_VOICES)
        ]

    else:

        voice = VOICES.get(
            role,
            VOICES["Moderator"]
        )

    clean_text = clean_for_speech(text)

    try:

        return asyncio.run(
            generate_audio_async(
                clean_text,
                voice,
                filename,
            )
        )

    except Exception as exc:

        print(
            f"⚠️ TTS failed using "
            f"{voice}: {str(exc)[:150]}"
        )

        return asyncio.run(
            generate_audio_async(
                clean_text,
                VOICES["Moderator"],
                filename,
            )
        )


# ============================================================
# SUBTITLES - CHUNKS/PARAGRAPHS
# ============================================================

def format_ass_time(seconds):

    seconds = max(
        0.0,
        float(seconds)
    )

    hours = int(
        seconds // 3600
    )

    minutes = int(
        (seconds % 3600) // 60
    )

    secs = seconds % 60

    return (
        f"{hours}:"
        f"{minutes:02d}:"
        f"{secs:05.2f}"
    )


def ass_escape(text):

    text = str(text)

    text = text.replace(
        "\\",
        "\\\\"
    )

    text = text.replace(
        "{",
        "\\{"
    )

    text = text.replace(
        "}",
        "\\}"
    )

    text = text.replace(
        "\n",
        " "
    )

    return text


def generate_subtitles(
    words,
    filename,
    scorecard=False,
):
    # Using larger margin constraints effectively groups text into paragraph blocks
    margin_v = 75 if scorecard else 100

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: DebateSub,DejaVu Sans,38,&H00FFFFFF,&H00FFFFFF,&H00000000,&HCC000000,1,0,0,0,100,100,0,0,1,3,1,2,300,300,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    if not words:
        with open(filename, "w", encoding="utf-8") as file:
            file.write(header)
        return

    events = []
    chunks = []
    current_chunk = []

    # Chunk the words into paragraphs based on sentence boundaries
    for w in words:
        current_chunk.append(w)
        text_clean = str(w["text"]).strip()
        is_boundary = text_clean.endswith(('.', '?', '!', ':', ';'))
        
        # Break into a chunk if we've accumulated ~20 words AND hit punctuation,
        # OR if the block is dragging on over 40 words.
        if (len(current_chunk) >= 18 and is_boundary) or len(current_chunk) >= 40:
            chunks.append(current_chunk)
            current_chunk = []

    if current_chunk:
        chunks.append(current_chunk)

    for chunk in chunks:
        if not chunk:
            continue

        start = float(chunk[0]["start"])
        # Hold the paragraph slightly past the spoken audio for reading comfort
        end = float(chunk[-1]["end"]) + 0.35 

        # Fade in and out (250ms) so text isn't a jarring "still" cut
        subtitle_text = "{\\fad(250,250)}" + " ".join(
            ass_escape(w["text"]) for w in chunk
        )

        events.append(
            "Dialogue: 0,"
            + format_ass_time(start)
            + ","
            + format_ass_time(end)
            + ",DebateSub,,0,0,0,,"
            + subtitle_text
        )

    with open(filename, "w", encoding="utf-8") as file:
        file.write(header + "\n".join(events) + "\n")


# ============================================================
# TOPIC-ADAPTIVE VISUAL PLANNER
# ============================================================

def plan_visuals(
    text,
    model,
):

    """
    Ask the AI to identify useful visual moments.

    IMPORTANT:
    This does not control the video pipeline.
    If it fails, the video simply continues without visuals.
    """

    prompt = f"""
You are a visual director for a YouTube debate.

Read this spoken section:

{text}

Identify up to {MAX_VISUALS_PER_SEGMENT}
important moments that would benefit from a simple
visual illustration appearing temporarily on screen.

Do NOT choose generic concepts merely because they are
mentioned.

Choose moments where a visual would genuinely help viewers
understand an argument, example, story, historical event,
object, process, place or analogy.

Return ONLY valid JSON in this format:

[
  {{
    "phrase": "short exact phrase from the speech",
    "label": "SHORT 2-5 WORD LABEL",
    "description": "simple visual description",
    "kind": "person|place|object|process|concept|history|comparison"
  }}
]

Rules:

- The phrase MUST actually appear in the supplied speech.
- Use exact wording where possible.
- Maximum {MAX_VISUALS_PER_SEGMENT} items.
- Do not choose subtitles or conclusions.
- Do not create visuals merely for decorative purposes.
"""

    response = query_openrouter(
        prompt,
        model,
        timeout=35,
        max_tokens=500,
        temperature=0.2,
    )

    if not response:
        return []

    try:

        match = re.search(
            r"\[.*\]",
            response,
            re.DOTALL
        )

        if not match:
            return []

        data = json.loads(
            match.group(0)
        )

        if not isinstance(data, list):
            return []

        output = []

        for item in data:

            if not isinstance(item, dict):
                continue

            phrase = str(
                item.get("phrase", "")
            ).strip()

            label = str(
                item.get("label", "")
            ).strip()

            description = str(
                item.get("description", "")
            ).strip()

            kind = str(
                item.get(
                    "kind",
                    "concept"
                )
            ).strip().lower()

            if not phrase or not label:
                continue

            if phrase.lower() not in text.lower():
                continue

            output.append({
                "phrase": phrase,
                "label": label[:35],
                "description": description[:180],
                "kind": kind,
            })

            if len(output) >= MAX_VISUALS_PER_SEGMENT:
                break

        return output

    except Exception:
        return []


def find_phrase_timing(
    phrase,
    words,
):

    if not phrase or not words:
        return None

    phrase_words = re.findall(
        r"\b[\w'-]+\b",
        phrase.lower()
    )

    source_words = [
        re.sub(
            r"[^\w'-]",
            "",
            str(w["text"]).lower()
        )
        for w in words
    ]

    phrase_words = [
        x for x in phrase_words
        if x
    ]

    if not phrase_words:
        return None

    # Exact contiguous word matching.
    for i in range(
        0,
        len(source_words) - len(phrase_words) + 1
    ):

        if source_words[
            i:i + len(phrase_words)
        ] == phrase_words:

            start = float(
                words[i]["start"]
            )

            end_index = min(
                len(words) - 1,
                i + len(phrase_words) - 1
            )

            end = float(
                words[end_index]["end"]
            ) + 2.5

            return {
                "start": max(
                    0.0,
                    start - 0.15
                ),
                "end": max(
                    start + 2.5,
                    end
                ),
            }

    # Fuzzy fallback: use first distinctive word.
    for phrase_word in phrase_words:

        if len(phrase_word) < 4:
            continue

        for index, source_word in enumerate(
            source_words
        ):

            if phrase_word == source_word:

                start = float(
                    words[index]["start"]
                )

                end_index = min(
                    len(words) - 1,
                    index + 12
                )

                end = float(
                    words[end_index]["end"]
                ) + 1.5

                return {
                    "start": start,
                    "end": end,
                }

    return None


def create_visual_plan(
    text,
    words,
    model,
):

    if not words:
        return []

    candidates = plan_visuals(
        text,
        model
    )

    if not candidates:
        return []

    timed = []

    for item in candidates:

        timing = find_phrase_timing(
            item["phrase"],
            words
        )

        if not timing:
            continue

        item = dict(item)

        item.update(timing)

        timed.append(item)

    timed.sort(
        key=lambda x: x["start"]
    )

    output = []

    for item in timed:

        if any(
            abs(
                item["start"]
                -
                previous["start"]
            ) < MIN_VISUAL_GAP
            for previous in output
        ):
            continue

        output.append(item)

        if len(output) >= MAX_VISUALS_PER_SEGMENT:
            break

    return output


# ============================================================
# DYNAMIC ANIMATED VISUAL CARD
# ============================================================

def draw_animated_illustration(draw, kind, progress):
    """
    Draws the visual illustration dynamically based on a progress value (0.0 to 1.0).
    """
    p = progress

    if kind == "person":
        bob = int(8 * p)
        draw.ellipse((85, 35 + bob, 175, 125 + bob), fill=(235, 190, 150, 255))
        draw.line((130, 125 + bob, 130, 205), fill=(70, 145, 220, 255), width=25)
        arm_y = 185 - int(15 * p)
        draw.line((130, 145 + bob, 70, arm_y), fill=(70, 145, 220, 255), width=12)
        draw.line((130, 145 + bob, 190, arm_y), fill=(70, 145, 220, 255), width=12)
        draw.line((130, 200, 85, 230), fill=(70, 145, 220, 255), width=12)
        draw.line((130, 200, 175, 230), fill=(70, 145, 220, 255), width=12)

    elif kind == "place":
        shift = int(10 * p)
        draw.rectangle((55, 115, 205, 205), fill=(115, 80, 50, 255))
        draw.polygon([(40, 120), (130, 45 - shift), (220, 120)], fill=(160, 60, 55, 255))
        draw.rectangle((110, 155, 150, 205), fill=(55, 45, 40, 255))

    elif kind == "object":
        hover = int(12 * p)
        draw.rounded_rectangle(
            (55, 55 - hover, 205, 205 - hover), radius=25,
            fill=(55, 105, 180, 255), outline=(255, 215, 0, 255), width=4
        )
        shadow_w = int(100 - 20 * p)
        draw.ellipse((130 - shadow_w//2, 215, 130 + shadow_w//2, 225), fill=(0, 0, 0, 100))
        draw.line((80, 130 - hover, 180, 130 - hover), fill="white", width=8)

    elif kind == "process":
        node_x = int(20 * p)
        draw.ellipse((45 + node_x, 70, 115 + node_x, 140), fill=(60, 145, 220, 255))
        draw.ellipse((145 - node_x, 70, 215 - node_x, 140), fill=(80, 180, 100, 255))
        draw.line((115 + node_x, 105, 145 - node_x, 105), fill=(255, 215, 0, 255), width=12)
        draw.polygon([(145 - node_x, 105), (125 - node_x, 90), (125 - node_x, 120)], fill=(255, 215, 0, 255))
        draw.ellipse((45, 160, 215, 220), fill=(130, 75, 170, 255))

    elif kind == "history":
        width_ext = int(40 * p)
        draw.rectangle((60, 60, 200, 205), fill=(110, 75, 45, 255), outline=(255, 215, 0, 255), width=4)
        draw.line((80, 95, 140 + width_ext, 95), fill="white", width=6)
        draw.line((80, 130, 180 - int(20 * p), 130), fill="white", width=6)
        draw.line((80, 165, 120 + width_ext, 165), fill="white", width=6)

    elif kind == "comparison":
        bar1 = int(30 * p)
        bar2 = int(30 * (1 - p))
        draw.rectangle((45, 75 - bar1, 105, 190), fill=(50, 150, 210, 255))
        draw.rectangle((155, 45 + bar2, 215, 190), fill=(210, 90, 100, 255))
        draw.line((130, 45, 130, 205), fill=(255, 215, 0, 255), width=5)

    else:
        pulse = int(10 * p)
        draw.ellipse(
            (55 - pulse, 45 - pulse, 205 + pulse, 195 + pulse),
            fill=(55, 65, 105, 255), outline=(255, 215, 0, 255), width=5
        )
        font = load_font(90 + pulse, bold=True)
        draw.text((105 - pulse//2, 58 - pulse//2), "?", fill="white", font=font)


def create_visual_asset(visual, index):
    """
    Generates a smoothly looping animated GIF asset rather than a static image.
    """
    filename = f"visual_{index}.gif"
    
    frames = []
    num_frames = 30
    
    label_font = load_font(27, bold=True)
    desc_font = load_font(17)
    
    label = visual.get("label", "KEY IDEA").upper()
    description = visual.get("description", "")
    
    # Calculate simple word wrapping utilizing a dummy image
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
        
    for f in range(num_frames):
        image = Image.new("RGBA", (VISUAL_W, VISUAL_H), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        
        # Solid background avoids GIF transparency bugs
        draw.rounded_rectangle(
            (4, 4, VISUAL_W - 4, VISUAL_H - 4),
            radius=28,
            fill=(12, 18, 35, 255),
            outline=(255, 215, 0, 255),
            width=4
        )
        
        # Sine wave mapping for smooth continuous looping
        progress = math.sin(math.pi * (f / num_frames))
        
        draw_animated_illustration(draw, visual.get("kind", "concept"), progress)
        
        draw.text((230, 48), label, fill="white", font=label_font)
        
        for line_index, line in enumerate(lines[:5]):
            draw.text(
                (230, 95 + line_index * 27),
                line,
                fill=(215, 220, 235, 255),
                font=desc_font
            )
            
        frames.append(image)
        
    frames[0].save(
        filename,
        format='GIF',
        save_all=True,
        append_images=frames[1:],
        duration=33, # ~30fps 
        loop=0,
        disposal=2
    )
    
    return filename


# ============================================================
# BACKGROUND
# ============================================================

def create_background(
    position,
    glow_color,
    filename,
):

    source = os.path.join(
        os.path.dirname(
            os.path.abspath(__file__)
        ),
        "background.png"
    )

    if os.path.exists(source):

        try:

            image = (
                Image.open(source)
                .convert("RGB")
                .resize(
                    (VIDEO_W, VIDEO_H)
                )
            )

        except Exception:

            image = Image.new(
                "RGB",
                (VIDEO_W, VIDEO_H),
                (12, 16, 32)
            )

    else:

        image = Image.new(
            "RGB",
            (VIDEO_W, VIDEO_H),
            (12, 16, 32)
        )

        draw = ImageDraw.Draw(
            image
        )

        for x in range(
            0,
            VIDEO_W,
            60
        ):

            draw.line(
                [(x, 0), (x, VIDEO_H)],
                fill=(20, 26, 45),
                width=2
            )

        for y in range(
            0,
            VIDEO_H,
            60
        ):

            draw.line(
                [(0, y), (VIDEO_W, y)],
                fill=(20, 26, 45),
                width=2
            )

    overlay = Image.new(
        "RGBA",
        (VIDEO_W, VIDEO_H),
        (0, 0, 0, 0)
    )

    draw = ImageDraw.Draw(
        overlay
    )

    if position == "left":
        cx = 400
    elif position == "right":
        cx = 1520
    else:
        cx = 960

    for radius in range(
        700,
        50,
        -50
    ):

        alpha = int(
            15 * (
                1 - radius / 700
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
                alpha
            )
        )

    overlay = overlay.filter(
        ImageFilter.GaussianBlur(30)
    )

    result = Image.alpha_composite(
        image.convert("RGBA"),
        overlay
    ).convert("RGB")

    result.save(filename)


# ============================================================
# SPEAKER CARD
# ============================================================

def create_ui_overlay(
    speaker_name,
    topic,
    position,
    glow_color,
    filename,
):

    image = Image.new(
        "RGBA",
        (VIDEO_W, VIDEO_H),
        (0, 0, 0, 0)
    )

    draw = ImageDraw.Draw(
        image
    )

    title_font = load_font(
        30,
        bold=True
    )

    name_font = load_font(
        30,
        bold=True
    )

    # Topic at top.
    title = f"TOPIC: {topic}"

    box = draw.textbbox(
        (0, 0),
        title,
        font=title_font
    )

    width = box[2] - box[0]

    draw.text(
        (
            (VIDEO_W - width) // 2,
            24
        ),
        title,
        fill="white",
        font=title_font
    )

    # One identity line only.
    card_width = 650
    card_height = 110
    card_y = 885

    if position == "left":
        card_x = 75
    elif position == "right":
        card_x = 1195
    else:
        card_x = (
            VIDEO_W - card_width
        ) // 2

    draw.rounded_rectangle(
        [
            card_x,
            card_y,
            card_x + card_width,
            card_y + card_height
        ],
        radius=18,
        fill=(18, 26, 46, 235),
        outline=glow_color,
        width=4
    )

    draw.ellipse(
        [
            card_x + 22,
            card_y + 27,
            card_x + 47,
            card_y + 52
        ],
        fill=glow_color
    )

    draw.text(
        (
            card_x + 65,
            card_y + 22
        ),
        speaker_name,
        fill="white",
        font=name_font
    )

    image.save(filename)

    return card_x, card_y


# ============================================================
# SCORECARD IMAGE
# ============================================================

def generate_scoreboard(
    round_num,
    results,
    round_a,
    round_b,
    cumulative_a,
    cumulative_b,
    filename,
):

    source = os.path.join(
        os.path.dirname(
            os.path.abspath(__file__)
        ),
        "background.png"
    )

    if os.path.exists(source):

        try:

            image = (
                Image.open(source)
                .convert("RGB")
                .resize(
                    (VIDEO_W, VIDEO_H)
                )
            )

        except Exception:

            image = Image.new(
                "RGB",
                (VIDEO_W, VIDEO_H),
                (12, 16, 32)
            )

    else:

        image = Image.new(
            "RGB",
            (VIDEO_W, VIDEO_H),
            (12, 16, 32)
        )

    # Dark static background.
    overlay = Image.new(
        "RGBA",
        (VIDEO_W, VIDEO_H),
        (0, 0, 0, 235)
    )

    image = Image.alpha_composite(
        image.convert("RGBA"),
        overlay
    ).convert("RGB")

    draw = ImageDraw.Draw(
        image
    )

    header = load_font(
        38,
        bold=True
    )

    sub = load_font(
        22,
        bold=True
    )

    small = load_font(
        20
    )

    def centred(
        y,
        text,
        font,
        fill,
    ):

        box = draw.textbbox(
            (0, 0),
            text,
            font=font
        )

        width = box[2] - box[0]

        draw.text(
            (
                (VIDEO_W - width) // 2,
                y
            ),
            text,
            fill=fill,
            font=font
        )

    judge_count = len(results)

    centred(
        24,
        f"ROUND {round_num} — AI JUDGING PANEL",
        header,
        "#FFD700"
    )

    centred(
        72,
        f"{judge_count} INDEPENDENT JUDGES",
        sub,
        "white"
    )

    centred(
        112,
        (
            f"ROUND SCORE   "
            f"APOLOGIST {round_a:.1f}   "
            f"VS   "
            f"SKEPTIC {round_b:.1f}"
        ),
        sub,
        "white"
    )

    centred(
        150,
        (
            f"CUMULATIVE   "
            f"APOLOGIST {cumulative_a:.1f}   "
            f"VS   "
            f"SKEPTIC {cumulative_b:.1f}"
        ),
        sub,
        "#FFD700"
    )

    # Left panel.
    draw.text(
        (100, 225),
        "CATEGORY AVERAGES",
        fill="#FFD700",
        font=sub
    )

    draw.text(
        (500, 265),
        "APOLOGIST",
        fill="#00FFCC",
        font=small
    )

    draw.text(
        (680, 265),
        "SKEPTIC",
        fill="#FF66FF",
        font=small
    )

    categories = [
        (
            "Argument strength",
            "A_argument",
            "B_argument"
        ),
        (
            "Rebuttal quality",
            "A_rebuttal",
            "B_rebuttal"
        ),
        (
            "Clarity & reasoning",
            "A_clarity",
            "B_clarity"
        ),
    ]

    y = 310

    for label, a_key, b_key in categories:

        a = sum(
            r[a_key]
            for r in results
        ) / judge_count

        b = sum(
            r[b_key]
            for r in results
        ) / judge_count

        draw.text(
            (100, y),
            label,
            fill="white",
            font=small
        )

        draw.text(
            (500, y),
            f"{a:.1f}",
            fill="#00FFCC",
            font=small
        )

        draw.text(
            (680, y),
            f"{b:.1f}",
            fill="#FF66FF",
            font=small
        )

        y += 48

    # Right panel.
    draw.text(
        (980, 225),
        "INDIVIDUAL JUDGES",
        fill="#FFD700",
        font=sub
    )

    draw.text(
        (980, 270),
        "PROVIDER",
        fill="white",
        font=small
    )

    draw.text(
        (1500, 270),
        "A",
        fill="#00FFCC",
        font=small
    )

    draw.text(
        (1580, 270),
        "B",
        fill="#FF66FF",
        font=small
    )

    draw.line(
        [(970, 300), (1680, 300)],
        fill=(100, 110, 140, 255),
        width=2
    )

    # Seven judges maximum.
    row_height = 48
    start_y = 320

    for index, result in enumerate(results):

        row_y = (
            start_y
            + index * row_height
        )

        provider = result.get(
            "provider",
            "Unknown"
        )

        if len(provider) > 28:
            provider = (
                provider[:25]
                + "..."
            )

        draw.text(
            (980, row_y),
            provider,
            fill="white",
            font=small
        )

        draw.text(
            (1500, row_y),
            f"{result['A_total']:.1f}",
            fill="#00FFCC",
            font=small
        )

        draw.text(
            (1580, row_y),
            f"{result['B_total']:.1f}",
            fill="#FF66FF",
            font=small
        )

    # Important:
    # No topic title, speaker card or zooming occurs here.
    image.save(filename)


# ============================================================
# FFMPEG PATH
# ============================================================

def ffmpeg_filter_path(filename):

    path = os.path.abspath(
        filename
    )

    path = path.replace(
        "\\",
        "/"
    )

    path = path.replace(
        "'",
        r"\'"
    )

    path = path.replace(
        ":",
        r"\:"
    )

    return path


# ============================================================
# VIDEO SEGMENT
# ============================================================

def render_video_segment(
    background,
    ui,
    audio,
    subtitles,
    output,
    position,
    glow_color,
    card_x,
    card_y,
    visual_plan,
):

    required = [
        background,
        ui,
        audio,
        subtitles,
    ]

    for path in required:

        if not os.path.exists(path):

            raise FileNotFoundError(
                f"Required file missing: "
                f"{os.path.abspath(path)}"
            )

    visual_assets = []

    for index, visual in enumerate(
        visual_plan or []
    ):

        try:

            asset = create_visual_asset(
                visual,
                index
            )

            visual_assets.append(
                (asset, visual)
            )

        except Exception as exc:

            print(
                f"⚠️ Visual creation skipped: "
                f"{str(exc)[:100]}"
            )

    glow = glow_color.lstrip("#")

    if position == "left":
        pan_x = "0"
    elif position == "right":
        pan_x = "iw-(iw/zoom)"
    else:
        pan_x = "(iw-(iw/zoom))/2"

    filter_parts = []

    filter_parts.append(
        "[0:v]"
        "scale=1920:1080,"
        "zoompan="
        "z='min(zoom+0.00020,1.05)':"
        f"x='{pan_x}':"
        "y='(ih-(ih/zoom))/2':"
        "d=9000:"
        "s=1920x1080:"
        "fps=30"
        "[bg];"
    )

    filter_parts.append(
        "[1:v]"
        "scale=1920:1080"
        "[ui];"
    )

    filter_parts.append(
        "[2:a]"
        "showwaves="
        "s=300x58:"
        "mode=cline:"
        f"colors=0x{glow}:"
        "rate=30"
        "[wave];"
    )

    filter_parts.append(
        "[bg][ui]"
        "overlay=0:0"
        "[base];"
    )

    wave_x = card_x + 330
    wave_y = card_y + 47

    filter_parts.append(
        "[base][wave]"
        f"overlay={wave_x}:{wave_y}"
        "[withwave];"
    )

    current = "[withwave]"
    input_index = 3

    for index, (asset, visual) in enumerate(visual_assets):
        label = f"visual{index}"

        start = max(0.0, float(visual["start"]))
        end = max(start + 2.0, float(visual["end"]))
        
        # Apply fade in and out to the alpha channel of the visual card
        filter_parts.append(
            f"[{input_index}:v]"
            f"format=rgba,"
            f"fade=t=in:st={start}:d=0.4:alpha=1,"
            f"fade=t=out:st={end-0.4}:d=0.4:alpha=1"
            f"[{label}_faded];"
        )

        x = (VIDEO_W - VISUAL_W) // 2
        
        # Dynamic float animation: Start low, slowly drift upwards while visible
        drift_speed = 15 # pixels per second
        y_expr = f"{VISUAL_Y} + 20 - (t-{start})*{drift_speed}"
        
        enable = f"between(t,{start:.2f},{end:.2f})"

        filter_parts.append(
            f"{current}"
            f"[{label}_faded]"
            f"overlay={x}:'{y_expr}':"
            f"enable='{enable}'"
            f"[v{index}];"
        )

        current = f"[v{index}]"
        input_index += 1

    subtitle_path = ffmpeg_filter_path(
        subtitles
    )

    # Subtitles are burned LAST.
    filter_parts.append(
        f"{current}"
        f"ass='{subtitle_path}'"
        "[outv]"
    )

    filter_complex = "".join(
        filter_parts
    )

    command = [
        "ffmpeg",
        "-y",

        "-loop",
        "1",
        "-framerate",
        str(FPS),
        "-i",
        background,

        "-i",
        ui,

        "-i",
        audio,
    ]

    for asset, _ in visual_assets:
        if asset.endswith(".gif"):
            command += [
                "-ignore_loop",
                "0",
                "-i",
                asset,
            ]
        else:
            command += [
                "-loop",
                "1",
                "-i",
                asset,
            ]

    command += [
        "-filter_complex",
        filter_complex,

        "-map",
        "[outv]",

        "-map",
        "2:a",

        "-c:v",
        "libx264",

        "-preset",
        "veryfast",

        "-crf",
        "20",

        "-pix_fmt",
        "yuv420p",

        "-c:a",
        "aac",

        "-b:a",
        "192k",

        "-shortest",

        output,
    ]

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    if result.returncode != 0:

        print(
            "\n❌ FFmpeg failed:"
        )

        print(
            result.stderr[-7000:]
        )

        raise RuntimeError(
            f"FFmpeg failed creating {output}"
        )

    for asset, _ in visual_assets:
        try:
            os.remove(asset)
        except Exception:
            pass


# ============================================================
# SCORECARD VIDEO
# ============================================================

def render_scorecard_video(
    scorecard,
    audio,
    subtitles,
    output,
):

    for path in [
        scorecard,
        audio,
        subtitles,
    ]:

        if not os.path.exists(path):

            raise FileNotFoundError(
                f"Scorecard file missing: "
                f"{os.path.abspath(path)}"
            )

    subtitle_path = ffmpeg_filter_path(
        subtitles
    )

    filter_complex = (
        "[0:v]"
        "scale=1920:1080"
        "[base];"
        f"[base]ass='{subtitle_path}'"
        "[outv]"
    )

    command = [
        "ffmpeg",
        "-y",

        "-loop",
        "1",

        "-framerate",
        str(FPS),

        "-i",
        scorecard,

        "-i",
        audio,

        "-filter_complex",
        filter_complex,

        "-map",
        "[outv]",

        "-map",
        "1:a",

        "-c:v",
        "libx264",

        "-preset",
        "veryfast",

        "-crf",
        "20",

        "-pix_fmt",
        "yuv420p",

        "-c:a",
        "aac",

        "-b:a",
        "192k",

        "-shortest",

        output,
    ]

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    if result.returncode != 0:

        print(
            "\n❌ Scorecard FFmpeg failed:"
        )

        print(
            result.stderr[-7000:]
        )

        raise RuntimeError(
            "Scorecard rendering failed."
        )


# ============================================================
# SEGMENT CREATION
# ============================================================

def create_segment(
    text,
    role,
    speaker_name,
    topic,
    segment_id,
    model_for_visuals,
    position=None,
    glow=None,
    judge_voice_index=None,
):

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

    audio_file = (
        f"audio_{segment_id}.mp3"
    )

    subtitle_file = (
        f"subs_{segment_id}.ass"
    )

    background_file = (
        f"bg_{segment_id}.png"
    )

    ui_file = (
        f"ui_{segment_id}.png"
    )

    video_file = (
        f"segment_{segment_id}.mp4"
    )

    words = generate_audio(
        text,
        role,
        audio_file,
        judge_voice_index
    )

    generate_subtitles(
        words,
        subtitle_file
    )

    visual_plan = []

    try:

        visual_plan = create_visual_plan(
            clean_for_speech(text),
            words,
            model_for_visuals
        )

        if visual_plan:

            print(
                f"   🎨 {len(visual_plan)} "
                f"adaptive visual cue(s)"
            )

    except Exception as exc:

        print(
            f"⚠️ Visual planning skipped: "
            f"{str(exc)[:120]}"
        )

    create_background(
        position,
        glow,
        background_file
    )

    card_x, card_y = create_ui_overlay(
        speaker_name,
        topic,
        position,
        glow,
        ui_file
    )

    render_video_segment(
        background_file,
        ui_file,
        audio_file,
        subtitle_file,
        video_file,
        position,
        glow,
        card_x,
        card_y,
        visual_plan
    )

    return video_file


# ============================================================
# PANEL COMMENTARY
# ============================================================

def generate_panel_commentary(
    model,
    side,
    topic,
    round_num,
    apologist,
    skeptic,
    previous_comments,
):

    provider = provider_from_model(model)

    preferred_side = (
        "AI Christian Apologist"
        if side == "A"
        else
        "AI Skeptic"
    )

    recent = "\n".join(
        previous_comments[-6:]
    )

    prompt = f"""
You are an independent AI debate judge.

Your provider is {provider}.

Topic:
{topic}

Round:
{round_num}

You preferred:
{preferred_side}

Give a short insightful observation about
the quality of reasoning.

Do not simply say which side was convincing.

Do not summarise the debate.

Do not quote either debater.

Do not mention your model ID.

Do not mention that you are an AI.

Previous observations:

{recent}

Write 2 or 3 natural spoken sentences.
"""

    response = query_openrouter(
        prompt,
        model,
        timeout=40,
        max_tokens=220,
        temperature=0.85
    )

    if response:
        return response

    return (
        "The important distinction is between "
        "a conclusion that sounds plausible and "
        "an argument that has actually answered "
        "the strongest objection."
    )


# ============================================================
# INTRO / OUTRO
# ============================================================

def build_intro(
    topic,
    judge_count
):

    return (
        "Welcome to the AI Debate Arena. "
        "Today, an AI Christian Apologist "
        "faces an AI Skeptic on the question: "
        f"{topic}. "
        "The debate will unfold over three rounds "
        "with equal speaking time for both sides. "
        f"An independent panel of {judge_count} "
        "AI systems will score argument strength, "
        "rebuttal quality, and clarity of reasoning. "
        "Let's begin."
    )


def build_outro(
    judge_count,
    cumulative_a,
    cumulative_b
):

    if math.isclose(
        cumulative_a,
        cumulative_b,
        abs_tol=0.01
    ):

        result = "a draw"

    elif cumulative_a > cumulative_b:

        result = "the AI Christian Apologist"

    else:

        result = "the AI Skeptic"

    return (
        f"After three rounds, our panel of "
        f"{judge_count} AI judges gave the "
        f"AI Christian Apologist a cumulative "
        f"score of {cumulative_a:.1f}, compared "
        f"with {cumulative_b:.1f} for the "
        f"AI Skeptic. "
        f"The final result is {result}. "
        "But the final verdict is still yours. "
        "Which side do you think actually won?"
    )


# ============================================================
# CONCATENATION
# ============================================================

def stitch_segments(
    segments,
    output
):

    list_file = "concat_list.txt"

    with open(
        list_file,
        "w",
        encoding="utf-8"
    ) as file:

        for segment in segments:

            path = os.path.abspath(
                segment
            )

            path = path.replace(
                "'",
                "'\\''"
            )

            file.write(
                f"file '{path}'\n"
            )

    print(
        "🎬 Stitching final video..."
    )

    command = [
        "ffmpeg",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        list_file,
        "-c",
        "copy",
        output,
    ]

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    if result.returncode != 0:

        print(
            result.stderr[-7000:]
        )

        raise RuntimeError(
            "Final FFmpeg concatenation failed."
        )


# ============================================================
# MAIN PIPELINE
# ============================================================

def run_debate_pipeline():

    cleanup_cache()

    if not OPENROUTER_API_KEY:

        raise RuntimeError(
            "OPENROUTER_API_KEY environment "
            "variable is missing."
        )

    # --------------------------------------------------------
    # TOPIC
    # --------------------------------------------------------

    if not os.path.exists("topic.txt"):

        with open(
            "topic.txt",
            "w",
            encoding="utf-8"
        ) as file:

            file.write(
                "Does the universe require a creator?"
            )

    with open(
        "topic.txt",
        "r",
        encoding="utf-8"
    ) as file:

        topic = file.read().strip()

    if not topic:

        topic = (
            "Does the universe require a creator?"
        )

    print()
    print("=" * 70)
    print("AI DEBATE ARENA")
    print("=" * 70)
    print(f"\nTOPIC: {topic}\n")

    # --------------------------------------------------------
    # MODEL DISCOVERY
    # --------------------------------------------------------

    available_models = discover_models()

    if not available_models:

        print(
            "⚠️ Dynamic discovery failed. "
            "Using fallback models."
        )

        available_models = (
            FALLBACK_MODELS.copy()
        )

    # --------------------------------------------------------
    # DEBATE MODELS
    # --------------------------------------------------------

    (
        apologist_model,
        skeptic_model
    ) = choose_primary_models(
        available_models
    )

    print(
        "🎤 Debate engines:"
    )

    print(
        f"   Apologist: "
        f"{provider_from_model(apologist_model)}"
    )

    print(
        f"   Skeptic: "
        f"{provider_from_model(skeptic_model)}"
    )

    # --------------------------------------------------------
    # JUDGES
    # --------------------------------------------------------

    judges = choose_judges(
        available_models,
        (
            apologist_model,
            skeptic_model
        )
    )

    if not judges:

        used = set()
        judges = []

        for model in FALLBACK_MODELS:

            provider = provider_from_model(
                model
            )

            if provider in used:
                continue

            if model in (
                apologist_model,
                skeptic_model
            ):
                continue

            judges.append(model)
            used.add(provider)

            if len(judges) >= MAX_JUDGES:
                break

    print()
    print(
        f"⚖️ Maximum judges: {MAX_JUDGES}"
    )

    print(
        f"⚖️ Actual judges: {len(judges)}"
    )

    print(
        "⚖️ ONE MODEL PER PROVIDER:"
    )

    for model in judges:

        print(
            f"   • "
            f"{provider_from_model(model)}"
            f" — "
            f"{model.split('/', 1)[-1][:28]}"
        )

    # --------------------------------------------------------
    # VIDEO
    # --------------------------------------------------------

    segments = []
    segment_id = 0

    def add_segment(
        text,
        role,
        name,
        position=None,
        glow=None,
        judge_voice_index=None
    ):

        nonlocal segment_id

        video = create_segment(
            text,
            role,
            name,
            topic,
            segment_id,
            apologist_model,
            position,
            glow,
            judge_voice_index
        )

        segments.append(video)

        segment_id += 1

    # --------------------------------------------------------
    # INTRO
    # --------------------------------------------------------

    add_segment(
        build_intro(
            topic,
            len(judges)
        ),
        "Moderator",
        "MODERATOR"
    )

    # --------------------------------------------------------
    # HISTORY
    # --------------------------------------------------------

    previous_history = ""

    cumulative_a = 0.0
    cumulative_b = 0.0

    panel_comments = []

    # --------------------------------------------------------
    # ROUNDS
    # --------------------------------------------------------

    for round_num in range(
        1,
        ROUNDS + 1
    ):

        print()
        print("=" * 70)
        print(
            f"ROUND {round_num}"
        )
        print("=" * 70)

        (
            apologist_turns,
            skeptic_turns,
            previous_history
        ) = build_round_exchanges(
            topic,
            round_num,
            apologist_model,
            skeptic_model,
            previous_history
        )

        # True back-and-forth.
        for turn_index in range(
            TURNS_PER_SIDE_PER_ROUND
        ):

            apologist_text = (
                apologist_turns[
                    turn_index
                ]
            )

            skeptic_text = (
                skeptic_turns[
                    turn_index
                ]
            )

            print(
                f"   Exchange "
                f"{turn_index + 1}: "
                f"A={count_words(apologist_text)} "
                f"words | "
                f"B={count_words(skeptic_text)} "
                f"words"
            )

            add_segment(
                apologist_text,
                "AI Christian Apologist",
                "AI CHRISTIAN APOLOGIST",
                "left",
                "#00FFCC"
            )

            add_segment(
                skeptic_text,
                "AI Skeptic",
                "AI SKEPTIC",
                "right",
                "#FF00FF"
            )

        apologist_full = "\n".join(
            apologist_turns
        )

        skeptic_full = "\n".join(
            skeptic_turns
        )

        print(
            f"   Round total: "
            f"A={count_words(apologist_full)} "
            f"words | "
            f"B={count_words(skeptic_full)} "
            f"words"
        )

        # ----------------------------------------------------
        # JUDGING
        # ----------------------------------------------------

        results = evaluate_round(
            judges,
            topic,
            round_num,
            apologist_full,
            skeptic_full
        )

        round_a, round_b = (
            calculate_round_average(
                results
            )
        )

        cumulative_a += round_a
        cumulative_b += round_b

        print(
            f"📊 Round {round_num}: "
            f"A {round_a:.1f} "
            f"vs "
            f"B {round_b:.1f}"
        )

        print(
            f"📊 Cumulative: "
            f"A {cumulative_a:.1f} "
            f"vs "
            f"B {cumulative_b:.1f}"
        )

        # ----------------------------------------------------
        # STATIC SCORECARD
        # ----------------------------------------------------

        scoreboard_file = (
            f"scoreboard_r{round_num}.png"
        )

        generate_scoreboard(
            round_num,
            results,
            round_a,
            round_b,
            cumulative_a,
            cumulative_b,
            scoreboard_file
        )

        score_text = (
            f"Round {round_num} is complete. "
            f"The {len(results)} independent "
            f"AI judges gave the AI Christian "
            f"Apologist an average score of "
            f"{round_a:.1f}, and the AI Skeptic "
            f"an average score of {round_b:.1f}. "
            f"The cumulative score is "
            f"{cumulative_a:.1f} to "
            f"{cumulative_b:.1f}."
        )

        score_audio = (
            f"score_audio_r{round_num}.mp3"
        )

        score_subs = (
            f"score_subs_r{round_num}.ass"
        )

        score_video = (
            f"score_video_r{round_num}.mp4"
        )

        score_words = generate_audio(
            score_text,
            "Moderator",
            score_audio
        )

        generate_subtitles(
            score_words,
            score_subs,
            scorecard=True
        )

        render_scorecard_video(
            scoreboard_file,
            score_audio,
            score_subs,
            score_video
        )

        segments.append(
            score_video
        )

        # ----------------------------------------------------
        # TWO INTER-ROUND JUDGES
        # ----------------------------------------------------

        if results:

            a_results = [
                r for r in results
                if r["winner"] == "A"
            ]

            b_results = [
                r for r in results
                if r["winner"] == "B"
            ]

            if not a_results:
                a_results = results

            if not b_results:
                b_results = results

            judge_a = random.choice(
                a_results
            )

            judge_b = random.choice(
                b_results
            )

            comment_a = (
                generate_panel_commentary(
                    judge_a["model"],
                    "A",
                    topic,
                    round_num,
                    apologist_full,
                    skeptic_full,
                    panel_comments
                )
            )

            panel_comments.append(
                comment_a
            )

            add_segment(
                comment_a,
                "AI Judge",
                (
                    "AI JUDGE — "
                    + judge_a["provider"].upper()
                ),
                "center",
                "#3399FF",
                judge_voice_index=0
            )

            comment_b = (
                generate_panel_commentary(
                    judge_b["model"],
                    "B",
                    topic,
                    round_num,
                    apologist_full,
                    skeptic_full,
                    panel_comments
                )
            )

            panel_comments.append(
                comment_b
            )

            add_segment(
                comment_b,
                "AI Judge",
                (
                    "AI JUDGE — "
                    + judge_b["provider"].upper()
                ),
                "center",
                "#3399FF",
                judge_voice_index=1
            )

    # --------------------------------------------------------
    # OUTRO
    # --------------------------------------------------------

    add_segment(
        build_outro(
            len(judges),
            cumulative_a,
            cumulative_b
        ),
        "Moderator",
        "MODERATOR"
    )

    # --------------------------------------------------------
    # STITCH
    # --------------------------------------------------------

    stitch_segments(
        segments,
        OUTPUT_FILE
    )

    print()
    print("=" * 70)
    print("✅ DEBATE COMPLETE")
    print("=" * 70)

    print(
        f"🎥 Output: {OUTPUT_FILE}"
    )

    print(
        f"⚖️ AI judges: {len(judges)}"
    )

    print(
        f"🏆 Final score: "
        f"Apologist {cumulative_a:.1f} "
        f"vs "
        f"Skeptic {cumulative_b:.1f}"
    )

    cleanup_cache()


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

    except Exception as exc:

        print(
            "\n❌ PIPELINE FAILED"
        )

        print(
            str(exc)
        )

        raise
