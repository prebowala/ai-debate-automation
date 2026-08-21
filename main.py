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
from typing import List, Dict, Tuple, Optional

import edge_tts
from PIL import Image, ImageDraw, ImageFont, ImageFilter


# ============================================================
# CONFIGURATION
# ============================================================

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"

OUTPUT_FILE = "final_debate_output.mp4"

MAX_JUDGES = 100

# Keep this relatively conservative.
# 100 simultaneous OpenRouter requests can trigger rate limits.
JUDGE_WORKERS = 12

MODEL_DISCOVERY_TIMEOUT = 20
MODEL_REQUEST_TIMEOUT = 45
JUDGE_REQUEST_TIMEOUT = 25

ROUNDS = 3

MIN_SKEPTIC_WORDS = 450
MAX_SKEPTIC_WORDS = 700

MIN_APOLOGIST_WORDS = 300
MAX_APOLOGIST_WORDS = 500

# ============================================================
# NATURAL EDGE TTS VOICES
# ============================================================

# These are deliberately conversational Microsoft neural voices.
# We don't associate a voice with the underlying AI model.
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

# Used only if dynamic discovery fails or a preferred model
# becomes unavailable.
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
# CACHE CLEANUP
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

def safe_filename(text: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", text)
    return text[:100]


def clean_for_speech(text: str) -> str:
    text = re.sub(r"\([^)]*\)", "", text)

    text = text.replace("*", "")
    text = text.replace("#", "")
    text = text.replace("_", "")
    text = text.replace("`", "")
    text = text.replace("–", "-")
    text = text.replace("—", "-")
    text = text.replace('"', "")
    text = text.replace(":", " ")
    text = text.replace(";", " ")
    text = text.replace("&", "and")

    text = re.sub(r"\s+", " ", text)

    return text.strip()


def count_words(text: str) -> int:
    return len(re.findall(r"\b[\w'-]+\b", text))


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

def load_font(size: int, bold: bool = False):

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


def discover_models() -> List[str]:

    if not OPENROUTER_API_KEY:
        print("❌ OPENROUTER_API_KEY is missing.")
        return []

    try:

        response = requests.get(
            OPENROUTER_MODELS_URL,
            headers=openrouter_headers(),
            timeout=MODEL_DISCOVERY_TIMEOUT,
        )

        if response.status_code != 200:
            print(
                f"⚠️ Model discovery failed: "
                f"{response.status_code}"
            )
            return []

        data = response.json()

        models = []

        for model in data.get("data", []):

            model_id = model.get("id")

            if not model_id:
                continue

            # Avoid models that are obviously unsuitable for
            # a text judging task.
            lowered = model_id.lower()

            excluded = [
                "embed",
                "tts",
                "whisper",
                "audio",
                "image",
                "vision",
                "guard",
                "moderation",
            ]

            if any(x in lowered for x in excluded):
                continue

            models.append(model_id)

        # Remove duplicates.
        models = list(dict.fromkeys(models))

        print(f"🔎 OpenRouter reports {len(models)} usable text models.")

        return models

    except Exception as exc:

        print(f"⚠️ Model discovery exception: {exc}")

        return []


def model_available(model_id: str, available_models: List[str]) -> bool:

    return model_id in available_models


def query_openrouter(
    prompt: str,
    model_id: str,
    timeout: int = MODEL_REQUEST_TIMEOUT,
    max_tokens: int = 1200,
    temperature: float = 0.7,
) -> Optional[str]:

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

                choices = data.get("choices", [])

                if choices:

                    content = (
                        choices[0]
                        .get("message", {})
                        .get("content", "")
                    )

                    if content and len(content.strip()) > 20:
                        return content.strip()

            else:

                # Don't print giant API error bodies.
                print(
                    f"⚠️ {model_id} returned "
                    f"HTTP {response.status_code}"
                )

        except Exception as exc:

            print(
                f"⚠️ Request failed for {model_id}: "
                f"{str(exc)[:120]}"
            )

        if attempt < 2:
            import time
            time.sleep(1.5 * (attempt + 1))

    return None


# ============================================================
# MODEL SELECTION
# ============================================================

def choose_primary_debate_models(
    available_models: List[str],
) -> Tuple[str, str]:

    preference = [
        "openai/gpt-4o",
        "openai/gpt-4o-mini",
        "anthropic/claude-3.5-sonnet",
        "anthropic/claude-3.5-haiku",
        "google/gemini-2.5-flash",
        "google/gemini-2.0-flash-001",
        "deepseek/deepseek-chat",
        "qwen/qwen-2.5-72b-instruct",
    ]

    usable = set(available_models)

    found = []

    for model in preference:

        if model in usable:
            found.append(model)

    if len(found) >= 2:
        return found[0], found[1]

    if len(found) == 1:

        remaining = [
            x for x in available_models
            if x != found[0]
        ]

        if remaining:
            return found[0], remaining[0]

    # Absolute fallback.
    if len(available_models) >= 2:
        return available_models[0], available_models[1]

    return (
        FALLBACK_MODELS[0],
        FALLBACK_MODELS[1],
    )


def choose_judges(
    available_models: List[str],
    primary_models: Tuple[str, str],
) -> List[str]:

    excluded = set(primary_models)

    candidates = [
        model
        for model in available_models
        if model not in excluded
    ]

    # Prefer reasonably capable models where possible.
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
        m for m in candidates
        if any(k in m.lower() for k in preferred_keywords)
    ]

    others = [
        m for m in candidates
        if m not in preferred
    ]

    random.shuffle(preferred)
    random.shuffle(others)

    selected = preferred + others

    selected = selected[:MAX_JUDGES]

    # If discovery returned fewer than MAX_JUDGES,
    # that's fine. The video automatically adapts.
    return list(dict.fromkeys(selected))


