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
# AI DEBATE ARENA — STABLE VIDEO RENDERER
# ============================================================

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"

OUTPUT_FILE = "final_debate_output.mp4"

VIDEO_W = 1920
VIDEO_H = 1080
FPS = 30

ROUNDS = 3

# 4 exchanges x ~125 words = ~500 words per side per round.
TURNS_PER_SIDE_PER_ROUND = 4
WORDS_PER_TURN = 125
MIN_TURN_WORDS = 105
MAX_TURN_WORDS = 145

# Seven independent providers maximum.
MAX_JUDGES = 7
JUDGE_WORKERS = 7

MODEL_DISCOVERY_TIMEOUT = 20
MODEL_REQUEST_TIMEOUT = 60
JUDGE_REQUEST_TIMEOUT = 35


# ============================================================
# VOICES
# ============================================================

VOICES = {
    "Moderator": "en-US-AndrewMultilingualNeural",
    "AI Christian Apologist": "en-US-BrianMultilingualNeural",
    "AI Skeptic": "en-US-AvaMultilingualNeural",

    # Commentary deliberately uses different voices.
    "Judge 1": "en-US-ChristopherNeural",
    "Judge 2": "en-US-EmmaMultilingualNeural",
    "Judge 3": "en-US-RyanMultilingualNeural",
    "Judge 4": "en-US-AriaNeural",
    "Judge 5": "en-US-GuyNeural",
    "Judge 6": "en-US-JennyNeural",
    "Judge 7": "en-US-DavisNeural",
}


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
# PROVIDERS
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
}


def provider_from_model(model):

    if not model:
        return "Unknown"

    prefix = model.split("/", 1)[0].lower()

    return PROVIDER_ALIASES.get(
        prefix,
        prefix.replace("-", " ").title()
    )


def short_model_name(model):

    if not model:
        return "Unknown"

    name = model.split("/", 1)[-1]

    name = re.sub(
        r"[-_](instruct|chat|thinking|preview)$",
        "",
        name,
        flags=re.I,
    )

    if len(name) > 24:
        name = name[:21] + "..."

    return name


# ============================================================
# CLEANUP
# ============================================================

def cleanup_cache():

    patterns = [
        "*.mp4",
        "*.mp3",
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


def clamp(value):

    try:
        value = float(value)
    except Exception:
        value = 50

    return max(0, min(100, value))


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

    for a, b in replacements.items():
        text = text.replace(a, b)

    return re.sub(r"\s+", " ", text).strip()


def load_font(size, bold=False):

    if bold:
        paths = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        ]
    else:
        paths = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        ]

    for path in paths:

        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass

    return ImageFont.load_default()


# ============================================================
# OPENROUTER
# ============================================================

def headers():

    return {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://openrouter.ai/",
        "X-Title": "AI Debate Arena",
    }


def discover_models():

    try:

        r = requests.get(
            OPENROUTER_MODELS_URL,
            headers=headers(),
            timeout=MODEL_DISCOVERY_TIMEOUT,
        )

        if r.status_code != 200:
            return []

        models = []

        for item in r.json().get("data", []):

            model = item.get("id")

            if not model:
                continue

            low = model.lower()

            if any(
                x in low
                for x in [
                    "embed",
                    "tts",
                    "whisper",
                    "audio",
                    "image",
                    "vision",
                    "moderation",
                    "guard",
                ]
            ):
                continue

            models.append(model)

        return list(dict.fromkeys(models))

    except Exception as exc:

        print("Model discovery failed:", exc)

        return []


