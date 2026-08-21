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
from typing import List, Dict

import edge_tts
from PIL import Image, ImageDraw, ImageFont, ImageFilter


# ============================================================
# AI DEBATE ARENA — STABLE VIDEO BUILD
# ============================================================

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"

OUTPUT_FILE = "final_debate_output.mp4"

VIDEO_W = 1920
VIDEO_H = 1080


# ============================================================
# DEBATE SETTINGS
# ============================================================

ROUNDS = 3

# Fair structure:
# 4 turns per side × approximately 125 words
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
# TIMEOUTS
# ============================================================

MODEL_DISCOVERY_TIMEOUT = 20
MODEL_REQUEST_TIMEOUT = 60
JUDGE_REQUEST_TIMEOUT = 35


# ============================================================
# TTS VOICES
# ============================================================

# Main speakers.
VOICES = {
    "Moderator": "en-US-AndrewMultilingualNeural",
    "AI Christian Apologist": "en-US-BrianMultilingualNeural",
    "AI Skeptic": "en-US-AvaMultilingualNeural",

    # Judge commentary gets its own voice.
    "AI Judge 1": "en-US-ChristopherNeural",
    "AI Judge 2": "en-US-EmmaMultilingualNeural",
    "AI Judge 3": "en-US-EricNeural",
}


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
    "together": "Together AI",
    "fireworks": "Fireworks AI",
}