# ============================================================
# ARGUMENT GENERATION
# ============================================================

def generate_apologist(
    topic: str,
    round_num: int,
    previous_apologist: str,
    previous_skeptic: str,
    model: str,
) -> str:

    if round_num == 1:

        context = """
This is the opening round.
Build the strongest case for the Christian position.
Introduce genuinely substantive arguments rather than filler.
"""

    else:

        context = f"""
This is Round {round_num}.

The debate has already progressed.

Previous Apologist argument:
{previous_apologist}

Previous Skeptic response:
{previous_skeptic}

Continue naturally from that exchange.

Do NOT restart the debate.
Do NOT introduce yourself.
Do NOT explain what you are about to do.
Do NOT say "in this round".
Do NOT repeat your previous arguments word-for-word.

Instead, develop NEW arguments and directly respond to the strongest
unanswered challenge from the Skeptic.
"""

    prompt = f"""
You are participating in a serious public debate.

Topic:
{topic}

You are the AI Christian Apologist.

{context}

Write a persuasive spoken argument for a general YouTube audience.

Requirements:

- {MIN_APOLOGIST_WORDS}-{MAX_APOLOGIST_WORDS} words.
- Natural conversational speech.
- Strong reasoning.
- Concrete examples and analogies where useful.
- Address the actual argument rather than a caricature.
- Avoid academic jargon.
- Avoid unnecessary headings.
- Avoid numbered lists.
- Avoid saying you are an AI.
- Do not mention which underlying model you are.
- Sound like a skilled human debater speaking naturally.
"""

    response = query_openrouter(
        prompt,
        model,
        max_tokens=900,
        temperature=0.75,
    )

    if response:
        return response

    return (
        "The Christian case rests on whether the existence of the "
        "universe is better explained by something beyond the universe "
        "itself, rather than by the universe simply appearing without "
        "any deeper explanation."
    )