def ask_model(
    prompt,
    model,
    timeout=MODEL_REQUEST_TIMEOUT,
    max_tokens=1000,
    temperature=0.7,
):

    payload = {
        "model": model,
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

            r = requests.post(
                OPENROUTER_URL,
                headers=headers(),
                json=payload,
                timeout=timeout,
            )

            if r.status_code == 200:

                choices = r.json().get(
                    "choices",
                    [],
                )

                if choices:

                    text = (
                        choices[0]
                        .get("message", {})
                        .get("content", "")
                    )

                    if text and len(text.strip()) > 20:
                        return text.strip()

            else:

                print(
                    f"⚠️ {provider_from_model(model)} "
                    f"HTTP {r.status_code}"
                )

        except Exception as exc:

            print(
                f"⚠️ {provider_from_model(model)} "
                f"{str(exc)[:100]}"
            )

        time.sleep(1 + attempt)

    return None


# ============================================================
# MODEL SELECTION
# ============================================================

def choose_debate_models(models):

    preferred = [
        "openai/gpt-4o",
        "openai/gpt-4o-mini",
        "openai/gpt-4.1-mini",
        "anthropic/claude-3.5-sonnet",
        "anthropic/claude-3.5-haiku",
        "google/gemini-2.5-flash",
        "google/gemini-2.0-flash-001",
        "deepseek/deepseek-chat",
    ]

    for first in preferred:

        if first in models:

            for second in preferred:

                if (
                    second in models
                    and provider_from_model(first)
                    != provider_from_model(second)
                ):
                    return first, second

    return (
        FALLBACK_MODELS[0],
        FALLBACK_MODELS[1],
    )


def choose_judges(models, debate_models):

    used = {
        provider_from_model(x)
        for x in debate_models
    }

    candidates = [
        x
        for x in models
        if provider_from_model(x) not in used
    ]

    # Preferred order for a strong, diverse panel.
    preferred_providers = [
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

    groups = {}

    for model in candidates:

        provider = provider_from_model(model)

        groups.setdefault(
            provider,
            [],
        ).append(model)

    selected = []

    # STRICTLY ONE MODEL PER PROVIDER.
    for provider in preferred_providers:

        if provider not in groups:
            continue

        # Prefer shorter, established-looking model IDs.
        candidate = sorted(
            groups[provider],
            key=lambda x: (
                len(x),
                x,
            ),
        )[0]

        selected.append(candidate)

        if len(selected) >= MAX_JUDGES:
            break

    if len(selected) < MAX_JUDGES:

        for provider, provider_models in groups.items():

            if provider in {
                provider_from_model(x)
                for x in selected
            }:
                continue

            selected.append(
                sorted(
                    provider_models,
                    key=len,
                )[0]
            )

            if len(selected) >= MAX_JUDGES:
                break

    return selected[:MAX_JUDGES]


# ============================================================
# DEBATE GENERATION
# ============================================================

def generate_turn(
    side,
    topic,
    round_num,
    turn_num,
    history,
    model,
):

    if side == "A":
        role = "AI Christian Apologist"
        opponent = "AI Skeptic"
    else:
        role = "AI Skeptic"
        opponent = "AI Christian Apologist"

    prompt = f"""
You are the {role} in a serious public debate.

Topic:
{topic}

Round:
{round_num}

Turn:
{turn_num}

Opponent:
{opponent}

Previous exchange:
{history or "This is the opening."}

Write approximately {WORDS_PER_TURN} words.

Target:
{MIN_TURN_WORDS}-{MAX_TURN_WORDS} words.

Respond directly to what was just said.

Do not restart the debate.

Do not introduce yourself.

Do not say "in this round".

Do not mention AI models or companies.

Do not use headings or numbered lists.

Use natural spoken YouTube language.

Give a substantive argument or rebuttal.

Write ONLY the spoken contribution.
"""

    result = ask_model(
        prompt,
        model,
        max_tokens=420,
        temperature=0.78,
    )

    if result:
        return result

    return (
        "The important issue is whether the evidence really "
        "supports that conclusion, or whether the argument "
        "requires assumptions that have not yet been established."
    )


def generate_round(
    topic,
    round_num,
    model_a,
    model_b,
    previous_history,
):

    a_turns = []
    b_turns = []

    history = previous_history

    for turn in range(
        1,
        TURNS_PER_SIDE_PER_ROUND + 1,
    ):

        a = generate_turn(
            "A",
            topic,
            round_num,
            turn,
            history,
            model_a,
        )

        a_turns.append(a)

        history += (
            "\n\nAI CHRISTIAN APOLOGIST:\n"
            + a
        )

        b = generate_turn(
            "B",
            topic,
            round_num,
            turn,
            history,
            model_b,
        )

        b_turns.append(b)

        history += (
            "\n\nAI SKEPTIC:\n"
            + b
        )

    return a_turns, b_turns, history


# ============================================================
# JUDGING
# ============================================================

def judge_one(
    model,
    topic,
    round_num,
    apologist,
    skeptic,
):

    prompt = f"""
You are an impartial debate judge.

Topic:
{topic}

Round:
{round_num}

SIDE A:
{apologist}

SIDE B:
{skeptic}

Score both sides independently.

Categories:

Argument strength
Rebuttal quality
Clarity and reasoning

Scores must be 0-100.

Return ONLY JSON:

{{
"A_argument": 0,
"A_rebuttal": 0,
"A_clarity": 0,
"B_argument": 0,
"B_rebuttal": 0,
"B_clarity": 0
}}
"""

    response = ask_model(
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
            re.S,
        )

        if not match:
            return neutral_judge(model)

        data = json.loads(
            match.group(0)
        )

        aa = clamp(data.get("A_argument", 50))
        ar = clamp(data.get("A_rebuttal", 50))
        ac = clamp(data.get("A_clarity", 50))

        ba = clamp(data.get("B_argument", 50))
        br = clamp(data.get("B_rebuttal", 50))
        bc = clamp(data.get("B_clarity", 50))

        at = (aa + ar + ac) / 3
        bt = (ba + br + bc) / 3

        return {
            "model": model,
            "provider": provider_from_model(model),
            "A_argument": aa,
            "A_rebuttal": ar,
            "A_clarity": ac,
            "A_total": round(at, 1),
            "B_argument": ba,
            "B_rebuttal": br,
            "B_clarity": bc,
            "B_total": round(bt, 1),
            "winner": "A" if at > bt else "B",
        }

    except Exception:
        return neutral_judge(model)


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
    judges,
    topic,
    round_num,
    a,
    b,
):

    results = []

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(
            JUDGE_WORKERS,
            len(judges),
        )
    ) as executor:

        futures = [
            executor.submit(
                judge_one,
                model,
                topic,
                round_num,
                a,
                b,
            )
            for model in judges
        ]

        for i, future in enumerate(
            concurrent.futures.as_completed(futures),
            1,
        ):

            result = future.result()

            results.append(result)

            print(
                f"   ✓ Judge {i}/{len(judges)} "
                f"— {result['provider']}"
            )

    if not results:
        results = [
            neutral_judge("fallback/fallback")
        ]

    return results


