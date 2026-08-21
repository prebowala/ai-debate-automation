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
from typing import List, Dict, Tuple, Optional

import edge_tts
from PIL import Image, ImageDraw, ImageFont, ImageFilter


# ============================================================
# AI DEBATE ARENA — FUTURE-PROOF MAIN.PY
# ============================================================

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"

OUTPUT_FILE = "final_debate_output.mp4"

# ============================================================
# JUDGING
# ============================================================

MAX_JUDGES = 30
JUDGE_WORKERS = 10

# ============================================================
# DEBATE
# ============================================================

ROUNDS = 3

MIN_APOLOGIST_WORDS = 300
MAX_APOLOGIST_WORDS = 500

MIN_SKEPTIC_WORDS = 450
MAX_SKEPTIC_WORDS = 750

# ============================================================
# TIMEOUTS
# ============================================================

MODEL_DISCOVERY_TIMEOUT = 20
MODEL_REQUEST_TIMEOUT = 60
JUDGE_REQUEST_TIMEOUT = 35

# ============================================================
# NATURAL EDGE TTS VOICES
# ============================================================

VOICES = {
    "Moderator": "en-US-AndrewMultilingualNeural",
    "AI Christian Apologist": "en-US-BrianMultilingualNeural",
    "AI Skeptic": "en-US-AvaMultilingualNeural",
    "Panelist 1": "en-US-ChristopherNeural",
    "Panelist 2": "en-US-EmmaMultilingualNeural",
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
# BASIC UTILITIES
# ============================================================

def count_words(text):

    return len(
        re.findall(
            r"\b[\w'-]+\b",
            text or "",
        )
    )


def safe_filename(text):

    return re.sub(
        r"[^A-Za-z0-9_.-]+",
        "_",
        text,
    )[:100]


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


def clamp_score(value):

    try:
        value = float(value)
    except Exception:
        value = 50.0

    return max(
        0.0,
        min(100.0, value),
    )


def hex_to_rgba(hex_str, alpha):

    hex_str = hex_str.lstrip("#")

    return (
        int(hex_str[0:2], 16),
        int(hex_str[2:4], 16),
        int(hex_str[4:6], 16),
        alpha,
    )


# ============================================================
# FONT LOADING
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
            continue

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

                    if content and len(
                        content.strip()
                    ) > 20:

                        return content.strip()

            else:

                print(
                    f"⚠️ {model_id} returned "
                    f"HTTP {response.status_code}"
                )

        except Exception as exc:

            print(
                f"⚠️ Request failed for "
                f"{model_id}: "
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

    found = []

    for model in preference:

        if model in available:
            found.append(model)

    if len(found) >= 2:

        return (
            found[0],
            found[1],
        )

    if len(found) == 1:

        remaining = [
            m
            for m in available_models
            if m != found[0]
        ]

        if remaining:

            return (
                found[0],
                remaining[0],
            )

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

    excluded = set(
        primary_models
    )

    candidates = [
        model
        for model in available_models
        if model not in excluded
    ]

    preferred_keywords = [
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
        "yi",
        "jamba",
    ]

    preferred = [
        m
        for m in candidates
        if any(
            key in m.lower()
            for key in preferred_keywords
        )
    ]

    others = [
        m
        for m in candidates
        if m not in preferred
    ]

    random.shuffle(preferred)
    random.shuffle(others)

    selected = (
        preferred +
        others
    )

    selected = selected[
        :MAX_JUDGES
    ]

    return list(
        dict.fromkeys(
            selected
        )
    )


# ============================================================
# DEBATE GENERATION
# ============================================================

def generate_apologist(
    topic,
    round_num,
    previous_apologist,
    previous_skeptic,
    model,
):

    if round_num == 1:

        context = """
This is the opening of the debate.

Build the strongest Christian case you can.
Introduce substantive arguments.
Do not waste time explaining your role.
"""

    else:

        context = f"""
This is a continuing debate.

Previous Apologist argument:

{previous_apologist}

Previous Skeptic response:

{previous_skeptic}

Continue naturally from the previous exchange.

Do NOT restart the debate.

Do NOT introduce yourself.

Do NOT say "in this round".

Do NOT explain what you are about to do.

Do NOT simply repeat previous arguments.

Identify the strongest unanswered challenge and
develop NEW reasoning that moves the debate forward.
"""

    prompt = f"""
You are the AI Christian Apologist in a serious public debate.

Topic:

{topic}

{context}

Write a natural spoken argument.

Requirements:

- {MIN_APOLOGIST_WORDS}-{MAX_APOLOGIST_WORDS} words.
- Conversational YouTube speech.
- Strong reasoning.
- Concrete examples and analogies.
- Directly address the opponent.
- Avoid academic jargon.
- No headings.
- No numbered lists.
- Do not mention being an AI.
- Do not mention the underlying model.
- Do not say which company created you.
- Sound like a confident human debater.
- Introduce genuinely new reasoning as the debate progresses.

Write ONLY the spoken argument.
"""

    response = query_openrouter(
        prompt,
        model,
        max_tokens=1100,
        temperature=0.75,
    )

    if response and count_words(
        response
    ) >= 220:

        return response

    return (
        "The central question is whether the existence of the "
        "universe is best understood as something that ultimately "
        "explains itself, or whether its existence points beyond "
        "itself to a deeper explanation."
    )


def generate_skeptic(
    topic,
    round_num,
    apologist_text,
    previous_skeptic,
    model,
):

    history = ""

    if round_num > 1:

        history = f"""
This debate is already underway.

Your previous response was:

{previous_skeptic}

Do not recycle that response.

Find a new weakness, implication, assumption, or
counterargument that has not already been fully explored.
"""

    prompt = f"""
You are the AI Skeptic in a serious public debate.

Topic:

{topic}

Round:

{round_num}

The AI Christian Apologist has just said:

{apologist_text}

{history}

Your job is to give a FULL and substantial rebuttal.

IMPORTANT LENGTH REQUIREMENT:

Write at least {MIN_SKEPTIC_WORDS} words.

Target approximately
{MIN_SKEPTIC_WORDS}-{MAX_SKEPTIC_WORDS} words.

This must NOT be a short response.

You must address multiple distinct weaknesses.

For each major weakness:

1. Identify the claim.
2. Explain the problem.
3. Give a clear example or analogy.
4. Explain why the problem matters.

Then introduce at least one additional objection
that has not already been made.

Rules:

- Natural conversational speech.
- Suitable for a general YouTube audience.
- No academic jargon where ordinary language works.
- No headings.
- No numbered lists.
- Do not introduce yourself.
- Do not say "in this round".
- Do not explain your task.
- Do not mention being an AI.
- Do not mention the underlying model.
- Do not mention model availability.
- Never say that a response could not be generated.
- Do not merely say the argument is unconvincing.
- Actually explain why.
- Do not repeat the same argument from previous rounds.
- End with a strong unresolved challenge.

Write ONLY the spoken rebuttal.
"""

    response = query_openrouter(
        prompt,
        model,
        timeout=MODEL_REQUEST_TIMEOUT,
        max_tokens=1800,
        temperature=0.82,
    )

    if response and count_words(
        response
    ) >= MIN_SKEPTIC_WORDS:

        return response

    print(
        "⚠️ Skeptic response too short. "
        "Running expansion retry..."
    )

    retry_prompt = f"""
Rewrite and substantially expand this Skeptic rebuttal.

Topic:

{topic}

Minimum length:
{MIN_SKEPTIC_WORDS} words.

The response must contain:

- Several independent objections.
- Direct responses to the Apologist.
- At least one concrete analogy.
- A new argument.
- A strong final challenge.

Do not repeat sentences.

Do not mention AI models.

Do not mention that this is a retry.

Apologist:

{apologist_text}

Existing response:

{response or "No usable response."}

Return ONLY the complete spoken rebuttal.
"""

    retry = query_openrouter(
        retry_prompt,
        model,
        timeout=MODEL_REQUEST_TIMEOUT,
        max_tokens=1900,
        temperature=0.8,
    )

    if retry and count_words(
        retry
    ) >= 350:

        return retry

    for fallback in FALLBACK_MODELS:

        if fallback == model:
            continue

        fallback_response = query_openrouter(
            prompt,
            fallback,
            timeout=MODEL_REQUEST_TIMEOUT,
            max_tokens=1800,
            temperature=0.8,
        )

        if (
            fallback_response
            and count_words(
                fallback_response
            ) >= 350
        ):

            print(
                f"✅ Skeptic fallback succeeded: "
                f"{fallback}"
            )

            return fallback_response

    return (
        "The difficulty with this argument is that it moves from "
        "the fact that we have a question about the universe to "
        "the conclusion that we already know the answer. Even if "
        "the universe needs some explanation, that does not by "
        "itself tell us what that explanation must be. We still "
        "have to establish why the explanation should be a personal "
        "creator, why that creator would have the particular "
        "properties being claimed, and why alternative explanations "
        "should be rejected. Those are separate steps, and each one "
        "needs its own evidence. Otherwise we risk taking one "
        "interesting possibility and treating it as though the "
        "whole argument has already been proved."
    )


# ============================================================
# JUDGING
# ============================================================

def neutral_judge(model):

    return {
        "model": model,

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
You are an independent judge of a debate.

Topic:
{topic}

Round:
{round_num}

SIDE A — AI CHRISTIAN APOLOGIST:

{apologist}

SIDE B — AI SKEPTIC:

{skeptic}

Evaluate BOTH sides independently.

Use exactly these three categories:

1. ARGUMENT STRENGTH
2. REBUTTAL QUALITY
3. CLARITY AND REASONING

Score every category from 0 to 100.

Then calculate the average of the three
categories for each side.

Return ONLY valid JSON.

Exactly this structure:

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
        temperature=0.15,
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

        a_argument = clamp_score(
            data.get("A_argument", 50)
        )

        a_rebuttal = clamp_score(
            data.get("A_rebuttal", 50)
        )

        a_clarity = clamp_score(
            data.get("A_clarity", 50)
        )

        b_argument = clamp_score(
            data.get("B_argument", 50)
        )

        b_rebuttal = clamp_score(
            data.get("B_rebuttal", 50)
        )

        b_clarity = clamp_score(
            data.get("B_clarity", 50)
        )

        a_total = (
            a_argument +
            a_rebuttal +
            a_clarity
        ) / 3

        b_total = (
            b_argument +
            b_rebuttal +
            b_clarity
        ) / 3

        return {
            "model": model,

            "A_argument": a_argument,
            "A_rebuttal": a_rebuttal,
            "A_clarity": a_clarity,
            "A_total": round(a_total, 2),

            "B_argument": b_argument,
            "B_rebuttal": b_rebuttal,
            "B_clarity": b_clarity,
            "B_total": round(b_total, 2),

            "winner": (
                "A"
                if a_total > b_total
                else "B"
            ),
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
        f"⚖️ Asking "
        f"{len(judges)} AI judges..."
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
        max_workers=JUDGE_WORKERS
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
                    f"{completed}/"
                    f"{len(judges)}"
                )

            except Exception as exc:

                print(
                    f"   ✗ Judge failed "
                    f"{model}: "
                    f"{str(exc)[:100]}"
                )

    if not results:

        results = [
            neutral_judge(
                "Fallback Judge"
            )
        ]

    return results


def calculate_round_average(results):

    if not results:
        return 50.0, 50.0

    a = sum(
        float(r.get("A_total", 50))
        for r in results
    ) / len(results)

    b = sum(
        float(r.get("B_total", 50))
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

            words.append(
                {
                    "text": chunk["text"],
                    "start": (
                        chunk["offset"]
                        / 10_000_000
                    ),
                    "duration": (
                        chunk["duration"]
                        / 10_000_000
                    ),
                    "end": (
                        chunk["offset"]
                        +
                        chunk["duration"]
                    ) / 10_000_000,
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
# SUBTITLES
# ============================================================

def format_ass_time(seconds):

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


def generate_subtitles(
    words,
    filename,
):

    header = r"""[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: DebateSub,DejaVu Sans,44,&H00FFFFFF,&H0000FFFF,&H00000000,&HCC000000,1,0,0,0,100,100,0,0,1,3,1,5,260,260,180,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    if not words:

        with open(
            filename,
            "w",
            encoding="utf-8",
        ) as file:

            file.write(header)

        return

    # Larger blocks reduce the visual distraction caused by
    # tiny word-boundary timing differences.
    chunk_size = 16

    chunks = []

    for i in range(
        0,
        len(words),
        chunk_size,
    ):

        chunk = words[
            i:i + chunk_size
        ]

        if chunk:
            chunks.append(chunk)

    events = []

    for chunk in chunks:

        paragraph_end = (
            chunk[-1]["end"] + 0.15
        )

        for index, active in enumerate(chunk):

            start = active["start"]

            if index + 1 < len(chunk):

                end = chunk[
                    index + 1
                ]["start"]

            else:

                end = paragraph_end

            rendered_words = []

            for word in chunk:

                if word is active:

                    rendered_words.append(
                        r"{\c&H00FFFF&}"
                        +
                        ass_escape(
                            word["text"]
                        )
                        +
                        r"{\c&HFFFFFF&}"
                    )

                else:

                    rendered_words.append(
                        ass_escape(
                            word["text"]
                        )
                    )

            subtitle = " ".join(
                rendered_words
            )

            events.append(
                "Dialogue: 0,"
                +
                format_ass_time(start)
                +
                ","
                +
                format_ass_time(end)
                +
                ",DebateSub,,0,0,0,,"
                +
                subtitle
            )

    with open(
        filename,
        "w",
        encoding="utf-8",
    ) as file:

        file.write(
            header
            +
            "\n".join(events)
            +
            "\n"
        )


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
            os.path.abspath(
                __file__
            )
        ),
        "background.png",
    )

    if os.path.exists(background):

        try:

            image = (
                Image.open(background)
                .convert("RGB")
                .resize((1920, 1080))
            )

        except Exception:

            image = Image.new(
                "RGB",
                (1920, 1080),
                (12, 16, 32),
            )

    else:

        image = Image.new(
            "RGB",
            (1920, 1080),
            (12, 16, 32),
        )

        draw = ImageDraw.Draw(image)

        for x in range(0, 1920, 60):

            draw.line(
                [(x, 0), (x, 1080)],
                fill=(20, 26, 45),
                width=2,
            )

        for y in range(0, 1080, 60):

            draw.line(
                [(0, y), (1920, y)],
                fill=(20, 26, 45),
                width=2,
            )

    overlay = Image.new(
        "RGBA",
        (1920, 1080),
        (0, 0, 0, 0),
    )

    draw = ImageDraw.Draw(overlay)

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
                1 -
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
# UI CARD
# ============================================================

def create_ui_overlay(
    speaker_name,
    role_label,
    topic,
    position,
    glow_color,
    filename,
):

    image = Image.new(
        "RGBA",
        (1920, 1080),
        (0, 0, 0, 0),
    )

    draw = ImageDraw.Draw(image)

    title_font = load_font(
        30,
        bold=True,
    )

    name_font = load_font(
        28,
        bold=True,
    )

    role_font = load_font(
        19,
        bold=True,
    )

    title = f"TOPIC: {topic}"

    bbox = draw.textbbox(
        (0, 0),
        title,
        font=title_font,
    )

    width = (
        bbox[2] -
        bbox[0]
    )

    draw.text(
        (
            (1920 - width) // 2,
            24,
        ),
        title,
        font=title_font,
        fill="white",
    )

    # --------------------------------------------------------
    # CARD
    # --------------------------------------------------------

    card_width = 610
    card_height = 92
    card_y = 920

    if position == "left":

        card_x = 80

    elif position == "right":

        card_x = 1230

    else:

        card_x = (
            1920 -
            card_width
        ) // 2

    draw.rounded_rectangle(
        [
            card_x,
            card_y,
            card_x + card_width,
            card_y + card_height,
        ],
        radius=15,
        fill=(18, 26, 46, 235),
        outline=glow_color,
        width=3,
    )

    draw.ellipse(
        [
            card_x + 22,
            card_y + 34,
            card_x + 42,
            card_y + 54,
        ],
        fill=glow_color,
    )

    draw.text(
        (
            card_x + 60,
            card_y + 12,
        ),
        speaker_name,
        font=name_font,
        fill="white",
    )

    draw.text(
        (
            card_x + 60,
            card_y + 52,
        ),
        role_label.upper(),
        font=role_font,
        fill=glow_color,
    )

    image.save(filename)

    return card_x


# ============================================================
# SCOREBOARD IMAGE — FIXED
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
            os.path.abspath(
                __file__
            )
        ),
        "background.png",
    )

    if os.path.exists(background):

        try:

            image = (
                Image.open(background)
                .convert("RGB")
                .resize((1920, 1080))
            )

        except Exception:

            image = Image.new(
                "RGB",
                (1920, 1080),
                (12, 16, 32),
            )

    else:

        image = Image.new(
            "RGB",
            (1920, 1080),
            (12, 16, 32),
        )

    overlay = Image.new(
        "RGBA",
        (1920, 1080),
        (0, 0, 0, 220),
    )

    image = Image.alpha_composite(
        image.convert("RGBA"),
        overlay,
    ).convert("RGB")

    draw = ImageDraw.Draw(image)

    header = load_font(
        36,
        bold=True,
    )

    sub = load_font(
        23,
        bold=True,
    )

    small = load_font(18)

    # --------------------------------------------------------
    # IMPORTANT:
    #
    # Every draw.text() call below explicitly uses font=.
    # This prevents Pillow from interpreting the font as
    # the fill argument and producing:
    #
    # ImageDraw.text() got multiple values for argument 'fill'
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
            box[2] -
            box[0]
        )

        draw.text(
            (
                (1920 - width) // 2,
                y,
            ),
            text,
            font=font,
            fill=fill,
        )

    judge_count = max(
        1,
        len(results),
    )

    centred(
        25,
        f"ROUND {round_num} — AI PANEL SCORECARD",
        header,
        "#FFD700",
    )

    centred(
        72,
        (
            f"{judge_count} AI JUDGES • "
            f"THREE CATEGORIES • EACH SCORE 0–100"
        ),
        sub,
        "white",
    )

    centred(
        115,
        (
            f"ROUND AVERAGE   "
            f"APOLOGIST {round_a:.1f}   "
            f"VS   "
            f"SKEPTIC {round_b:.1f}"
        ),
        sub,
        "white",
    )

    centred(
        155,
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
    # CATEGORY AVERAGES
    # --------------------------------------------------------

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

    draw.text(
        (120, 215),
        "CATEGORY AVERAGES",
        font=sub,
        fill="#FFD700",
    )

    draw.text(
        (500, 250),
        "APOLOGIST",
        font=small,
        fill="#00FFCC",
    )

    draw.text(
        (680, 250),
        "SKEPTIC",
        font=small,
        fill="#FF66FF",
    )

    y = 285

    for label, a_key, b_key in categories:

        a = sum(
            float(r.get(a_key, 50))
            for r in results
        ) / judge_count

        b = sum(
            float(r.get(b_key, 50))
            for r in results
        ) / judge_count

        draw.text(
            (120, y),
            label,
            font=small,
            fill="white",
        )

        draw.text(
            (500, y),
            f"{a:.1f}",
            font=small,
            fill="#00FFCC",
        )

        draw.text(
            (680, y),
            f"{b:.1f}",
            font=small,
            fill="#FF66FF",
        )

        y += 35

    # --------------------------------------------------------
    # INDIVIDUAL JUDGE SCORES
    # --------------------------------------------------------

    draw.text(
        (1000, 215),
        "INDIVIDUAL JUDGE SCORES",
        font=sub,
        fill="#FFD700",
    )

    draw.text(
        (1000, 250),
        "Judge",
        font=small,
        fill="white",
    )

    draw.text(
        (1570, 250),
        "A",
        font=small,
        fill="#00FFCC",
    )

    draw.text(
        (1630, 250),
        "B",
        font=small,
        fill="#FF66FF",
    )

    row_height = 25
    start_y = 285

    for index, result in enumerate(results):

        y = (
            start_y +
            index * row_height
        )

        name = str(
            result.get(
                "model",
                f"Judge {index + 1}",
            )
        )

        if len(name) > 48:

            name = (
                name[:45]
                +
                "..."
            )

        draw.text(
            (1000, y),
            name,
            font=small,
            fill="white",
        )

        draw.text(
            (1570, y),
            f"{float(result.get('A_total', 50)):.0f}",
            font=small,
            fill="#00FFCC",
        )

        draw.text(
            (1630, y),
            f"{float(result.get('B_total', 50)):.0f}",
            font=small,
            fill="#FF66FF",
        )

    # --------------------------------------------------------
    # IF MORE RESULTS EVER GET PASSED IN THAN FIT ON SCREEN
    # --------------------------------------------------------

    max_visible = 30

    if len(results) > max_visible:

        draw.text(
            (1000, 1035),
            (
                f"+ {len(results) - max_visible} "
                f"additional judges included in the average"
            ),
            font=small,
            fill="#FFD700",
        )

    image.save(filename)


# ============================================================
# FFMPEG PATH HANDLING
# ============================================================

def ffmpeg_filter_path(filename):

    path = os.path.abspath(
        filename
    )

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
):

    if not os.path.exists(ass):

        raise FileNotFoundError(
            f"Subtitle file was not created: "
            f"{os.path.abspath(ass)}"
        )

    if not os.path.exists(audio):

        raise FileNotFoundError(
            f"Audio file was not created: "
            f"{os.path.abspath(audio)}"
        )

    if not os.path.exists(background):

        raise FileNotFoundError(
            f"Background file missing: "
            f"{os.path.abspath(background)}"
        )

    if not os.path.exists(ui):

        raise FileNotFoundError(
            f"UI file missing: "
            f"{os.path.abspath(ui)}"
        )

    ass_path = ffmpeg_filter_path(
        ass
    )

    glow = glow_color.lstrip("#")

    wave_x = card_x + 365
    wave_y = 943

    wave_x = max(
        10,
        min(
            wave_x,
            1680,
        ),
    )

    if position == "left":

        pan_x = "0"

    elif position == "right":

        pan_x = "iw-(iw/zoom)"

    else:

        pan_x = "(iw-(iw/zoom))/2"

    pan_y = "(ih-(ih/zoom))/2"

    filter_complex = (
        "[0:v]"
        "scale=1920:1080,"
        "zoompan="
        "z='min(zoom+0.00025,1.08)':"
        f"x='{pan_x}':"
        f"y='{pan_y}':"
        "d=9000:"
        "s=1920x1080:"
        "fps=30"
        "[bg];"

        "[1:v]"
        "scale=1920:1080"
        "[ui];"

        "[2:a]"
        "showwaves="
        "s=210x42:"
        "mode=cline:"
        f"colors=0x{glow}:"
        "rate=30"
        "[wave];"

        "[bg]"
        "[ui]"
        "overlay=0:0"
        "[base];"

        "[base]"
        "[wave]"
        f"overlay={wave_x}:{wave_y}"
        "[withwave];"

        "[withwave]"
        f"ass='{ass_path}'"
        "[outv]"
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
            "\n❌ FFmpeg failed."
        )

        print(
            result.stderr[-5000:]
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
    )

    create_background(
        position,
        glow,
        background_file,
    )

    card_x = create_ui_overlay(
        speaker_name,
        role,
        topic,
        position,
        glow,
        ui_file,
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

    recent = "\n".join(
        previous_comments[-8:]
    )

    preferred_side = (
        "AI Christian Apologist"
        if side == "A"
        else
        "AI Skeptic"
    )

    prompt = f"""
You are one member of an independent AI judging panel.

Topic:
{topic}

Round:
{round_num}

You preferred:
{preferred_side}

Do NOT summarise the debate.

Do NOT repeat the debaters' arguments.

Do NOT quote them.

Do NOT mention your AI model.

Do NOT mention any company.

Do NOT simply say that one side was "more convincing".

Give a genuinely new observation about the quality of reasoning.

Previous panel observations:

{recent}

Your observation MUST introduce an idea that has not already
been used by those comments.

Write only 2 or 3 natural spoken sentences.

Make it sound like a thoughtful commentator speaking to viewers.
"""

    response = query_openrouter(
        prompt,
        model,
        timeout=40,
        max_tokens=220,
        temperature=0.9,
    )

    if not response:

        return (
            "What matters here is not simply which conclusion "
            "sounds more appealing, but which side left fewer "
            "important assumptions unexplained."
        )

    return response


# ============================================================
# INTRO / OUTRO
# ============================================================

def build_intro(
    topic,
    judge_count,
):

    return (
        "Welcome to the AI Debate Arena. "
        "Today, an AI Christian Apologist faces an AI Skeptic "
        "on one of humanity's biggest questions. "
        f"After three rounds, an independent panel of "
        f"{judge_count} available AI judges will evaluate "
        "both sides. "
        "Each judge will score argument strength, rebuttal quality, "
        "and clarity of reasoning. "
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

        result = (
            "the AI Christian Apologist"
        )

    else:

        result = (
            "the AI Skeptic"
        )

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
            result.stderr[-5000:]
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
    print(
        f"\nTOPIC: {topic}\n"
    )

    # --------------------------------------------------------
    # MODEL DISCOVERY
    # --------------------------------------------------------

    available_models = discover_models()

    if not available_models:

        print(
            "⚠️ Dynamic discovery failed."
        )

        available_models = (
            FALLBACK_MODELS.copy()
        )

    # --------------------------------------------------------
    # PRIMARY MODELS
    # --------------------------------------------------------

    apologist_model, skeptic_model = (
        choose_primary_models(
            available_models
        )
    )

    # --------------------------------------------------------
    # JUDGES
    # --------------------------------------------------------

    judges = choose_judges(
        available_models,
        (
            apologist_model,
            skeptic_model,
        ),
    )

    if not judges:

        judges = [
            model
            for model in FALLBACK_MODELS
            if model not in (
                apologist_model,
                skeptic_model,
            )
        ][:MAX_JUDGES]

    print(
        "🎤 Debate generation models selected internally."
    )

    print(
        f"⚖️ Maximum judging panel: "
        f"{MAX_JUDGES}"
    )

    print(
        f"⚖️ Actual judging panel: "
        f"{len(judges)}"
    )

    print(
        "📺 Viewer-facing identities:"
    )

    print(
        "   AI Christian Apologist"
    )

    print(
        "   AI Skeptic"
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
        "Moderator",
    )

    add_segment(
        f"Today's question is: {topic}",
        "Moderator",
        "Moderator",
    )

    # --------------------------------------------------------
    # DEBATE HISTORY
    # --------------------------------------------------------

    previous_apologist = ""
    previous_skeptic = ""

    cumulative_a = 0.0
    cumulative_b = 0.0

    panel_comments = []

    # --------------------------------------------------------
    # THREE ROUNDS
    # --------------------------------------------------------

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

        # ----------------------------------------------------
        # APOLOGIST
        # ----------------------------------------------------

        apologist_text = generate_apologist(
            topic,
            round_num,
            previous_apologist,
            previous_skeptic,
            apologist_model,
        )

        print(
            f"🟢 Apologist: "
            f"{count_words(apologist_text)} words"
        )

        add_segment(
            apologist_text,
            "AI Christian Apologist",
            "AI Christian Apologist",
            "left",
            "#00FFCC",
        )

        # ----------------------------------------------------
        # SKEPTIC
        # ----------------------------------------------------

        skeptic_text = generate_skeptic(
            topic,
            round_num,
            apologist_text,
            previous_skeptic,
            skeptic_model,
        )

        print(
            f"🟣 Skeptic: "
            f"{count_words(skeptic_text)} words"
        )

        add_segment(
            skeptic_text,
            "AI Skeptic",
            "AI Skeptic",
            "right",
            "#FF00FF",
        )

        # ----------------------------------------------------
        # SAVE HISTORY
        # ----------------------------------------------------

        previous_apologist = apologist_text
        previous_skeptic = skeptic_text

        # ----------------------------------------------------
        # JUDGING
        # ----------------------------------------------------

        results = evaluate_round(
            judges,
            topic,
            round_num,
            apologist_text,
            skeptic_text,
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
        # SCOREBOARD
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
            f"The {len(results)} AI judges gave the "
            f"AI Christian Apologist an average score "
            f"of {round_a:.1f}, and the AI Skeptic an "
            f"average score of {round_b:.1f}. "
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

        score_ui = (
            f"score_ui_r{round_num}.png"
        )

        score_video = (
            f"score_video_r{round_num}.mp4"
        )

        score_words = generate_audio(
            score_text,
            "Moderator",
            score_audio,
        )

        generate_subtitles(
            score_words,
            score_subs,
        )

        score_card_x = create_ui_overlay(
            "Moderator",
            "Scoreboard",
            topic,
            "center",
            "#FFD700",
            score_ui,
        )

        render_video_segment(
            scoreboard_file,
            score_ui,
            score_audio,
            score_subs,
            score_video,
            "center",
            "#FFD700",
            score_card_x,
        )

        segments.append(
            score_video
        )

        # ----------------------------------------------------
        # PANEL COMMENTARY
        # ----------------------------------------------------

        a_results = [
            r
            for r in results
            if r.get("winner") == "A"
        ]

        b_results = [
            r
            for r in results
            if r.get("winner") == "B"
        ]

        if not a_results:
            a_results = results

        if not b_results:
            b_results = results

        if results:

            judge_a = random.choice(
                a_results
            )

            judge_b = random.choice(
                b_results
            )

            comment_a = generate_panel_commentary(
                judge_a["model"],
                "A",
                topic,
                round_num,
                apologist_text,
                skeptic_text,
                panel_comments,
            )

            panel_comments.append(
                comment_a
            )

            add_segment(
                comment_a,
                "Panelist 1",
                "AI Panel Judge",
                "center",
                "#3399FF",
            )

            comment_b = generate_panel_commentary(
                judge_b["model"],
                "B",
                topic,
                round_num,
                apologist_text,
                skeptic_text,
                panel_comments,
            )

            panel_comments.append(
                comment_b
            )

            add_segment(
                comment_b,
                "Panelist 2",
                "AI Panel Judge",
                "center",
                "#3399FF",
            )

    # --------------------------------------------------------
    # FINAL RESULT
    # --------------------------------------------------------

    add_segment(
        build_outro(
            len(judges),
            cumulative_a,
            cumulative_b,
        ),
        "Moderator",
        "Moderator",
    )

    add_segment(
        (
            "That concludes today's AI Debate Arena. "
            "The arguments have been presented and the "
            "panel has delivered its verdict. "
            "But what do you think? "
            "Subscribe for more AI debates and let us know "
            "which side you believe made the stronger case."
        ),
        "Moderator",
        "Moderator",
    )

    # --------------------------------------------------------
    # STITCH
    # --------------------------------------------------------

    stitch_segments(
        segments,
        OUTPUT_FILE,
    )

    # --------------------------------------------------------
    # SUCCESS
    # --------------------------------------------------------

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