def generate_skeptic(
    topic: str,
    round_num: int,
    apologist_text: str,
    previous_skeptic: str,
    model: str,
) -> str:

    previous_instruction = ""

    if round_num > 1:

        previous_instruction = f"""
This is a continuing debate.

Your previous response was:

{previous_skeptic}

You MUST move the discussion forward.

Do not repeat your previous response.
Do not simply restate the same objection.
Identify a NEW weakness, implication, assumption, or consequence.
"""

    prompt = f"""
You are the AI Skeptic in a serious public debate.

Topic:
{topic}

Round:
{round_num}

The opposing AI Christian Apologist said:

{apologist_text}

{previous_instruction}

Your task is to give a FULL, FORCEFUL rebuttal.

THIS IS CRITICAL:

Write between {MIN_SKEPTIC_WORDS} and {MAX_SKEPTIC_WORDS} words.

You MUST produce a substantial spoken response.

Do NOT produce a short answer.

Do NOT say:
- "I cannot answer"
- "The model is unavailable"
- "I don't have enough information"
- "As an AI"
- "In conclusion" followed by a tiny answer

Do not merely say that the argument is unconvincing.

Actually attack it.

Address several distinct weaknesses.

Explain why each weakness matters.

Use simple conversational language suitable for a YouTube audience.

Use examples and analogies.

Do not use academic jargon unless absolutely necessary.

Do not introduce yourself.

Do not describe what you are going to do.

Do not say "in this round".

Do not repeat the Apologist's wording unnecessarily.

The response should sound like a confident human debate speaker
who has listened carefully and is now answering point by point.

End with a strong unresolved challenge that naturally sets up
the next stage of the debate.

Write ONLY the spoken rebuttal.
"""

    response = query_openrouter(
        prompt,
        model,
        timeout=MODEL_REQUEST_TIMEOUT,
        max_tokens=1500,
        temperature=0.8,
    )

    # --------------------------------------------------------
    # RETRY IF TOO SHORT
    # --------------------------------------------------------

    if response and count_words(response) >= MIN_SKEPTIC_WORDS:
        return response

    print(
        f"⚠️ Skeptic response from {model} was too short. "
        f"Retrying with explicit expansion."
    )

    retry_prompt = f"""
Expand the following Skeptic rebuttal into a complete,
natural spoken debate response.

Topic:
{topic}

Minimum length: {MIN_SKEPTIC_WORDS} words.

Keep the original reasoning but substantially deepen it.

Add:
1. Another independent objection.
2. A concrete analogy.
3. A direct response to the strongest part of the Apologist's case.
4. An explanation of why the disagreement matters.

Do not repeat sentences unnecessarily.

Apologist:
{apologist_text}

Existing rebuttal:
{response or "No usable rebuttal was generated."}

Return ONLY the finished spoken rebuttal.
"""

    retry = query_openrouter(
        retry_prompt,
        model,
        timeout=MODEL_REQUEST_TIMEOUT,
        max_tokens=1700,
        temperature=0.8,
    )

    if retry and count_words(retry) >= 300:
        return retry

    # --------------------------------------------------------
    # FALLBACK MODEL
    # --------------------------------------------------------

    for fallback in FALLBACK_MODELS:

        if fallback == model:
            continue

        fallback_response = query_openrouter(
            prompt,
            fallback,
            timeout=MODEL_REQUEST_TIMEOUT,
            max_tokens=1500,
            temperature=0.8,
        )

        if fallback_response and count_words(fallback_response) >= 300:
            print(
                f"✅ Skeptic fallback succeeded using {fallback}"
            )
            return fallback_response

    # --------------------------------------------------------
    # LAST RESORT
    # --------------------------------------------------------

    return (
        "The central problem with this argument is that it moves from "
        "a question about what we know to a conclusion about what must "
        "exist. Even if the universe has an explanation, that does not "
        "automatically establish that the explanation is a personal "
        "creator, and that distinction is crucial. We need to examine "
        "each step of the argument rather than treating the first "
        "possible explanation as the final answer."
    )


# ============================================================
# JUDGING
# ============================================================

def judge_round(
    judge_model: str,
    topic: str,
    round_num: int,
    apologist_text: str,
    skeptic_text: str,
) -> Dict:

    prompt = f"""
You are an independent judge evaluating a debate.

Topic:
{topic}

Round:
{round_num}

SIDE A — AI CHRISTIAN APOLOGIST:
{apologist_text}

SIDE B — AI SKEPTIC:
{skeptic_text}

Score BOTH sides independently.

Use exactly three categories:

1. ARGUMENT STRENGTH
How strong, coherent and persuasive were the arguments?

2. REBUTTAL QUALITY
How effectively did the side respond to the opposing position?

3. CLARITY AND REASONING
How clear, logically structured and understandable was the reasoning?

Each category must be scored from 0 to 100.

Then calculate each side's average.

Return ONLY valid JSON in exactly this format:

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

Do not add commentary.
"""

    response = query_openrouter(
        prompt,
        judge_model,
        timeout=JUDGE_REQUEST_TIMEOUT,
        max_tokens=250,
        temperature=0.2,
    )

    if not response:
        return make_neutral_judge_result(judge_model)

    try:

        match = re.search(
            r"\{.*\}",
            response,
            re.DOTALL,
        )

        if not match:
            return make_neutral_judge_result(judge_model)

        data = json.loads(match.group(0))

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
            "model": judge_model,
            "A_argument": a_argument,
            "A_rebuttal": a_rebuttal,
            "A_clarity": a_clarity,
            "A_total": round(a_total, 2),
            "B_argument": b_argument,
            "B_rebuttal": b_rebuttal,
            "B_clarity": b_clarity,
            "B_total": round(b_total, 2),
            "winner": "A" if a_total > b_total else "B",
        }

    except Exception:

        return make_neutral_judge_result(judge_model)