def averages(results):

    a = sum(x["A_total"] for x in results) / len(results)
    b = sum(x["B_total"] for x in results) / len(results)

    return round(a, 1), round(b, 1)


# ============================================================
# TTS
# ============================================================

async def tts_async(
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
                "end": start + duration,
            })

    with open(filename, "wb") as f:
        f.write(audio)

    return words


def generate_audio(
    text,
    role,
    filename,
):

    if role.startswith("Judge"):

        voice = VOICES.get(
            role,
            VOICES["Judge 1"],
        )

    else:

        voice = VOICES.get(
            role,
            VOICES["Moderator"],
        )

    clean = clean_for_speech(text)

    return asyncio.run(
        tts_async(
            clean,
            voice,
            filename,
        )
    )


# ============================================================
# SUBTITLE PNG SYSTEM
#
# NO ASS / LIBASS.
# Subtitles are ordinary PNG images placed directly into
# the FFmpeg video filter graph.
# ============================================================

def wrap_text(
    draw,
    text,
    font,
    max_width,
):

    words = text.split()

    lines = []
    current = ""

    for word in words:

        test = (
            word
            if not current
            else current + " " + word
        )

        box = draw.textbbox(
            (0, 0),
            test,
            font=font,
        )

        if box[2] - box[0] <= max_width:
            current = test
        else:

            if current:
                lines.append(current)

            current = word

    if current:
        lines.append(current)

    return lines


def create_subtitle_blocks(
    words,
    prefix,
):

    if not words:
        return []

    blocks = []

    # 12 words per block gives a small paragraph,
    # while still changing frequently enough to follow speech.
    BLOCK_WORDS = 12

    for i in range(
        0,
        len(words),
        BLOCK_WORDS,
    ):

        chunk = words[
            i:i + BLOCK_WORDS
        ]

        start = chunk[0]["start"]

        if i + BLOCK_WORDS < len(words):
            end = words[
                i + BLOCK_WORDS
            ]["start"]
        else:
            end = chunk[-1]["end"] + 0.15

        filename = (
            f"{prefix}_subtitle_{i}.png"
        )

        image = Image.new(
            "RGBA",
            (1700, 190),
            (0, 0, 0, 0),
        )

        draw = ImageDraw.Draw(image)

        font = load_font(
            42,
            bold=True,
        )

        text = " ".join(
            x["text"]
            for x in chunk
        )

        lines = wrap_text(
            draw,
            text,
            font,
            1580,
        )

        # Maximum two lines.
        lines = lines[:2]

        total_height = len(lines) * 52

        y = (
            95 -
            total_height / 2
        )

        for line in lines:

            box = draw.textbbox(
                (0, 0),
                line,
                font=font,
            )

            width = (
                box[2] -
                box[0]
            )

            x = (
                1700 -
                width
            ) / 2

            # Thick dark outline.
            draw.text(
                (x, y),
                line,
                font=font,
                fill="white",
                stroke_width=4,
                stroke_fill=(0, 0, 0, 255),
            )

            y += 52

        image.save(filename)

        blocks.append({
            "file": filename,
            "start": start,
            "end": end,
        })

    return blocks


# ============================================================
# VISUAL CUES
# ============================================================

VISUALS = {
    "garden of eden": ("GARDEN OF EDEN", "garden"),
    "adam": ("ADAM", "person"),
    "eve": ("EVE", "person"),
    "apple": ("FRUIT", "fruit"),
    "fruit": ("FRUIT", "fruit"),
    "tree": ("TREE", "tree"),
    "big bang": ("BIG BANG", "universe"),
    "universe": ("UNIVERSE", "universe"),
    "earth": ("EARTH", "earth"),
    "bible": ("BIBLE", "book"),
    "scripture": ("SCRIPTURE", "book"),
    "creator": ("CREATOR", "creator"),
    "god": ("GOD / CREATOR", "creator"),
    "evolution": ("EVOLUTION", "evolution"),
    "science": ("SCIENCE", "science"),
    "consciousness": ("CONSCIOUSNESS", "mind"),
}


def detect_visuals(text, words):

    lower = text.lower()
    found = []

    for phrase, (label, kind) in VISUALS.items():

        pos = lower.find(phrase)

        if pos < 0:
            continue

        ratio = pos / max(1, len(text))

        index = min(
            len(words) - 1,
            int(ratio * len(words)),
        )

        start = words[index]["start"]

        end_index = min(
            len(words) - 1,
            index + 18,
        )

        end = words[end_index]["end"] + 0.4

        found.append({
            "label": label,
            "kind": kind,
            "start": start,
            "end": end,
        })

    found.sort(
        key=lambda x: x["start"]
    )

    # Avoid a screen full of competing graphics.
    output = []

    for item in found:

        if any(
            abs(item["start"] - x["start"]) < 1.5
            for x in output
        ):
            continue

        output.append(item)

        if len(output) >= 3:
            break

    return output