def provider_from_model(model_id):
    if not model_id:
        return "Unknown"

    prefix = model_id.split("/", 1)[0].lower().strip()

    return PROVIDER_ALIASES.get(
        prefix,
        prefix.replace("-", " ").title(),
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
# UTILITIES
# ============================================================

def count_words(text):

    return len(
        re.findall(
            r"\b[\w'-]+\b",
            text or "",
        )
    )


def clamp_score(value):

    try:
        value = float(value)
    except Exception:
        value = 50.0

    return max(
        0.0,
        min(
            100.0,
            value,
        ),
    )


def clean_for_speech(text):

    text = re.sub(
        r"\([^)]*\)",
        "",
        text or "",
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

    text = re.sub(
        r"\s+",
        " ",
        text,
    )

    return text.strip()


def hex_to_rgba(hex_str, alpha):

    h = hex_str.lstrip("#")

    return (
        int(h[0:2], 16),
        int(h[2:4], 16),
        int(h[4:6], 16),
        alpha,
    )


# ============================================================
# FONT
# ============================================================

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
            return ImageFont.truetype(
                path,
                size,
            )
        except Exception:
            pass

    return ImageFont.load_default()


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
        raise RuntimeError(
            "OPENROUTER_API_KEY is missing."
        )

    try:

        response = requests.get(
            OPENROUTER_MODELS_URL,
            headers=openrouter_headers(),
            timeout=MODEL_DISCOVERY_TIMEOUT,
        )

        if response.status_code != 200:

            print(
                f"⚠️ Model discovery returned "
                f"HTTP {response.status_code}"
            )

            return []

        data = response.json()

        models = []

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

        for item in data.get("data", []):

            model_id = item.get("id")

            if not model_id:
                continue

            lowered = model_id.lower()

            if any(
                x in lowered
                for x in excluded
            ):
                continue

            models.append(model_id)

        models = list(
            dict.fromkeys(models)
        )

        print(
            f"🔎 OpenRouter reports "
            f"{len(models)} usable text models."
        )

        return models

    except Exception as exc:

        print(
            f"⚠️ Model discovery failed: "
            f"{str(exc)[:200]}"
        )

        return []


def query_openrouter(
    prompt,
    model_id,
    timeout=MODEL_REQUEST_TIMEOUT,
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
                    [],
                )

                if choices:

                    content = (
                        choices[0]
                        .get("message", {})
                        .get("content", "")
                    )

                    if (
                        content
                        and len(content.strip()) > 20
                    ):
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
# PRIMARY MODEL SELECTION
# ============================================================

def choose_primary_models(
    available_models,
):

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

    available = set(
        available_models
    )

    found = [
        model
        for model in preference
        if model in available
    ]

    if len(found) >= 2:

        return found[0], found[1]

    if len(found) == 1:

        remaining = [
            m
            for m in available_models
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


# ============================================================
# ONE MODEL PER PROVIDER
# ============================================================

def choose_judges(
    available_models,
    primary_models,
):

    excluded = set(primary_models)

    candidates = [
        model
        for model in available_models
        if model not in excluded
    ]

    groups = {}

    for model in candidates:

        provider = provider_from_model(
            model
        )

        groups.setdefault(
            provider,
            [],
        ).append(model)

    # Prefer recognisable frontier families.
    keywords = [
        "gpt",
        "claude",
        "gemini",
        "mistral",
        "llama",
        "qwen",
        "deepseek",
        "command",
        "grok",
        "nemotron",
    ]

    selected = []

    provider_priority = [
        "OpenAI",
        "Anthropic",
        "Google",
        "xAI",
        "DeepSeek",
        "Mistral",
        "Alibaba / Qwen",
        "Meta",
        "Cohere",
        "Perplexity",
    ]

    ordered = sorted(
        groups.items(),
        key=lambda item: (
            provider_priority.index(item[0])
            if item[0] in provider_priority
            else 999,
            item[0],
        ),
    )

    for provider, models in ordered:

        models = sorted(
            models,
            key=lambda m: (
                0
                if any(
                    k in m.lower()
                    for k in keywords
                )
                else 1,
                len(m),
            ),
        )

        selected.append(
            models[0]
        )

        if len(selected) >= MAX_JUDGES:
            break

    return selected


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

Establish your strongest foundation,
but do not try to finish the entire
debate in this turn.
"""

    else:

        instruction = f"""
This is turn {turn_num} of round {round_num}.

Respond directly to the immediately
preceding argument.

Do not restart the debate.

Do not introduce yourself.

Do not explain what you are doing.

Do not say "in this round".

Do not repeat previous arguments unless
you are directly rebutting them.

Add one genuinely useful new point.
"""

    prompt = f"""
You are the {side_name}
in a serious public debate.

Topic:

{topic}

Your opponent:

{opponent}

{instruction}

Previous exchange:

{previous_exchange or "None - opening exchange."}

Write ONLY your spoken contribution.

Target approximately {WORDS_PER_TURN} words.

Aim for between {MIN_TURN_WORDS} and
{MAX_TURN_WORDS} words.

Natural conversational speech.

Suitable for a general YouTube audience.

Be specific.

Use examples or analogies when helpful.

No headings.

No numbered lists.

No bullet points.

Do not mention AI models.

Do not mention companies.

Do not mention model availability.

Do not use filler.
"""

    response = query_openrouter(
        prompt,
        model,
        max_tokens=420,
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
        TURNS_PER_SIDE_PER_ROUND + 1,
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

        exchange_history = (
            "AI Christian Apologist:\n"
            + apologist
            + "\n\n"
            + "AI Skeptic:\n"
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
You are an independent and impartial
debate judge.

Topic:

{topic}

Round:

{round_num}

SIDE A — AI CHRISTIAN APOLOGIST:

{apologist}

SIDE B — AI SKEPTIC:

{skeptic}

Evaluate both sides independently.

Use exactly:

1. Argument strength
2. Rebuttal quality
3. Clarity and reasoning

Score every category from 0 to 100.

Calculate the average for each side.

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

Do not include commentary.
"""

    response = query_openrouter(
        prompt,
        model,
        timeout=JUDGE_REQUEST_TIMEOUT,
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

        at = (
            aa + ar + ac
        ) / 3

        bt = (
            ba + br + bc
        ) / 3

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
        max_workers=min(
            JUDGE_WORKERS,
            len(judges),
        )
    ) as executor:

        futures = {
            executor.submit(
                worker,
                model,
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

    return (
        round(a, 2),
        round(b, 2),
    )


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

            words.append(
                {
                    "text": chunk["text"],
                    "start": start,
                    "duration": duration,
                    "end": start + duration,
                }
            )

    with open(
        filename,
        "wb",
    ) as file:

        file.write(audio)

    return words


def generate_audio(
    text,
    role,
    filename,
):

    if role == "AI Judge 1":
        voice = VOICES["AI Judge 1"]

    elif role == "AI Judge 2":
        voice = VOICES["AI Judge 2"]

    elif role == "AI Judge 3":
        voice = VOICES["AI Judge 3"]

    else:
        voice = VOICES.get(
            role,
            VOICES["Moderator"],
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
            f"{voice}: "
            f"{str(exc)[:150]}"
        )

        return asyncio.run(
            generate_audio_async(
                clean_text,
                VOICES["Moderator"],
                filename,
            )
        )


# ============================================================
# SUBTITLES — STABLE PARAGRAPH SYSTEM
# ============================================================

def format_ass_time(seconds):

    seconds = max(
        0.0,
        float(seconds),
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
        "\\\\",
    )

    text = text.replace(
        "{",
        "\\{",
    )

    text = text.replace(
        "}",
        "\\}",
    )

    text = text.replace(
        "\n",
        " ",
    )

    return text


def split_subtitle_blocks(
    words,
    target_words=13,
):

    blocks = []

    if not words:
        return blocks

    current = []

    for word in words:

        current.append(word)

        # Finish on punctuation after a sensible
        # number of words.
        punctuation = bool(
            re.search(
                r"[.!?,;:]$",
                str(word["text"]),
            )
        )

        if (
            len(current) >= target_words
            and punctuation
        ):

            blocks.append(current)
            current = []

        elif len(current) >= 16:

            blocks.append(current)
            current = []

    if current:
        blocks.append(current)

    return blocks


def wrap_subtitle_text(
    words,
    max_chars=58,
):

    lines = []
    current = ""

    for word in words:

        text = str(
            word["text"]
        )

        candidate = (
            text
            if not current
            else current + " " + text
        )

        if (
            len(candidate) <= max_chars
        ):

            current = candidate

        else:

            if current:
                lines.append(current)

            current = text

    if current:
        lines.append(current)

    # Maximum 3 lines.
    if len(lines) <= 3:
        return "\\N".join(lines)

    first = lines[0]
    second = lines[1]
    remaining = " ".join(
        lines[2:]
    )

    return (
        first
        + "\\N"
        + second
        + "\\N"
        + remaining
    )


def generate_subtitles(
    words,
    filename,
    scorecard=False,
):

    # --------------------------------------------------------
    # SAFE ZONES
    #
    # Debate:
    # subtitle bottom edge = 825
    # speaker card begins at ~885
    #
    # Scorecard:
    # subtitle bottom edge = 950
    # but scorecard itself is designed to stay above/away
    # from this zone.
    # --------------------------------------------------------

    if scorecard:

        alignment = 2
        margin_v = 35

    else:

        alignment = 2
        margin_v = 205

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: DebateSub,DejaVu Sans,40,&H00FFFFFF,&H00FFFFFF,&H00101010,&HE6000000,1,0,0,0,100,100,0,0,1,3,1,{alignment},180,180,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    with open(
        filename,
        "w",
        encoding="utf-8",
    ) as file:

        file.write(header)

        if not words:
            return

        blocks = split_subtitle_blocks(
            words,
            target_words=13,
        )

        for index, block in enumerate(
            blocks
        ):

            start = float(
                block[0]["start"]
            )

            if index + 1 < len(blocks):

                next_start = float(
                    blocks[index + 1][0]["start"]
                )

                # Tiny overlap avoids a visible subtitle gap.
                end = max(
                    start + 0.35,
                    next_start + 0.04,
                )

            else:

                end = (
                    float(block[-1]["end"])
                    + 0.20
                )

            text = wrap_subtitle_text(
                block
            )

            file.write(
                "Dialogue: 0,"
                + format_ass_time(start)
                + ","
                + format_ass_time(end)
                + ",DebateSub,,0,0,0,,"
                + text
                + "\n"
            )


# ============================================================
# VISUAL CUES
# ============================================================

VISUAL_CUES = {

    "garden of eden": (
        "GARDEN OF EDEN",
        "garden",
    ),

    "big bang": (
        "BIG BANG",
        "bigbang",
    ),

    "evolution": (
        "EVOLUTION",
        "evolution",
    ),

    "universe": (
        "UNIVERSE",
        "universe",
    ),

    "earth": (
        "EARTH",
        "earth",
    ),

    "apple": (
        "FRUIT",
        "fruit",
    ),

    "fruit": (
        "FRUIT",
        "fruit",
    ),

    "tree": (
        "TREE",
        "tree",
    ),

    "adam": (
        "ADAM",
        "person",
    ),

    "eve": (
        "EVE",
        "person",
    ),

    "creator": (
        "CREATOR",
        "creator",
    ),

    "god": (
        "GOD / CREATOR",
        "creator",
    ),

    "bible": (
        "BIBLE",
        "book",
    ),

    "scripture": (
        "SCRIPTURE",
        "book",
    ),

    "evidence": (
        "EVIDENCE",
        "evidence",
    ),

    "consciousness": (
        "CONSCIOUSNESS",
        "mind",
    ),

    "mind": (
        "MIND",
        "mind",
    ),
}


def detect_visual_cues(
    text,
    words,
):

    lowered = text.lower()

    cues = []

    for phrase, data in VISUAL_CUES.items():

        label, kind = data

        index = lowered.find(
            phrase
        )

        if index < 0 or not words:
            continue

        # Map character position to TTS word position.
        ratio = (
            index
            / max(
                1,
                len(text),
            )
        )

        word_index = int(
            ratio * len(words)
        )

        word_index = max(
            0,
            min(
                len(words) - 1,
                word_index,
            ),
        )

        start = float(
            words[word_index]["start"]
        )

        # Keep cue visible for several seconds.
        end_index = min(
            len(words) - 1,
            word_index + 30,
        )

        end = float(
            words[end_index]["end"]
        ) + 0.4

        cues.append(
            {
                "label": label,
                "kind": kind,
                "start": start,
                "end": end,
            }
        )

    cues.sort(
        key=lambda x: x["start"]
    )

    filtered = []

    for cue in cues:

        if any(
            abs(
                cue["start"]
                -
                existing["start"]
            ) < 2.0
            for existing in filtered
        ):
            continue

        filtered.append(cue)

        if len(filtered) >= 3:
            break

    return filtered


# ============================================================
# BACKGROUND
# ============================================================

def create_background(
    position,
    glow_color,
    filename,
):

    background = os.path.join(
        os.path.dirname(
            os.path.abspath(__file__)
        ),
        "background.png",
    )

    if os.path.exists(background):

        try:

            image = (
                Image.open(background)
                .convert("RGB")
                .resize(
                    (
                        VIDEO_W,
                        VIDEO_H,
                    )
                )
            )

        except Exception:

            image = Image.new(
                "RGB",
                (
                    VIDEO_W,
                    VIDEO_H,
                ),
                (
                    12,
                    16,
                    32,
                ),
            )

    else:

        image = Image.new(
            "RGB",
            (
                VIDEO_W,
                VIDEO_H,
            ),
            (
                12,
                16,
                32,
            ),
        )

        draw = ImageDraw.Draw(
            image
        )

        for x in range(
            0,
            VIDEO_W,
            60,
        ):

            draw.line(
                [
                    (x, 0),
                    (x, VIDEO_H),
                ],
                fill=(
                    20,
                    26,
                    45,
                ),
                width=2,
            )

        for y in range(
            0,
            VIDEO_H,
            60,
        ):

            draw.line(
                [
                    (0, y),
                    (VIDEO_W, y),
                ],
                fill=(
                    20,
                    26,
                    45,
                ),
                width=2,
            )

    overlay = Image.new(
        "RGBA",
        (
            VIDEO_W,
            VIDEO_H,
        ),
        (
            0,
            0,
            0,
            0,
        ),
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
        -50,
    ):

        alpha = int(
            15 *
            (
                1
                -
                radius / 700
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

    overlay = overlay.filter(
        ImageFilter.GaussianBlur(30)
    )

    result = Image.alpha_composite(
        image.convert("RGBA"),
        overlay,
    ).convert("RGB")

    result.save(filename)


# ============================================================
# SPEAKER CARD
# ============================================================

def create_ui_overlay(
    speaker_name,
    role_label,
    topic,
    position,
    glow_color,
    filename,
    show_topic=True,
):

    image = Image.new(
        "RGBA",
        (
            VIDEO_W,
            VIDEO_H,
        ),
        (
            0,
            0,
            0,
            0,
        ),
    )

    draw = ImageDraw.Draw(
        image
    )

    title_font = load_font(
        28,
        bold=True,
    )

    name_font = load_font(
        30,
        bold=True,
    )

    if show_topic:

        title = f"TOPIC: {topic}"

        bbox = draw.textbbox(
            (0, 0),
            title,
            font=title_font,
        )

        width = (
            bbox[2]
            -
            bbox[0]
        )

        draw.text(
            (
                (
                    VIDEO_W -
                    width
                )
                // 2,
                22,
            ),
            title,
            fill="white",
            font=title_font,
        )

    # --------------------------------------------------------
    # SINGLE IDENTITY CARD
    # --------------------------------------------------------

    card_width = 650
    card_height = 115
    card_y = 885

    if position == "left":

        card_x = 75

    elif position == "right":

        card_x = 1195

    else:

        card_x = (
            VIDEO_W -
            card_width
        ) // 2

    draw.rounded_rectangle(
        [
            card_x,
            card_y,
            card_x + card_width,
            card_y + card_height,
        ],
        radius=18,
        fill=(
            18,
            26,
            46,
            240,
        ),
        outline=glow_color,
        width=4,
    )

    # Single speaker identity.
    draw.ellipse(
        [
            card_x + 22,
            card_y + 25,
            card_x + 46,
            card_y + 49,
        ],
        fill=glow_color,
    )

    draw.text(
        (
            card_x + 65,
            card_y + 20,
        ),
        speaker_name,
        fill="white",
        font=name_font,
    )

    image.save(filename)

    return (
        card_x,
        card_y,
    )


# ============================================================
# SCOREBOARD — STATIC, NON-ZOOMING
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

    background = os.path.join(
        os.path.dirname(
            os.path.abspath(__file__)
        ),
        "background.png",
    )

    if os.path.exists(background):

        try:

            image = (
                Image.open(background)
                .convert("RGB")
                .resize(
                    (
                        VIDEO_W,
                        VIDEO_H,
                    )
                )
            )

        except Exception:

            image = Image.new(
                "RGB",
                (
                    VIDEO_W,
                    VIDEO_H,
                ),
                (
                    12,
                    16,
                    32,
                ),
            )

    else:

        image = Image.new(
            "RGB",
            (
                VIDEO_W,
                VIDEO_H,
            ),
            (
                12,
                16,
                32,
            ),
        )

    # Darken but do not zoom.
    overlay = Image.new(
        "RGBA",
        (
            VIDEO_W,
            VIDEO_H,
        ),
        (
            0,
            0,
            0,
            225,
        ),
    )

    image = Image.alpha_composite(
        image.convert("RGBA"),
        overlay,
    ).convert("RGB")

    draw = ImageDraw.Draw(
        image
    )

    header = load_font(
        34,
        bold=True,
    )

    sub = load_font(
        21,
        bold=True,
    )

    small = load_font(
        18
    )

    tiny = load_font(
        16
    )

    # --------------------------------------------------------
    # HEADER
    # --------------------------------------------------------

    def centred(
        y,
        text,
        font,
        fill,
    ):

        box = draw.textbbox(
            (0, 0),
            text,
            font=font,
        )

        width = (
            box[2]
            -
            box[0]
        )

        draw.text(
            (
                (
                    VIDEO_W -
                    width
                )
                // 2,
                y,
            ),
            text,
            fill=fill,
            font=font,
        )

    judge_count = len(results)

    centred(
        25,
        f"ROUND {round_num} — AI JUDGING PANEL",
        header,
        "#FFD700",
    )

    centred(
        70,
        f"{judge_count} INDEPENDENT JUDGES • 3 CATEGORIES • 0–100",
        sub,
        "white",
    )

    centred(
        110,
        (
            f"ROUND SCORE   "
            f"APOLOGIST {round_a:.1f}   "
            f"VS   "
            f"SKEPTIC {round_b:.1f}"
        ),
        sub,
        "white",
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
        "#FFD700",
    )

    # --------------------------------------------------------
    # LEFT PANEL
    # --------------------------------------------------------

    left_x = 90
    left_width = 700

    draw.rounded_rectangle(
        [
            left_x,
            215,
            left_x + left_width,
            455,
        ],
        radius=18,
        fill=(
            10,
            18,
            32,
            235,
        ),
        outline=(
            80,
            90,
            120,
        ),
        width=2,
    )

    draw.text(
        (
            left_x + 25,
            235,
        ),
        "CATEGORY AVERAGES",
        fill="#FFD700",
        font=sub,
    )

    draw.text(
        (
            left_x + 405,
            275,
        ),
        "APOLOGIST",
        fill="#00FFCC",
        font=tiny,
    )

    draw.text(
        (
            left_x + 565,
            275,
        ),
        "SKEPTIC",
        fill="#FF66FF",
        font=tiny,
    )

    categories = [
        (
            "Argument strength",
            "A_argument",
            "B_argument",
        ),
        (
            "Rebuttal quality",
            "A_rebuttal",
            "B_rebuttal",
        ),
        (
            "Clarity & reasoning",
            "A_clarity",
            "B_clarity",
        ),
    ]

    y = 315

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
            (
                left_x + 25,
                y,
            ),
            label,
            fill="white",
            font=small,
        )

        draw.text(
            (
                left_x + 420,
                y,
            ),
            f"{a:.1f}",
            fill="#00FFCC",
            font=small,
        )

        draw.text(
            (
                left_x + 580,
                y,
            ),
            f"{b:.1f}",
            fill="#FF66FF",
            font=small,
        )

        y += 45

    # --------------------------------------------------------
    # RIGHT PANEL
    # --------------------------------------------------------

    right_x = 850
    right_width = 900

    draw.rounded_rectangle(
        [
            right_x,
            215,
            right_x + right_width,
            625,
        ],
        radius=18,
        fill=(
            10,
            18,
            32,
            235,
        ),
        outline=(
            80,
            90,
            120,
        ),
        width=2,
    )

    draw.text(
        (
            right_x + 25,
            235,
        ),
        "INDIVIDUAL JUDGE SCORES",
        fill="#FFD700",
        font=sub,
    )

    # Fixed columns.
    provider_x = right_x + 35
    a_x = right_x + 690
    b_x = right_x + 770

    draw.text(
        (
            provider_x,
            275,
        ),
        "PROVIDER",
        fill="white",
        font=tiny,
    )

    draw.text(
        (
            a_x,
            275,
        ),
        "A",
        fill="#00FFCC",
        font=tiny,
    )

    draw.text(
        (
            b_x,
            275,
        ),
        "B",
        fill="#FF66FF",
        font=tiny,
    )

    draw.line(
        [
            (
                right_x + 25,
                300,
            ),
            (
                right_x + right_width - 25,
                300,
            ),
        ],
        fill=(
            90,
            100,
            130,
        ),
        width=2,
    )

    # Seven judges maximum.
    row_height = 42

    for index, result in enumerate(results[:MAX_JUDGES]):

        row_y = (
            315
            +
            index * row_height
        )

        provider = result.get(
            "provider",
            provider_from_model(
                result["model"]
            ),
        )

        if len(provider) > 30:

            provider = (
                provider[:27]
                + "..."
            )

        draw.text(
            (
                provider_x,
                row_y,
            ),
            provider,
            fill="white",
            font=small,
        )

        draw.text(
            (
                a_x,
                row_y,
            ),
            f"{result['A_total']:.1f}",
            fill="#00FFCC",
            font=small,
        )

        draw.text(
            (
                b_x,
                row_y,
            ),
            f"{result['B_total']:.1f}",
            fill="#FF66FF",
            font=small,
        )

    # --------------------------------------------------------
    # BOTTOM LEGEND
    # --------------------------------------------------------

    centred(
        675,
        "A = AI CHRISTIAN APOLOGIST     B = AI SKEPTIC",
        small,
        "white",
    )

    # Important:
    # No topic title is drawn here.
    # No animation.
    # No zoom.
    # No speaker card.
    #
    # This leaves a clean lower subtitle-safe area.
    image.save(filename)


# ============================================================
# FFMPEG PATH
# ============================================================

def ffmpeg_filter_path(filename):

    path = os.path.abspath(filename)

    path = path.replace(
        "\\",
        "/",
    )

    path = path.replace(
        "'",
        r"\'",
    )

    path = path.replace(
        ":",
        r"\:",
    )

    return path


# ============================================================
# VISUAL CUE FILTER
# ============================================================

def visual_cue_filter(
    cue,
    input_index,
):

    start = max(
        0.0,
        float(cue["start"]),
    )

    end = max(
        start + 0.5,
        float(cue["end"]),
    )

    # A simple visual panel.
    #
    # It fades in and out and sits ABOVE the subtitle zone.
    #
    # The actual icon is generated from FFmpeg primitives.

    label = cue["label"]
    kind = cue["kind"]

    escaped_label = (
        label
        .replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace(":", "\\:")
    )

    # Different primitive animation styles.
    if kind == "bigbang":

        draw = (
            "drawbox="
            "x=760:y=300:w=400:h=260:"
            "color=0x111827CC:t=fill,"
            "drawbox="
            "x=800:y=340:w=320:h=180:"
            "color=0xFFD70088:t=fill,"
        )

    elif kind in ("earth", "universe"):

        draw = (
            "drawbox="
            "x=730:y=275:w=460:h=280:"
            "color=0x101A35DD:t=fill,"
        )

    elif kind in ("garden", "tree"):

        draw = (
            "drawbox="
            "x=710:y=260:w=500:h=300:"
            "color=0x102B1CDD:t=fill,"
        )

    else:

        draw = (
            "drawbox="
            "x=710:y=300:w=500:h=220:"
            "color=0x101827DD:t=fill,"
        )

    # The label itself is the guaranteed visible component.
    # The background panel fades in/out.
    #
    # This avoids depending on separate PNG assets.

    return (
        draw
        +
        "drawtext="
        f"fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:"
        f"text='{escaped_label}':"
        "fontcolor=white:"
        "fontsize=42:"
        "x=(w-text_w)/2:"
        "y=365:"
        f"alpha='if(lt(t,{start:.2f}),0,"
        f"if(lt(t,{start + 0.35:.2f}),"
        f"(t-{start:.2f})/0.35,"
        f"if(lt(t,{end - 0.35:.2f}),1,"
        f"if(lt(t,{end:.2f}),"
        f"({end:.2f}-t)/0.35,0))))':"
        f"enable='between(t,{start:.2f},{end:.2f})'"
    )


# ============================================================
# VIDEO SEGMENT RENDERING
# ============================================================

def render_video_segment(
    background,
    ui,
    audio,
    ass,
    output,
    position,
    glow_color,
    card_x,
    card_y,
    cues=None,
    scorecard=False,
):

    required = [
        (
            background,
            "background",
        ),
        (
            ui,
            "UI",
        ),
        (
            audio,
            "audio",
        ),
        (
            ass,
            "subtitle",
        ),
    ]

    for path, label in required:

        if not os.path.exists(path):

            raise FileNotFoundError(
                f"{label} file missing: "
                f"{os.path.abspath(path)}"
            )

    cues = cues or []

    ass_path = ffmpeg_filter_path(
        ass
    )

    glow = glow_color.lstrip("#")

    # --------------------------------------------------------
    # BASE VIDEO
    #
    # Scorecards MUST NOT zoom.
    # --------------------------------------------------------

    if scorecard:

        base_filter = (
            "[0:v]"
            "scale=1920:1080"
            "[bg];"
        )

    else:

        if position == "left":

            pan_x = "0"

        elif position == "right":

            pan_x = "iw-(iw/zoom)"

        else:

            pan_x = "(iw-(iw/zoom))/2"

        pan_y = (
            "(ih-(ih/zoom))/2"
        )

        base_filter = (
            "[0:v]"
            "scale=1920:1080,"
            "zoompan="
            "z='min(zoom+0.00020,1.06)':"
            f"x='{pan_x}':"
            f"y='{pan_y}':"
            "d=9000:"
            "s=1920x1080:"
            "fps=30"
            "[bg];"
        )

    # --------------------------------------------------------
    # UI
    # --------------------------------------------------------

    filter_parts = [
        base_filter,

        "[1:v]"
        "scale=1920:1080"
        "[ui];",

        "[bg]"
        "[ui]"
        "overlay=0:0"
        "[base];",
    ]

    current = "[base]"

    # --------------------------------------------------------
    # WAVEFORM
    #
    # Not shown on scorecards.
    # Positioned above the bottom of the card,
    # to the RIGHT of the speaker name.
    # --------------------------------------------------------

    if not scorecard:

        wave_x = card_x + 370
        wave_y = card_y + 67

        wave_x = max(
            10,
            min(
                wave_x,
                1600,
            ),
        )

        wave_y = max(
            10,
            min(
                wave_y,
                1000,
            ),
        )

        filter_parts.extend(
            [
                "[2:a]"
                "showwaves="
                "s=240x38:"
                "mode=cline:"
                f"colors=0x{glow}:"
                "rate=30"
                "[wave];",

                current
                + "[wave]"
                + f"overlay={wave_x}:{wave_y}"
                "[withwave];",
            ]
        )

        current = "[withwave]"

    # --------------------------------------------------------
    # VISUAL CUES
    #
    # These are now drawn directly in FFmpeg.
    # No external cue PNG files.
    # --------------------------------------------------------

    if cues and not scorecard:

        # Keep the first cue only when cues overlap.
        for index, cue in enumerate(cues[:2]):

            start = max(
                0.0,
                float(cue["start"]),
            )

            end = max(
                start + 0.5,
                float(cue["end"]),
            )

            label = (
                cue["label"]
                .replace("\\", "\\\\")
                .replace("'", "\\'")
                .replace(":", "\\:")
            )

            # Safe upper-middle visual area.
            x = 650
            y = 300

            # Fade animation.
            alpha = (
                f"if(lt(t,{start:.2f}),0,"
                f"if(lt(t,{start + 0.35:.2f}),"
                f"(t-{start:.2f})/0.35,"
                f"if(lt(t,{end - 0.35:.2f}),1,"
                f"if(lt(t,{end:.2f}),"
                f"({end:.2f}-t)/0.35,0))))"
            )

            filter_parts.append(
                current
                + "drawbox="
                f"x={x}:y={y}:w=620:h=180:"
                "color=0x0B1226DD:t=fill:"
                f"enable='between(t,{start:.2f},{end:.2f})'"
                "[cuebox];"
            )

            current = "[cuebox]"

            filter_parts.append(
                current
                + "drawtext="
                "fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:"
                f"text='{label}':"
                "fontcolor=white:"
                "fontsize=42:"
                "x=(w-text_w)/2:"
                "y=365:"
                f"alpha='{alpha}':"
                f"enable='between(t,{start:.2f},{end:.2f})'"
                "[cueout];"
            )

            current = "[cueout]"

    # --------------------------------------------------------
    # SUBTITLES LAST
    #
    # Therefore they are always rendered on top.
    # --------------------------------------------------------

    filter_parts.append(
        current
        + f"ass='{ass_path}'"
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
        "30",

        "-i",
        background,

        "-i",
        ui,

        "-i",
        audio,

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
        text=True,
    )

    if result.returncode != 0:

        print(
            "\n❌ FFmpeg failed:"
        )

        print(
            result.stderr[-7000:]
        )

        raise RuntimeError(
            f"FFmpeg failed creating "
            f"{output}"
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
    position=None,
    glow=None,
    scorecard=False,
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
    )

    generate_subtitles(
        words,
        subtitle_file,
        scorecard=scorecard,
    )

    clean_text = clean_for_speech(
        text
    )

    cues = []

    if not scorecard:

        cues = detect_visual_cues(
            clean_text,
            words,
        )

    create_background(
        position,
        glow,
        background_file,
    )

    card_x, card_y = create_ui_overlay(
        speaker_name,
        role,
        topic,
        position,
        glow,
        ui_file,
        show_topic=not scorecard,
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
        cues,
        scorecard=scorecard,
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

    provider = provider_from_model(
        model
    )

    recent = "\n".join(
        previous_comments[-6:]
    )

    preferred_side = (
        "AI Christian Apologist"
        if side == "A"
        else "AI Skeptic"
    )

    prompt = f"""
You are an independent debate judge.

Your company/provider is:

{provider}

Topic:

{topic}

Round:

{round_num}

You preferred:

{preferred_side}

Give a short, insightful observation
about the quality of reasoning.

Do not simply say which side was more convincing.

Do not summarise the debate.

Do not quote either debater.

Do not mention model IDs.

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
        temperature=0.85,
    )

    if response:
        return response

    return (
        "The important distinction here is "
        "between a conclusion that sounds plausible "
        "and an argument that has actually answered "
        "the strongest objection."
    )


# ============================================================
# INTRO / OUTRO
# ============================================================

def build_intro(
    topic,
    judge_count,
):

    return (
        "Welcome to the AI Debate Arena. "
        "Today, an AI Christian Apologist "
        "faces an AI Skeptic on the question: "
        f"{topic}. "
        "The debate will unfold over three rounds, "
        "with equal speaking time for both sides. "
        f"An independent panel of up to {judge_count} "
        "AI systems will score argument strength, "
        "rebuttal quality, and clarity of reasoning. "
        "Let's begin."
    )


def build_outro(
    judge_count,
    cumulative_a,
    cumulative_b,
):

    if math.isclose(
        cumulative_a,
        cumulative_b,
        abs_tol=0.01,
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
        f"with {cumulative_b:.1f} for the AI Skeptic. "
        f"The final result is {result}. "
        "But the final verdict is still yours. "
        "Which side do you think actually won?"
    )


# ============================================================
# CONCATENATION
# ============================================================

def stitch_segments(
    segments,
    output,
):

    list_file = "concat_list.txt"

    with open(
        list_file,
        "w",
        encoding="utf-8",
    ) as file:

        for segment in segments:

            path = os.path.abspath(
                segment
            )

            path = path.replace(
                "'",
                "'\\''",
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
        text=True,
    )

    if result.returncode != 0:

        print(
            result.stderr[-7000:]
        )

        raise RuntimeError(
            "Final FFmpeg concatenation failed."
        )


# ============================================================
# MAIN
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

    if not os.path.exists(
        "topic.txt"
    ):

        with open(
            "topic.txt",
            "w",
            encoding="utf-8",
        ) as file:

            file.write(
                "Does the universe require a creator?"
            )

    with open(
        "topic.txt",
        "r",
        encoding="utf-8",
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
    print()
    print(f"TOPIC: {topic}")
    print()

    # --------------------------------------------------------
    # MODELS
    # --------------------------------------------------------

    available_models = discover_models()

    if not available_models:

        print(
            "⚠️ Dynamic discovery failed."
        )

        available_models = (
            FALLBACK_MODELS.copy()
        )

    (
        apologist_model,
        skeptic_model,
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

    judges = choose_judges(
        available_models,
        (
            apologist_model,
            skeptic_model,
        ),
    )

    if not judges:

        used_providers = set()
        judges = []

        for model in FALLBACK_MODELS:

            provider = provider_from_model(
                model
            )

            if provider in used_providers:
                continue

            if model in (
                apologist_model,
                skeptic_model,
            ):
                continue

            judges.append(model)
            used_providers.add(provider)

            if len(judges) >= MAX_JUDGES:
                break

    print()
    print(
        f"⚖️ Maximum judges: {MAX_JUDGES}"
    )

    print(
        f"⚖️ Judges selected: {len(judges)}"
    )

    print(
        "⚖️ One model per provider:"
    )

    for model in judges:

        print(
            f"   • "
            f"{provider_from_model(model)}"
        )

    # --------------------------------------------------------
    # VIDEO
    # --------------------------------------------------------

    segments = []
    segment_id = 0

    # Used to give different voices to judge commentators.
    judge_voice_counter = 0

    def add_segment(
        text,
        role,
        name,
        position=None,
        glow=None,
        scorecard=False,
    ):

        nonlocal segment_id

        video = create_segment(
            text,
            role,
            name,
            topic,
            segment_id,
            position,
            glow,
            scorecard=scorecard,
        )

        segments.append(video)

        segment_id += 1

    # --------------------------------------------------------
    # INTRO
    # --------------------------------------------------------

    add_segment(
        build_intro(
            topic,
            len(judges),
        ),
        "Moderator",
        "MODERATOR",
        "center",
        "#FFD700",
    )

    # --------------------------------------------------------
    # DEBATE
    # --------------------------------------------------------

    previous_history = ""

    cumulative_a = 0.0
    cumulative_b = 0.0

    panel_comments = []

    for round_num in range(
        1,
        ROUNDS + 1,
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
            previous_history,
        ) = build_round_exchanges(
            topic,
            round_num,
            apologist_model,
            skeptic_model,
            previous_history,
        )

        # ----------------------------------------------------
        # BACK AND FORTH
        # ----------------------------------------------------

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
                "#00FFCC",
            )

            add_segment(
                skeptic_text,
                "AI Skeptic",
                "AI SKEPTIC",
                "right",
                "#FF00FF",
            )

        apologist_full = "\n".join(
            apologist_turns
        )

        skeptic_full = "\n".join(
            skeptic_turns
        )

        print(
            f"   Round totals: "
            f"A={count_words(apologist_full)} "
            f"| B={count_words(skeptic_full)}"
        )

        # ----------------------------------------------------
        # JUDGING
        # ----------------------------------------------------

        results = evaluate_round(
            judges,
            topic,
            round_num,
            apologist_full,
            skeptic_full,
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
            scoreboard_file,
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

        add_segment(
            score_text,
            "Moderator",
            "ROUND SCORECARD",
            "center",
            "#FFD700",
            scorecard=True,
        )

        # ----------------------------------------------------
        # JUDGE COMMENTARY
        # ----------------------------------------------------

        if results:

            a_results = [
                r
                for r in results
                if r["winner"] == "A"
            ]

            b_results = [
                r
                for r in results
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

            # Different judge voices.
            judge_voice_counter += 1

            if (
                judge_voice_counter % 3
                == 1
            ):

                judge_role_a = "AI Judge 1"

            elif (
                judge_voice_counter % 3
                == 2
            ):

                judge_role_a = "AI Judge 2"

            else:

                judge_role_a = "AI Judge 3"

            judge_voice_counter += 1

            if (
                judge_voice_counter % 3
                == 1
            ):

                judge_role_b = "AI Judge 1"

            elif (
                judge_voice_counter % 3
                == 2
            ):

                judge_role_b = "AI Judge 2"

            else:

                judge_role_b = "AI Judge 3"

            comment_a = generate_panel_commentary(
                judge_a["model"],
                "A",
                topic,
                round_num,
                apologist_full,
                skeptic_full,
                panel_comments,
            )

            panel_comments.append(
                comment_a
            )

            add_segment(
                comment_a,
                judge_role_a,
                (
                    "AI JUDGE — "
                    +
                    judge_a["provider"].upper()
                ),
                "center",
                "#3399FF",
            )

            comment_b = generate_panel_commentary(
                judge_b["model"],
                "B",
                topic,
                round_num,
                apologist_full,
                skeptic_full,
                panel_comments,
            )

            panel_comments.append(
                comment_b
            )

            add_segment(
                comment_b,
                judge_role_b,
                (
                    "AI JUDGE — "
                    +
                    judge_b["provider"].upper()
                ),
                "center",
                "#3399FF",
            )

    # --------------------------------------------------------
    # OUTRO
    # --------------------------------------------------------

    add_segment(
        build_outro(
            len(judges),
            cumulative_a,
            cumulative_b,
        ),
        "Moderator",
        "MODERATOR",
        "center",
        "#FFD700",
    )

    add_segment(
        (
            "That concludes today's AI Debate Arena. "
            "The arguments have been presented and "
            "the panel has delivered its verdict. "
            "But what do you think? "
            "Subscribe for more AI debates and let us "
            "know which side you believe made the "
            "stronger case."
        ),
        "Moderator",
        "MODERATOR",
        "center",
        "#FFD700",
    )

    # --------------------------------------------------------
    # STITCH
    # --------------------------------------------------------

    stitch_segments(
        segments,
        OUTPUT_FILE,
    )

    print()
    print("=" * 70)
    print("✅ DEBATE COMPLETE")
    print("=" * 70)

    print(
        f"🎥 Output: {OUTPUT_FILE}"
    )

    print(
        f"⚖️ AI judges used: "
        f"{len(judges)}"
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