def clamp_score(value):

    try:
        value = float(value)
    except Exception:
        value = 50

    return max(0, min(100, value))


def make_neutral_judge_result(model):

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


def evaluate_round(
    judges,
    topic,
    round_num,
    apologist,
    skeptic,
):

    results = []

    print(
        f"⚖️ Round {round_num}: "
        f"asking {len(judges)} AI judges..."
    )

    def worker(model):

        result = judge_round(
            model,
            topic,
            round_num,
            apologist,
            skeptic,
        )

        return result

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=JUDGE_WORKERS
    ) as executor:

        futures = [
            executor.submit(worker, model)
            for model in judges
        ]

        for future in concurrent.futures.as_completed(futures):

            try:
                results.append(future.result())
            except Exception as exc:
                print(
                    f"⚠️ Judge failed: {str(exc)[:100]}"
                )

    if not results:
        results = [
            make_neutral_judge_result("Fallback Judge")
        ]

    return results


def calculate_round_average(results):

    a = sum(r["A_total"] for r in results) / len(results)
    b = sum(r["B_total"] for r in results) / len(results)

    return round(a, 2), round(b, 2)


# ============================================================
# TEXT TO SPEECH
# ============================================================

async def _generate_audio_and_words(
    text,
    voice,
    audio_filename,
):

    communicate = edge_tts.Communicate(
        text,
        voice,
        rate="+0%",
        volume="+0%",
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
                        chunk["offset"] +
                        chunk["duration"]
                    ) / 10_000_000,
                }
            )

    with open(audio_filename, "wb") as file:
        file.write(audio_data)

    return words


def generate_edge_audio_and_words(
    text,
    role,
    output_audio,
):

    voice = VOICES.get(
        role,
        VOICES["Moderator"],
    )

    clean_text = clean_for_speech(text)

    try:

        words = asyncio.run(
            _generate_audio_and_words(
                clean_text,
                voice,
                output_audio,
            )
        )

    except Exception as exc:

        print(
            f"⚠️ TTS failed with {voice}: "
            f"{str(exc)[:100]}"
        )

        words = asyncio.run(
            _generate_audio_and_words(
                clean_text,
                VOICES["Moderator"],
                output_audio,
            )
        )

    return words


# ============================================================
# SUBTITLE PROCESSING
# ============================================================

def ass_escape(text):

    text = text.replace("\\", "\\\\")
    text = text.replace("{", "\\{")
    text = text.replace("}", "\\}")
    text = text.replace("\n", " ")

    return text


def build_subtitle_paragraphs(
    text,
    words,
    max_words=18,
):

    if not words:
        return []

    # --------------------------------------------------------
    # Important:
    #
    # We intentionally use larger blocks rather than individual
    # sentences. This hides tiny TTS timing discrepancies.
    # --------------------------------------------------------

    paragraphs = []

    word_index = 0

    while word_index < len(words):

        chunk = words[
            word_index:
            word_index + max_words
        ]

        if not chunk:
            break

        paragraphs.append(chunk)

        word_index += max_words

    return paragraphs