def draw_visual(
    kind,
    label,
):

    image = Image.new(
        "RGBA",
        (650, 300),
        (0, 0, 0, 0),
    )

    draw = ImageDraw.Draw(image)

    # Card.
    draw.rounded_rectangle(
        (5, 5, 645, 295),
        radius=28,
        fill=(10, 18, 35, 235),
        outline=(255, 215, 0, 240),
        width=4,
    )

    cx = 325
    cy = 125

    # Simple illustrated symbols.
    if kind == "garden" or kind == "tree":

        draw.rectangle(
            (cx - 15, cy, cx + 15, 240),
            fill=(120, 75, 35, 255),
        )

        for dx, dy, r in [
            (-60, -10, 65),
            (0, -55, 75),
            (60, -10, 65),
        ]:

            draw.ellipse(
                (
                    cx + dx - r,
                    cy + dy - r,
                    cx + dx + r,
                    cy + dy + r,
                ),
                fill=(65, 160, 80, 255),
            )

    elif kind == "fruit":

        draw.ellipse(
            (cx - 65, cy - 60, cx + 65, cy + 70),
            fill=(220, 60, 55, 255),
            outline="white",
            width=4,
        )

        draw.line(
            (cx, cy - 55, cx + 20, cy - 90),
            fill=(80, 140, 60, 255),
            width=8,
        )

    elif kind == "person":

        draw.ellipse(
            (cx - 30, cy - 85, cx + 30, cy - 25),
            fill=(235, 190, 150, 255),
        )

        draw.line(
            (cx, cy - 20, cx, cy + 90),
            fill=(80, 150, 230, 255),
            width=18,
        )

        draw.line(
            (cx, cy + 20, cx - 65, cy + 60),
            fill=(80, 150, 230, 255),
            width=10,
        )

        draw.line(
            (cx, cy + 20, cx + 65, cy + 60),
            fill=(80, 150, 230, 255),
            width=10,
        )

    elif kind in ("universe", "earth"):

        draw.ellipse(
            (cx - 75, cy - 75, cx + 75, cy + 75),
            fill=(45, 100, 210, 255),
            outline="white",
            width=4,
        )

    elif kind == "book":

        draw.rounded_rectangle(
            (cx - 100, cy - 70, cx + 100, cy + 70),
            radius=12,
            fill=(100, 60, 35, 255),
            outline="white",
            width=4,
        )

        draw.line(
            (cx, cy - 65, cx, cy + 65),
            fill="white",
            width=4,
        )

    else:

        draw.ellipse(
            (cx - 75, cy - 75, cx + 75, cy + 75),
            fill=(40, 55, 90, 255),
            outline=(255, 215, 0, 255),
            width=4,
        )

    font = load_font(
        30,
        bold=True,
    )

    box = draw.textbbox(
        (0, 0),
        label,
        font=font,
    )

    tw = box[2] - box[0]

    draw.text(
        (
            325 - tw / 2,
            220,
        ),
        label,
        font=font,
        fill="white",
    )

    return image


# ============================================================
# BACKGROUND
# ============================================================

def create_background(
    filename,
    glow="#00FFCC",
):

    if os.path.exists("background.png"):

        try:

            image = Image.open(
                "background.png"
            ).convert("RGB")

            image = image.resize(
                (VIDEO_W, VIDEO_H)
            )

        except Exception:

            image = Image.new(
                "RGB",
                (VIDEO_W, VIDEO_H),
                (12, 16, 32),
            )

    else:

        image = Image.new(
            "RGB",
            (VIDEO_W, VIDEO_H),
            (12, 16, 32),
        )

        draw = ImageDraw.Draw(image)

        for x in range(0, VIDEO_W, 80):

            draw.line(
                (x, 0, x, VIDEO_H),
                fill=(20, 27, 46),
                width=2,
            )

        for y in range(0, VIDEO_H, 80):

            draw.line(
                (0, y, VIDEO_W, y),
                fill=(20, 27, 46),
                width=2,
            )

    image.save(filename)


# ============================================================
# SPEAKER UI
# ============================================================

def create_speaker_ui(
    name,
    topic,
    glow,
    filename,
    position,
):

    image = Image.new(
        "RGBA",
        (VIDEO_W, VIDEO_H),
        (0, 0, 0, 0),
    )

    draw = ImageDraw.Draw(image)

    title_font = load_font(
        29,
        bold=True,
    )

    name_font = load_font(
        30,
        bold=True,
    )

    # Topic.
    title = f"TOPIC: {topic}"

    box = draw.textbbox(
        (0, 0),
        title,
        font=title_font,
    )

    draw.text(
        (
            (VIDEO_W - (box[2] - box[0])) / 2,
            25,
        ),
        title,
        font=title_font,
        fill="white",
    )

    card_w = 650
    card_h = 105
    card_y = 875

    if position == "left":
        card_x = 65
    elif position == "right":
        card_x = 1205
    else:
        card_x = (VIDEO_W - card_w) // 2

    draw.rounded_rectangle(
        (
            card_x,
            card_y,
            card_x + card_w,
            card_y + card_h,
        ),
        radius=18,
        fill=(12, 20, 38, 235),
        outline=glow,
        width=4,
    )

    draw.ellipse(
        (
            card_x + 22,
            card_y + 39,
            card_x + 46,
            card_y + 63,
        ),
        fill=glow,
    )

    draw.text(
        (
            card_x + 65,
            card_y + 28,
        ),
        name,
        font=name_font,
        fill="white",
    )

    image.save(filename)

    return card_x, card_y


# ============================================================
# SCORECARD
#
# IMPORTANT:
# This is deliberately a completely separate renderer.
# NO ZOOM.
# NO speaker card.
# NO waveform.
# NO visual cue.
# ============================================================

def create_scorecard(
    round_num,
    results,
    round_a,
    round_b,
    cumulative_a,
    cumulative_b,
    filename,
):

    image = Image.new(
        "RGB",
        (VIDEO_W, VIDEO_H),
        (9, 14, 27),
    )

    draw = ImageDraw.Draw(image)

    title = load_font(
        40,
        bold=True,
    )

    sub = load_font(
        24,
        bold=True,
    )

    body = load_font(
        21,
    )

    small = load_font(
        18,
    )

    def centre(y, text, font, fill):

        box = draw.textbbox(
            (0, 0),
            text,
            font=font,
        )

        width = box[2] - box[0]

        draw.text(
            (
                (VIDEO_W - width) / 2,
                y,
            ),
            text,
            font=font,
            fill=fill,
        )

    centre(
        35,
        f"ROUND {round_num} — AI JUDGING PANEL",
        title,
        (255, 215, 0),
    )

    centre(
        88,
        f"{len(results)} INDEPENDENT PROVIDERS",
        sub,
        "white",
    )

    # Score boxes.
    draw.rounded_rectangle(
        (100, 150, 860, 270),
        radius=18,
        fill=(15, 35, 55),
        outline=(0, 255, 204),
        width=3,
    )

    draw.rounded_rectangle(
        (1060, 150, 1820, 270),
        radius=18,
        fill=(45, 20, 48),
        outline=(255, 0, 255),
        width=3,
    )

    centre(
        175,
        f"APOLOGIST  {round_a:.1f}",
        sub,
        (0, 255, 204),
    )

    # Draw skeptic manually to avoid overlap.
    draw.text(
        (1280, 175),
        f"SKEPTIC  {round_b:.1f}",
        font=sub,
        fill=(255, 100, 255),
    )

    # --------------------------------------------------------
    # Categories.
    # --------------------------------------------------------

    draw.text(
        (100, 315),
        "CATEGORY AVERAGES",
        font=sub,
        fill=(255, 215, 0),
    )

    draw.text(
        (520, 355),
        "APOLOGIST",
        font=small,
        fill=(0, 255, 204),
    )

    draw.text(
        (700, 355),
        "SKEPTIC",
        font=small,
        fill=(255, 100, 255),
    )

    categories = [
        ("Argument strength", "A_argument", "B_argument"),
        ("Rebuttal quality", "A_rebuttal", "B_rebuttal"),
        ("Clarity & reasoning", "A_clarity", "B_clarity"),
    ]

    y = 395

    for label, ak, bk in categories:

        av = sum(
            r[ak]
            for r in results
        ) / len(results)

        bv = sum(
            r[bk]
            for r in results
        ) / len(results)

        draw.text(
            (100, y),
            label,
            font=body,
            fill="white",
        )

        draw.text(
            (530, y),
            f"{av:.1f}",
            font=body,
            fill=(0, 255, 204),
        )

        draw.text(
            (710, y),
            f"{bv:.1f}",
            font=body,
            fill=(255, 100, 255),
        )

        y += 45

    # --------------------------------------------------------
    # Individual judges.
    # --------------------------------------------------------

    draw.text(
        (1000, 315),
        "INDIVIDUAL JUDGES",
        font=sub,
        fill=(255, 215, 0),
    )

    draw.text(
        (1000, 355),
        "PROVIDER",
        font=small,
        fill="white",
    )

    draw.text(
        (1550, 355),
        "A",
        font=small,
        fill=(0, 255, 204),
    )

    draw.text(
        (1640, 355),
        "B",
        font=small,
        fill=(255, 100, 255),
    )

    y = 395

    for result in results:

        provider = result.get(
            "provider",
            "Unknown",
        )

        # Short provider names only.
        if len(provider) > 28:
            provider = provider[:25] + "..."

        draw.text(
            (1000, y),
            provider,
            font=body,
            fill="white",
        )

        draw.text(
            (1535, y),
            f"{result['A_total']:.1f}",
            font=body,
            fill=(0, 255, 204),
        )

        draw.text(
            (1625, y),
            f"{result['B_total']:.1f}",
            font=body,
            fill=(255, 100, 255),
        )

        y += 43

    # --------------------------------------------------------
    # Cumulative.
    # --------------------------------------------------------

    centre(
        595,
        f"CUMULATIVE  {cumulative_a:.1f}  —  {cumulative_b:.1f}",
        sub,
        (255, 215, 0),
    )

    # --------------------------------------------------------
    # Dedicated subtitle-safe zone.
    # Bottom 180px is intentionally blank.
    # --------------------------------------------------------

    draw.line(
        (100, 670, 1820, 670),
        fill=(60, 70, 95),
        width=2,
    )

    image.save(filename)