def generate_paragraph_ass(
    words,
    ass_filename,
):

    header = r"""[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Subtitle,DejaVu Sans,48,&H00FFFFFF,&H00FFFF00,&H00000000,&HCC000000,0,0,0,0,100,100,0,0,1,3,1,5,180,180,300,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    paragraphs = build_subtitle_paragraphs(
        words,
        words,
        max_words=18,
    )

    lines = []

    for paragraph in paragraphs:

        if not paragraph:
            continue

        start = paragraph[0]["start"]

        end = paragraph[-1]["end"] + 0.08

        # Construct a single block.
        #
        # The current spoken word is highlighted using ASS \c.
        #
        # We create one event for each word interval, but the
        # WHOLE paragraph remains on screen.
        #

        for index, active_word in enumerate(paragraph):

            active_start = active_word["start"]

            if index + 1 < len(paragraph):
                active_end = paragraph[index + 1]["start"]
            else:
                active_end = end

            parts = []

            for w in paragraph:

                if w is active_word:

                    parts.append(
                        r"{\c&H00FFFF&}"
                        + ass_escape(w["text"])
                        + r"{\c&HFFFFFF&}"
                    )

                else:

                    parts.append(
                        ass_escape(w["text"])
                    )

            subtitle_text = " ".join(parts)

            lines.append(
                "Dialogue: 0,"
                f"{format_ass_time(active_start)},"
                f"{format_ass_time(active_end)},"
                "Subtitle,,0,0,0,,"
                + subtitle_text
            )

    with open(
        ass_filename,
        "w",
        encoding="utf-8",
    ) as file:

        file.write(
            header +
            "\n".join(lines) +
            "\n"
        )


def format_ass_time(seconds):

    hours = int(seconds // 3600)

    minutes = int(
        (seconds % 3600) // 60
    )

    secs = seconds % 60

    return (
        f"{hours}:"
        f"{minutes:02d}:"
        f"{secs:05.2f}"
    )


# ============================================================
# BACKGROUND
# ============================================================

def create_background(
    position,
    glow_color,
    output,
):

    background_path = os.path.join(
        os.path.dirname(
            os.path.abspath(__file__)
        ),
        "background.png",
    )

    if os.path.exists(background_path):

        try:

            base = (
                Image.open(background_path)
                .convert("RGB")
                .resize((1920, 1080))
            )

        except Exception:

            base = Image.new(
                "RGB",
                (1920, 1080),
                (12, 16, 32),
            )

    else:

        base = Image.new(
            "RGB",
            (1920, 1080),
            (12, 16, 32),
        )

        draw = ImageDraw.Draw(base)

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
        base.size,
        (0, 0, 0, 0),
    )

    draw = ImageDraw.Draw(overlay)

    if position == "left":
        cx = 400
    elif position == "right":
        cx = 1520
    else:
        cx = 960

    for radius in range(700, 50, -50):

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
        base.convert("RGBA"),
        overlay,
    ).convert("RGB")

    result.save(output)


# ============================================================
# UI OVERLAY
# ============================================================

def create_ui_overlay(
    speaker_name,
    role_label,
    topic,
    position,
    glow_color,
    output,
):

    image = Image.new(
        "RGBA",
        (1920, 1080),
        (0, 0, 0, 0),
    )

    draw = ImageDraw.Draw(image)

    title_font = load_font(34, bold=True)
    name_font = load_font(30, bold=True)
    role_font = load_font(21, bold=True)

    # --------------------------------------------------------
    # SMALLER TOPIC
    # --------------------------------------------------------

    title = f"TOPIC: {topic}"

    bbox = draw.textbbox(
        (0, 0),
        title,
        font=title_font,
    )

    title_width = bbox[2] - bbox[0]

    draw.text(
        (
            (1920 - title_width) // 2,
            28,
        ),
        title,
        fill="white",
        font=title_font,
    )

    # --------------------------------------------------------
    # SPEAKER CARD
    #
    # It deliberately sits LOW enough to avoid the subtitle
    # area, but high enough to remain visible.
    # --------------------------------------------------------

    card_width = 620
    card_height = 105
    card_y = 900

    if position == "left":

        card_x = 90

    elif position == "right":

        card_x = 1210

    else:

        card_x = (1920 - card_width) // 2

    draw.rounded_rectangle(
        [
            card_x,
            card_y,
            card_x + card_width,
            card_y + card_height,
        ],
        radius=16,
        fill=(18, 26, 46, 235),
        outline=glow_color,
        width=3,
    )

    # Speaker indicator.
    draw.ellipse(
        [
            card_x + 24,
            card_y + 39,
            card_x + 44,
            card_y + 59,
        ],
        fill=glow_color,
    )

    # Name and role deliberately separated.
    draw.text(
        (
            card_x + 65,
            card_y + 19,
        ),
        speaker_name,
        fill="white",
        font=name_font,
    )

    draw.text(
        (
            card_x + 65,
            card_y + 61,
        ),
        role_label.upper(),
        fill=glow_color,
        font=role_font,
    )

    image.save(output)

    return card_x


# ============================================================
# SCOREBOARD
# ============================================================

def generate_round_breakdown_image(
    round_num,
    results,
    round_a,
    round_b,
    cumulative_a,
    cumulative_b,
    total_judges,
    output,
):

    background_path = os.path.join(
        os.path.dirname(
            os.path.abspath(__file__)
        ),
        "background.png",
    )

    if os.path.exists(background_path):

        try:

            image = (
                Image.open(background_path)
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

    dark = Image.new(
        "RGBA",
        (1920, 1080),
        (0, 0, 0, 210),
    )

    image = Image.alpha_composite(
        image.convert("RGBA"),
        dark,
    ).convert("RGB")

    draw = ImageDraw.Draw(image)

    header_font = load_font(38, bold=True)
    sub_font = load_font(24, bold=True)
    small_font = load_font(19)

    def centered(y, text, font, fill):

        bbox = draw.textbbox(
            (0, 0),
            text,
            font=font,
        )

        width = bbox[2] - bbox[0]

        draw.text(
            (
                (1920 - width) // 2,
                y,
            ),
            text,
            fill=fill,
            font=font,
        )

    centered(
        25,
        f"ROUND {round_num} — AI PANEL SCORECARD",
        header_font,
        "#FFD700",
    )

    centered(
        80,
        f"{total_judges} AI JUDGES • EACH CATEGORY SCORED 0–100",
        sub_font,
        "white",
    )

    centered(
        125,
        (
            f"ROUND AVERAGE   "
            f"APOLOGIST {round_a:.1f}   "
            f"vs   SKEPTIC {round_b:.1f}"
        ),
        sub_font,
        "white",
    )

    centered(
        170,
        (
            f"CUMULATIVE   "
            f"APOLOGIST {cumulative_a:.1f}   "
            f"vs   SKEPTIC {cumulative_b:.1f}"
        ),
        sub_font,
        "#FFD700",
    )

    # --------------------------------------------------------
    # THREE CATEGORY AVERAGES
    # --------------------------------------------------------

    avg_a_argument = sum(
        r["A_argument"] for r in results
    ) / len(results)

    avg_a_rebuttal = sum(
        r["A_rebuttal"] for r in results
    ) / len(results)

    avg_a_clarity = sum(
        r["A_clarity"] for r in results
    ) / len(results)

    avg_b_argument = sum(
        r["B_argument"] for r in results
    ) / len(results)

    avg_b_rebuttal = sum(
        r["B_rebuttal"] for r in results
    ) / len(results)

    avg_b_clarity = sum(
        r["B_clarity"] for r in results
    ) / len(results)

    draw.text(
        (160, 220),
        "CATEGORY AVERAGES",
        sub_font,
        fill="#FFD700",
    )

    categories = [
        (
            "Argument strength",
            avg_a_argument,
            avg_b_argument,
        ),
        (
            "Rebuttal quality",
            avg_a_rebuttal,
            avg_b_rebuttal,
        ),
        (
            "Clarity & reasoning",
            avg_a_clarity,
            avg_b_clarity,
        ),
    ]

    y = 260

    for name, a, b in categories:

        draw.text(
            (160, y),
            f"{name}:",
            small_font,
            fill="white",
        )

        draw.text(
            (520, y),
            f"{a:.1f}",
            small_font,
            fill="#00FFCC",
        )

        draw.text(
            (700, y),
            f"{b:.1f}",
            small_font,
            fill="#FF66FF",
        )

        y += 30

    # --------------------------------------------------------
    # INDIVIDUAL JUDGE SCORES
    # --------------------------------------------------------

    draw.text(
        (1050, 220),
        "INDIVIDUAL JUDGE RESULTS",
        sub_font,
        fill="#FFD700",
    )

    draw.text(
        (1050, 255),
        "Judge",
        small_font,
        fill="white",
    )

    draw.text(
        (1470, 255),
        "A",
        small_font,
        fill="#00FFCC",
    )

    draw.text(
        (1530, 255),
        "B",
        small_font,
        fill="#FF66FF",
    )

    # With up to 100 judges, a single screen cannot display
    # every model name legibly.
    #
    # Instead display all results in compact rows and let
    # the actual count dynamically determine the layout.
    #
    # The scores shown here are the final three-category
    # average for every judge.

    start_y = 285
    row_height = 25

    max_visible = 29

    for i, result in enumerate(results[:max_visible]):

        name = result["model"]

        if len(name) > 37:
            name = name[:34] + "..."

        y = start_y + i * row_height

        draw.text(
            (1050, y),
            name,
            small_font,
            fill="white",
        )

        draw.text(
            (1470, y),
            f"{result['A_total']:.0f}",
            small_font,
            fill="#00FFCC",
        )

        draw.text(
            (1530, y),
            f"{result['B_total']:.0f}",
            small_font,
            fill="#FF66FF",
        )

    if len(results) > max_visible:

        draw.text(
            (
                1050,
                start_y +
                max_visible * row_height +
                5,
            ),
            (
                f"+ {len(results) - max_visible} "
                f"additional judges included in the average"
            ),
            small_font,
            fill="#FFD700",
        )

    image.save(output)


# ============================================================
# VIDEO RENDERING
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

    # --------------------------------------------------------
    # IMPORTANT:
    #
    # This is deliberately constructed as normal Python strings.
    # There is no accidental quoted "[1:v]" line.
    # --------------------------------------------------------

    wave_x = card_x + 390
    wave_y = 915

    wave_x = max(
        0,
        min(
            wave_x,
            1740,
        ),
    )

    wave_y = 915

    # Simple centre/right/left animated background movement.
    if position == "left":

        pan_x = "0"

    elif position == "right":

        pan_x = "iw-(iw/zoom)"

    else:

        pan_x = "(iw-(iw/zoom))/2"

    pan_y = "(ih-(ih/zoom))/2"

    ass_path = os.path.abspath(ass)
    ass_path = ass_path.replace("\\", "/")

    # Escape colon for Windows drive letters.
    ass_path_ffmpeg = ass_path.replace(":", "\\:")

    glow = glow_color.lstrip("#")

    filter_complex = (
        "[0:v]"
        "scale=1920:1080,"
        "zoompan="
        "z='min(zoom+0.00035,1.10)':"
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
        "s=220x45:"
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
        "[combined];"

        "[combined]"
        f"ass='{ass_path_ffmpeg}'"
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
            "❌ FFmpeg segment failed:"
        )

        print(
            result.stderr[-3000:]
        )

        raise RuntimeError(
            f"FFmpeg failed creating {output}"
        )


# ============================================================
# VIDEO SEGMENT CREATOR
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

    audio_file = f"audio_{segment_id}.mp3"
    ass_file = f"subs_{segment_id}.ass"
    bg_file = f"bg_{segment_id}.png"
    ui_file = f"ui_{segment_id}.png"
    video_file = f"segment_{segment_id}.mp4"

    words = generate_edge_audio_and_words(
        text,
        role,
        audio_file,
    )

    generate_paragraph_ass(
        words,
        ass_file,
    )

    create_background(
        position,
        glow,
        bg_file,
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
        bg_file,
        ui_file,
        audio_file,
        ass_file,
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
    used_comments,
):

    role = (
        "logic-focused evaluator"
        if side == "A"
        else
        "real-world reasoning evaluator"
    )

    previous = "\n".join(
        used_comments[-6:]
    )

    prompt = f"""