# ============================================================
# FFMPEG HELPERS
# ============================================================

def duration_of_audio(filename):

    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        filename,
    ]

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        return float(result.stdout.strip())
    except Exception:
        return 1.0


def render_debate_video(
    background,
    ui,
    audio,
    subtitle_blocks,
    visual_cues,
    output,
):

    inputs = [
        "-loop", "1", "-i", background,
        "-loop", "1", "-i", ui,
        "-i", audio,
    ]

    # Subtitle PNG inputs.
    for block in subtitle_blocks:
        inputs += [
            "-loop", "1",
            "-i", block["file"],
        ]

    subtitle_start_index = 3

    # Visual inputs.
    visual_images = []

    for i, cue in enumerate(visual_cues):

        filename = f"visual_{i}.png"

        img = draw_visual(
            cue["kind"],
            cue["label"],
        )

        img.save(filename)

        visual_images.append(
            (filename, cue)
        )

        inputs += [
            "-loop", "1",
            "-i", filename,
        ]

    filters = []

    # Static background.
    filters.append(
        "[0:v]scale=1920:1080[bg]"
    )

    filters.append(
        "[1:v]scale=1920:1080[ui]"
    )

    filters.append(
        "[bg][ui]overlay=0:0[base]"
    )

    current = "[base]"

    # --------------------------------------------------------
    # Visuals ABOVE subtitles.
    # --------------------------------------------------------

    input_index = 3 + len(subtitle_blocks)

    for i, (filename, cue) in enumerate(
        visual_images
    ):

        label = f"v{i}"

        filters.append(
            f"[{input_index}:v]"
            f"scale=650:300"
            f"[{label}]"
        )

        out = f"vis{i}"

        filters.append(
            f"{current}[{label}]"
            f"overlay=635:535:"
            f"enable='between(t,"
            f"{cue['start']:.2f},"
            f"{cue['end']:.2f})'"
            f"[{out}]"
        )

        current = f"[{out}]"

        input_index += 1

    # --------------------------------------------------------
    # Subtitles LAST.
    #
    # They are therefore always on top.
    # Bottom position is fixed and never overlaps scorecards
    # because this renderer is used only for debate segments.
    # --------------------------------------------------------

    for i, block in enumerate(
        subtitle_blocks
    ):

        label = f"s{i}"

        filters.append(
            f"[{subtitle_start_index + i}:v]"
            f"format=rgba"
            f"[{label}]"
        )

        out = f"sub{i}"

        filters.append(
            f"{current}[{label}]"
            f"overlay=110:765:"
            f"enable='between(t,"
            f"{block['start']:.3f},"
            f"{block['end']:.3f})'"
            f"[{out}]"
        )

        current = f"[{out}]"

    filters.append(
        f"{current}[vout]"
    )

    filter_complex = ";".join(filters)

    command = [
        "ffmpeg",
        "-y",
    ]

    command += inputs

    command += [
        "-filter_complex",
        filter_complex,
        "-map",
        "[vout]",
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

        print(result.stderr[-7000:])

        raise RuntimeError(
            f"Debate video failed: {output}"
        )

    # Cleanup generated visual/subtitle PNGs.
    for block in subtitle_blocks:

        try:
            os.remove(block["file"])
        except Exception:
            pass

    for filename, _ in visual_images:

        try:
            os.remove(filename)
        except Exception:
            pass


# ============================================================
# SCORECARD VIDEO
# ============================================================

def render_scorecard_video(
    image,
    audio,
    output,
):

    # No zoom, no waveform, no speaker card.
    # Scorecard remains perfectly static.

    command = [
        "ffmpeg",
        "-y",
        "-loop",
        "1",
        "-framerate",
        str(FPS),
        "-i",
        image,
        "-i",
        audio,
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

        print(result.stderr[-7000:])

        raise RuntimeError(
            f"Scorecard render failed: {output}"
        )


def verify_video(filename):

    if not os.path.exists(filename):
        return False

    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "stream=codec_type",
        "-of",
        "csv=p=0",
        filename,
    ]

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    streams = result.stdout.lower()

    return (
        "video" in streams
        and
        "audio" in streams
    )


# ============================================================
# SEGMENT CREATION
# ============================================================

def create_debate_segment(
    text,
    role,
    name,
    topic,
    segment_id,
    position,
    glow,
):

    audio = f"audio_{segment_id}.mp3"
    background = f"bg_{segment_id}.png"
    ui = f"ui_{segment_id}.png"
    output = f"segment_{segment_id}.mp4"

    words = generate_audio(
        text,
        role,
        audio,
    )

    subtitles = create_subtitle_blocks(
        words,
        f"seg{segment_id}",
    )

    visuals = detect_visuals(
        clean_for_speech(text),
        words,
    )

    create_background(
        background,
        glow,
    )

    create_speaker_ui(
        name,
        topic,
        glow,
        ui,
        position,
    )

    render_debate_video(
        background,
        ui,
        audio,
        subtitles,
        visuals,
        output,
    )

    if not verify_video(output):
        raise RuntimeError(
            f"Invalid debate segment: {output}"
        )

    return output


# ============================================================
# JUDGE COMMENTARY
# ============================================================

def generate_commentary(
    model,
    preferred_side,
    topic,
    round_num,
    a,
    b,
):

    provider = provider_from_model(model)

    prompt = f"""
You are a debate commentator representing
{provider}.

Topic:
{topic}

Round:
{round_num}

You thought the stronger reasoning came from:
{preferred_side}

Give a genuinely useful observation about
the reasoning.

Do not simply say who won.

Do not summarise.

Do not quote either debater.

Write 2 or 3 natural spoken sentences.
"""

    result = ask_model(
        prompt,
        model,
        max_tokens=180,
        temperature=0.8,
    )

    return result or (
        "The most important issue is which side "
        "left fewer important assumptions unexplained."
    )


# ============================================================
# COMMENTARY SEGMENT
# ============================================================

def create_judge_segment(
    text,
    provider,
    judge_number,
    topic,
    segment_id,
):

    role = f"Judge {judge_number}"

    # Provider only — no huge model ID.
    name = provider.upper()

    return create_debate_segment(
        text,
        role,
        name,
        topic,
        segment_id,
        "center",
        "#3399FF",
    )


# ============================================================
# INTRO / OUTRO
# ============================================================

def intro_text(topic, judges):

    return (
        "Welcome to the AI Debate Arena. "
        f"Today's question is: {topic}. "
        "The debate will take place over three rounds "
        "with equal speaking time for both sides. "
        f"An independent panel of {judges} AI providers "
        "will judge the arguments. "
        "Let's begin."
    )


def outro_text(a, b, judges):

    if abs(a - b) < 0.05:
        winner = "a draw"
    elif a > b:
        winner = "the AI Christian Apologist"
    else:
        winner = "the AI Skeptic"

    return (
        f"After three rounds, the {judges} AI judges "
        f"gave the AI Christian Apologist {a:.1f} "
        f"and the AI Skeptic {b:.1f}. "
        f"The final result is {winner}. "
        "But the final verdict is still yours. "
        "Which side do you think actually won?"
    )


# ============================================================
# CONCATENATION
# ============================================================

def stitch(
    segments,
    output,
):

    list_file = "concat_list.txt"

    with open(
        list_file,
        "w",
        encoding="utf-8",
    ) as f:

        for segment in segments:

            path = os.path.abspath(segment)

            f.write(
                f"file '{path}'\n"
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

        print(result.stderr[-7000:])

        raise RuntimeError(
            "Final concatenation failed."
        )


# ============================================================
# MAIN
# ============================================================

def run():

    cleanup_cache()

    if not OPENROUTER_API_KEY:
        raise RuntimeError(
            "OPENROUTER_API_KEY is missing."
        )

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
        encoding="utf-8",
    ) as f:

        topic = f.read().strip()

    if not topic:
        topic = "Does the universe require a creator?"

    print("=" * 70)
    print("AI DEBATE ARENA")
    print("=" * 70)
    print("TOPIC:", topic)

    # --------------------------------------------------------
    # Models
    # --------------------------------------------------------

    models = discover_models()

    if not models:
        models = FALLBACK_MODELS.copy()

    model_a, model_b = choose_debate_models(models)

    judges = choose_judges(
        models,
        [model_a, model_b],
    )

    if not judges:

        judges = []

        used = {
            provider_from_model(model_a),
            provider_from_model(model_b),
        }

        for model in FALLBACK_MODELS:

            provider = provider_from_model(model)

            if provider in used:
                continue

            judges.append(model)
            used.add(provider)

            if len(judges) == MAX_JUDGES:
                break

    print()
    print(
        "Debate:",
        provider_from_model(model_a),
        "vs",
        provider_from_model(model_b),
    )

    print()
    print(
        "JUDGING PANEL — ONE MODEL PER PROVIDER"
    )

    for model in judges:
        print(
            " •",
            provider_from_model(model),
            "—",
            short_model_name(model),
        )

    # --------------------------------------------------------
    # Segments
    # --------------------------------------------------------

    segments = []
    segment_id = 0

    def add_debate(
        text,
        role,
        name,
        position,
        glow,
    ):

        nonlocal segment_id

        video = create_debate_segment(
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
    # Intro
    # --------------------------------------------------------

    add_debate(
        intro_text(
            topic,
            len(judges),
        ),
        "Moderator",
        "MODERATOR",
        "center",
        "#FFD700",
    )

    history = ""

    cumulative_a = 0
    cumulative_b = 0

    # --------------------------------------------------------
    # ROUNDS
    # --------------------------------------------------------

    for round_num in range(1, ROUNDS + 1):

        print()
        print("=" * 70)
        print(f"ROUND {round_num}")
        print("=" * 70)

        a_turns, b_turns, history = generate_round(
            topic,
            round_num,
            model_a,
            model_b,
            history,
        )

        # True back-and-forth.
        for i in range(
            TURNS_PER_SIDE_PER_ROUND
        ):

            a_text = a_turns[i]
            b_text = b_turns[i]

            print(
                f"Exchange {i + 1}: "
                f"A={count_words(a_text)} "
                f"B={count_words(b_text)}"
            )

            add_debate(
                a_text,
                "AI Christian Apologist",
                "AI CHRISTIAN APOLOGIST",
                "left",
                "#00FFCC",
            )

            add_debate(
                b_text,
                "AI Skeptic",
                "AI SKEPTIC",
                "right",
                "#FF00FF",
            )

        full_a = "\n".join(a_turns)
        full_b = "\n".join(b_turns)

        print(
            "Round word totals:",
            count_words(full_a),
            "vs",
            count_words(full_b),
        )

        # ----------------------------------------------------
        # Judges
        # ----------------------------------------------------

        results = judge_round(
            judges,
            topic,
            round_num,
            full_a,
            full_b,
        )

        round_a, round_b = averages(
            results
        )

        cumulative_a += round_a
        cumulative_b += round_b

        print(
            f"ROUND SCORE: {round_a:.1f} vs {round_b:.1f}"
        )

        print(
            f"CUMULATIVE: {cumulative_a:.1f} vs "
            f"{cumulative_b:.1f}"
        )

        # ----------------------------------------------------
        # SCORECARD — STATIC, SEPARATE VIDEO
        # ----------------------------------------------------

        score_image = (
            f"scorecard_r{round_num}.png"
        )

        score_audio = (
            f"score_audio_r{round_num}.mp3"
        )

        score_video = (
            f"score_video_r{round_num}.mp4"
        )

        create_scorecard(
            round_num,
            results,
            round_a,
            round_b,
            cumulative_a,
            cumulative_b,
            score_image,
        )

        score_text = (
            f"Round {round_num} is complete. "
            f"The AI Christian Apologist scored "
            f"{round_a:.1f}, while the AI Skeptic "
            f"scored {round_b:.1f}. "
            f"The cumulative score is "
            f"{cumulative_a:.1f} to "
            f"{cumulative_b:.1f}."
        )

        generate_audio(
            score_text,
            "Moderator",
            score_audio,
        )

        render_scorecard_video(
            score_image,
            score_audio,
            score_video,
        )

        if not verify_video(score_video):
            raise RuntimeError(
                f"Scorecard video failed verification: "
                f"{score_video}"
            )

        # THIS IS THE CRITICAL PART:
        # Explicitly append the scorecard.
        segments.append(score_video)

        print(
            f"✓ Scorecard r{round_num} rendered"
        )

        # ----------------------------------------------------
        # Judge commentary
        # ----------------------------------------------------

        if results:

            a_winners = [
                r for r in results
                if r["winner"] == "A"
            ]

            b_winners = [
                r for r in results
                if r["winner"] == "B"
            ]

            if not a_winners:
                a_winners = results

            if not b_winners:
                b_winners = results

            selected = [
                random.choice(a_winners),
                random.choice(b_winners),
            ]

            for n, result in enumerate(
                selected,
                1,
            ):

                preferred = (
                    "the Apologist"
                    if result["winner"] == "A"
                    else "the Skeptic"
                )

                commentary = generate_commentary(
                    result["model"],
                    preferred,
                    topic,
                    round_num,
                    full_a,
                    full_b,
                )

                print(
                    f"✓ {result['provider']} commentary"
                )

                video = create_judge_segment(
                    commentary,
                    result["provider"],
                    n,
                    topic,
                    segment_id,
                )

                segments.append(video)

                segment_id += 1

    # --------------------------------------------------------
    # Outro
    # --------------------------------------------------------

    add_debate(
        outro_text(
            cumulative_a,
            cumulative_b,
            len(judges),
        ),
        "Moderator",
        "MODERATOR",
        "center",
        "#FFD700",
    )

    # --------------------------------------------------------
    # FINAL STITCH
    # --------------------------------------------------------

    print()
    print("Checking all segments...")

    for segment in segments:

        if not verify_video(segment):

            raise RuntimeError(
                f"Invalid segment before final stitch: "
                f"{segment}"
            )

    print(
        f"✓ {len(segments)} segments verified"
    )

    stitch(
        segments,
        OUTPUT_FILE,
    )

    if not verify_video(OUTPUT_FILE):

        raise RuntimeError(
            "Final output failed video/audio verification."
        )

    print()
    print("=" * 70)
    print("✅ DEBATE COMPLETE")
    print("=" * 70)

    print(
        "Output:",
        OUTPUT_FILE,
    )

    print(
        f"Judges: {len(judges)}"
    )

    print(
        f"Final: Apologist {cumulative_a:.1f} "
        f"vs Skeptic {cumulative_b:.1f}"
    )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    try:

        run()

    except KeyboardInterrupt:

        print(
            "\n⛔ Build cancelled."
        )

    except Exception as exc:

        print(
            "\n❌ PIPELINE FAILED"
        )

        print(
            str(exc)
        )

        raise