You are an independent AI panel judge.

Topic:
{topic}

Round:
{round_num}

You are acting as a {role}.

The side you preferred was:
{"AI Christian Apologist" if side == "A" else "AI Skeptic"}

Do NOT summarise the debate.

Do NOT repeat either debater's arguments.

Do NOT quote them.

Do NOT say the same thing another panelist has already said.

Here are recent panel comments that MUST NOT be repeated:

{previous}

Give ONE short, natural observation of 2-3 sentences.

It should add a genuinely new insight about the quality of reasoning.

Do not mention your model name.

Do not say "as an AI".

Return only the spoken commentary.
"""

    response = query_openrouter(
        prompt,
        model,
        timeout=30,
        max_tokens=180,
        temperature=0.9,
    )

    if not response:
        return (
            "The stronger performance here came from the side "
            "that left fewer important assumptions unexplained."
        )

    return response


# ============================================================
# INTRO / OUTRO
# ============================================================

def build_intro(topic, judge_count):

    return (
        "Welcome to the AI Debate Arena. "
        f"Today, an AI Christian Apologist faces an AI Skeptic "
        f"on one of humanity's biggest questions. "
        f"After three rounds, an independent panel of "
        f"{judge_count} available AI judges will score the debate. "
        "The judges will evaluate argument strength, rebuttal quality, "
        "and clarity of reasoning. "
        "Let's begin."
    )


def build_outro(
    topic,
    judge_count,
    cumulative_a,
    cumulative_b,
):

    winner = (
        "AI Christian Apologist"
        if cumulative_a > cumulative_b
        else "AI Skeptic"
    )

    if math.isclose(
        cumulative_a,
        cumulative_b,
        abs_tol=0.01,
    ):
        winner = "a draw"

    return (
        f"After three rounds, our panel of {judge_count} AI judges "
        f"gave the AI Christian Apologist a cumulative score of "
        f"{cumulative_a:.1f}, compared with {cumulative_b:.1f} "
        f"for the AI Skeptic. "
        f"The final result is {winner}. "
        "But the final verdict is still yours. "
        "Who do you think actually won?"
    )


# ============================================================
# CONCAT
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

            absolute = os.path.abspath(
                segment
            )

            # FFmpeg concat requires escaping.
            absolute = absolute.replace(
                "'",
                "'\\''",
            )

            file.write(
                f"file '{absolute}'\n"
            )

    print("🎬 Stitching final video...")

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

        print(result.stderr[-3000:])

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
            "OPENROUTER_API_KEY environment variable is missing."
        )

    # --------------------------------------------------------
    # TOPIC
    # --------------------------------------------------------

    if not os.path.exists("topic.txt"):

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
    # DISCOVER MODELS
    # --------------------------------------------------------

    available_models = discover_models()

    if not available_models:

        print(
            "⚠️ Dynamic discovery failed. "
            "Using fallback model list."
        )

        available_models = FALLBACK_MODELS.copy()

    apologist_model, skeptic_model = (
        choose_primary_debate_models(
            available_models
        )
    )

    judges = choose_judges(
        available_models,
        (
            apologist_model,
            skeptic_model,
        ),
    )

    print()
    print(
        f"🎤 Debate engine models selected."
    )

    print(
        f"⚖️ Dynamic judging panel: "
        f"{len(judges)} models"
    )

    # --------------------------------------------------------
    # NEVER ADVERTISE UNDERLYING MODELS
    # --------------------------------------------------------

    print(
        "📺 Public debate identities:"
    )

    print(
        "   AI Christian Apologist"
    )

    print(
        "   AI Skeptic"
    )

    # --------------------------------------------------------
    # SEGMENTS
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

        filename = create_segment(
            text,
            role,
            name,
            topic,
            segment_id,
            position,
            glow,
        )

        segments.append(filename)

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
    # DEBATE
    # --------------------------------------------------------

    cumulative_a = 0.0
    cumulative_b = 0.0

    previous_apologist = ""
    previous_skeptic = ""

    panel_comments = []

    for round_num in range(1, ROUNDS + 1):

        print()
        print(
            "=" * 70
        )
        print(
            f"ROUND {round_num}"
        )
        print(
            "=" * 70
        )

        # ----------------------------------------------------
        # APologist
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

        scoreboard = (
            f"scoreboard_r{round_num}.png"
        )

        generate_round_breakdown_image(
            round_num,
            results,
            round_a,
            round_b,
            cumulative_a,
            cumulative_b,
            len(results),
            scoreboard,
        )

        score_audio = (
            f"score_audio_r{round_num}.mp3"
        )

        score_ass = (
            f"score_subs_r{round_num}.ass"
        )

        score_video = (
            f"score_video_r{round_num}.mp4"
        )

        score_text = (
            f"Round {round_num} is complete. "
            f"The {len(results)} AI judges gave the "
            f"AI Christian Apologist an average of "
            f"{round_a:.1f}, and the AI Skeptic an average of "
            f"{round_b:.1f}. "
            f"The cumulative score is "
            f"{cumulative_a:.1f} to {cumulative_b:.1f}."
        )

        words = generate_edge_audio_and_words(
            score_text,
            "Moderator",
            score_audio,
        )

        generate_paragraph_ass(
            words,
            score_ass,
        )

        score_ui = (
            f"score_ui_r{round_num}.png"
        )

        card_x = create_ui_overlay(
            "Moderator",
            "Scoreboard",
            topic,
            "center",
            "#FFD700",
            score_ui,
        )

        render_video_segment(
            scoreboard,
            score_ui,
            score_audio,
            score_ass,
            score_video,
            "center",
            "#FFD700",
            card_x,
        )

        segments.append(
            score_video
        )

        # ----------------------------------------------------
        # PANEL COMMENTARY
        #
        # Select two actual judges from this round.
        # ----------------------------------------------------

        if results:

            a_judges = [
                r for r in results
                if r["winner"] == "A"
            ]

            b_judges = [
                r for r in results
                if r["winner"] == "B"
            ]

            if not a_judges:
                a_judges = results

            if not b_judges:
                b_judges = results

            judge_a = random.choice(
                a_judges
            )

            judge_b = random.choice(
                b_judges
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
            topic,
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
            "The arguments have been presented and the panel "
            "has delivered its verdict. "
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
    # FINAL CLEANUP
    # --------------------------------------------------------

    print()
    print("=" * 70)
    print("✅ DEBATE COMPLETE")
    print("=" * 70)

    print(
        f"🎥 Output: {OUTPUT_FILE}"
    )

    print(
        f"⚖️ AI judges used: {len(judges)}"
    )

    print(
        f"🏆 Final score:"
        f" Apologist {cumulative_a:.1f}"
        f" vs Skeptic {cumulative_b:.1f}"
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

        print()
        print(
            "❌ PIPELINE FAILED"
        )
        print(
            str(exc)
        )
        print()

        raise
