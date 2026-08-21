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
# AI DEBATE ARENA
# ============================================================
#
# STRUCTURE
#
# 3 rounds
#
# Each side gets approximately 500 words TOTAL per round.
#
# Each round:
#
#   Apologist 1  ~165 words
#   Skeptic 1    ~165 words
#   Apologist 2  ~165 words
#   Skeptic 2    ~165 words
#   Apologist 3  ~170 words
#   Skeptic 3    ~170 words
#
# This produces genuine back-and-forth debate rather than
# one long speech followed by another.
#
# ============================================================


# ============================================================
# CONFIGURATION
# ============================================================

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")

OPENROUTER_URL = (
    "https://openrouter.ai/api/v1/chat/completions"
)

OPENROUTER_MODELS_URL = (
    "https://openrouter.ai/api/v1/models"
)

OUTPUT_FILE = "final_debate_output.mp4"

# ============================================================
# JUDGES
# ============================================================

MAX_JUDGES = 30

JUDGE_WORKERS = 10

# ============================================================
# DEBATE
# ============================================================

ROUNDS = 3

WORDS_PER_SIDE_PER_ROUND = 500

TURNS_PER_SIDE_PER_ROUND = 3

TURN_WORD_TARGET = 167

TURN_WORD_MIN = 145

TURN_WORD_MAX = 185

# ============================================================
# MODEL TIMEOUTS
# ============================================================

MODEL_DISCOVERY_TIMEOUT = 20

MODEL_REQUEST_TIMEOUT = 60

JUDGE_REQUEST_TIMEOUT = 35

# ============================================================
# VIDEO
# ============================================================

VIDEO_WIDTH = 1920

VIDEO_HEIGHT = 1080

FPS = 30


# ============================================================
# NATURAL TTS VOICES
# ============================================================

VOICES = {

    "Moderator":
        "en-US-AndrewMultilingualNeural",

    "AI Christian Apologist":
        "en-US-BrianMultilingualNeural",

    "AI Skeptic":
        "en-US-AvaMultilingualNeural",

    "Panelist 1":
        "en-US-ChristopherNeural",

    "Panelist 2":
        "en-US-EmmaMultilingualNeural",
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
# COMPANY / PROVIDER DETECTION
# ============================================================

def get_provider(model_id):

    lowered = model_id.lower()

    if lowered.startswith("openai/"):
        return "OpenAI"

    if lowered.startswith("anthropic/"):
        return "Anthropic"

    if lowered.startswith("google/"):
        return "Google"

    if lowered.startswith("mistralai/"):
        return "Mistral"

    if lowered.startswith("meta-llama/"):
        return "Meta"

    if lowered.startswith("qwen/"):
        return "Qwen"

    if lowered.startswith("deepseek/"):
        return "DeepSeek"

    if lowered.startswith("x-ai/"):
        return "xAI"

    if lowered.startswith("xai/"):
        return "xAI"

    if lowered.startswith("cohere/"):
        return "Cohere"

    if lowered.startswith("perplexity/"):
        return "Perplexity"

    if lowered.startswith("nvidia/"):
        return "NVIDIA"

    if lowered.startswith("amazon/"):
        return "Amazon"

    if lowered.startswith("microsoft/"):
        return "Microsoft"

    if lowered.startswith("01-ai/"):
        return "01.AI"

    if lowered.startswith("together/"):
        return "Together"

    if "/" in model_id:
        return model_id.split("/")[0].title()

    return "Other"


def short_model_name(model_id):

    provider = get_provider(model_id)

    if "/" in model_id:
        name = model_id.split("/", 1)[1]
    else:
        name = model_id

    name = re.sub(
        r":.*$",
        "",
        name,
    )

    replacements = {

        "instruct": "",
        "chat": "",
        "latest": "",
        "preview": "",
    }

    for old, new in replacements.items():

        name = name.replace(
            old,
            new,
        )

    name = re.sub(
        r"[-_]+",
        " ",
        name,
    )

    name = re.sub(
        r"\s+",
        " ",
        name,
    ).strip()

    if len(name) > 25:

        name = name[:22] + "..."

    return f"{provider} {name}"


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
        min(100.0, value),
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


def hex_to_rgba(
    hex_str,
    alpha,
):

    hex_str = hex_str.lstrip("#")

    return (

        int(hex_str[0:2], 16),
        int(hex_str[2:4], 16),
        int(hex_str[4:6], 16),
        alpha,

    )


# ============================================================
# FONT
# ============================================================

def load_font(
    size,
    bold=False,
):

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
# OPENROUTER HEADERS
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


# ============================================================
# MODEL DISCOVERY
# ============================================================

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
                "⚠️ Model discovery returned "
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

        for item in data.get(
            "data",
            [],
        ):

            model_id = item.get("id")

            if not model_id:
                continue

            lowered = model_id.lower()

            if any(
                x in lowered
                for x in excluded
            ):
                continue

            models.append(
                model_id
            )

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
            "⚠️ Model discovery failed: "
            f"{str(exc)[:200]}"
        )

        return []


# ============================================================
# OPENROUTER REQUEST
# ============================================================

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
                        and len(
                            content.strip()
                        ) > 20
                    ):

                        return content.strip()

            else:

                # Do not retry obvious unavailable/
                # incompatible model errors repeatedly.
                if response.status_code in (
                    400,
                    404,
                    410,
                ):

                    print(
                        f"⚠️ {model_id} "
                        f"unavailable "
                        f"(HTTP "
                        f"{response.status_code})"
                    )

                    return None

                print(
                    f"⚠️ {model_id} returned "
                    f"HTTP {response.status_code}"
                )

        except Exception as exc:

            print(
                f"⚠️ Request failed for "
                f"{model_id}: "
                f"{str(exc)[:100]}"
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


# ============================================================
# JUDGE SELECTION
# ONE MODEL PER COMPANY
# ============================================================

def choose_judges(
    available_models,
    primary_models,
):

    excluded = set(
        primary_models
    )

    candidates = [

        m
        for m in available_models
        if m not in excluded

    ]

    # Rank sensible judging models first.
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

    random.shuffle(
        preferred
    )

    random.shuffle(
        others
    )

    ordered = (
        preferred +
        others
    )

    selected = []

    providers_used = set()

    for model in ordered:

        provider = get_provider(
            model
        )

        if provider in providers_used:
            continue

        providers_used.add(
            provider
        )

        selected.append(
            model
        )

        if len(selected) >= MAX_JUDGES:
            break

    return selected


# ============================================================
# DEBATE TURN PROMPT
# ============================================================

def generate_debate_turn(
    side,
    topic,
    round_num,
    turn_num,
    current_round_history,
    previous_round_summary,
    model,
):

    if side == "A":

        role = "AI Christian Apologist"

        opponent = "AI Skeptic"

    else:

        role = "AI Skeptic"

        opponent = "AI Christian Apologist"

    history_text = "\n\n".join(
        current_round_history
    )

    if not history_text:

        history_text = (
            "No previous contribution has been made "
            "in this round."
        )

    if previous_round_summary:

        previous_context = f"""
The previous round ended with:

{previous_round_summary}

Do not simply repeat those arguments.
"""

    else:

        previous_context = ""

    if turn_num == 1:

        turn_instruction = """
This is the opening contribution of this round.

Make a substantive point that establishes the direction
of the exchange.
"""

    elif turn_num == 2:

        turn_instruction = """
This is the middle of the exchange.

Directly answer the opponent's most important point
from the immediately preceding contribution.

Then introduce a further argument.
"""

    else:

        turn_instruction = """
This is the final contribution of this round.

Directly answer the opponent's latest point.

Develop the strongest remaining argument.

End with a concise point that naturally leaves the
debate ready for the next round.
"""

    prompt = f"""
You are the {role} in a serious public debate.

Topic:

{topic}

Round {round_num} of {ROUNDS}.

This is contribution {turn_num} of {TURNS_PER_SIDE_PER_ROUND}
for your side.

Your opponent is the {opponent}.

{previous_context}

Previous contributions in THIS round:

{history_text}

{turn_instruction}

IMPORTANT FAIRNESS RULE:

Your side has approximately
{WORDS_PER_SIDE_PER_ROUND} words TOTAL in this round.

This contribution should therefore be approximately
{TURN_WORD_TARGET} words.

Acceptable range:
{TURN_WORD_MIN}-{TURN_WORD_MAX} words.

Do NOT write 500 words in this contribution.

Do NOT restart the debate.

Do NOT introduce yourself.

Do NOT explain your role.

Do NOT say "in this round".

Do NOT describe what you are going to do.

Do NOT repeat arguments already made.

Directly engage with what the other side has actually said.

Use natural conversational language suitable for YouTube.

Use concrete examples or analogies where helpful.

Avoid academic jargon.

No headings.

No numbered lists.

Do not mention your underlying AI model.

Do not mention your company.

Write ONLY the spoken contribution.
"""

    response = query_openrouter(

        prompt,

        model,

        timeout=MODEL_REQUEST_TIMEOUT,

        max_tokens=650,

        temperature=0.8,

    )

    if response:

        words = count_words(
            response
        )

        if (
            TURN_WORD_MIN
            <= words
            <= 230
        ):

            return response

    # Retry specifically for length.
    retry_prompt = f"""
Rewrite this debate contribution.

Keep the argument but make it approximately
{TURN_WORD_TARGET} words.

Target range:
{TURN_WORD_MIN}-{TURN_WORD_MAX} words.

It must directly respond to the preceding contribution.

Do not restart the debate.

Do not mention AI models.

Do not mention that this was rewritten.

Return only the spoken contribution.

Topic:
{topic}

Contribution:
{response or "No usable contribution was generated."}
"""

    retry = query_openrouter(

        retry_prompt,

        model,

        timeout=MODEL_REQUEST_TIMEOUT,

        max_tokens=650,

        temperature=0.75,

    )

    if retry:

        return retry

    # Safe fallback.
    if side == "A":

        return (
            "The important point is that this argument cannot "
            "simply be dismissed because there are alternative "
            "possibilities. We need to ask which explanation "
            "actually makes better sense of the evidence. "
            "If the skeptic rejects this conclusion, the burden "
            "is not merely to point to uncertainty, but to explain "
            "why the alternative is more convincing."
        )

    return (
        "The problem is that the conclusion goes further than "
        "the evidence allows. Even if the argument establishes "
        "that there is something we do not yet understand, that "
        "does not automatically establish the particular answer "
        "being proposed. We need to separate what the evidence "
        "actually shows from what we would like it to show."
    )


# ============================================================
# ROUND SUMMARY
# ============================================================

def generate_round_summary(
    topic,
    round_history,
    model,
):

    history = "\n".join(
        round_history
    )

    prompt = f"""
Give a concise internal summary of the arguments made in this
debate round.

Topic:
{topic}

Arguments:
{history}

Identify:

- strongest Apologist point
- strongest Skeptic point
- unresolved disagreement

This summary is for the next round's reasoning only.

Do not address viewers.

Do not mention AI models.

Maximum 150 words.
"""

    response = query_openrouter(

        prompt,

        model,

        timeout=40,

        max_tokens=220,

        temperature=0.3,

    )

    return response or ""


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
    debate_text,
):

    prompt = f"""
You are an independent AI judge evaluating a debate.

Topic:

{topic}

Round:

{round_num}

Evaluate these two sides:

AI CHRISTIAN APOLOGIST:
{debate_text["A"]}

AI SKEPTIC:
{debate_text["B"]}

Score both sides independently.

Use exactly three categories:

1. ARGUMENT STRENGTH
2. REBUTTAL QUALITY
3. CLARITY AND REASONING

Each category must be scored from 0 to 100.

Calculate each side's average across the three categories.

Do not favour either side because of the conclusion.

Judge the quality of reasoning and response.

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

        timeout=JUDGE_REQUEST_TIMEOUT,

        max_tokens=250,

        temperature=0.1,

    )

    if not response:

        return neutral_judge(
            model
        )

    try:

        match = re.search(
            r"\{.*\}",
            response,
            re.DOTALL,
        )

        if not match:

            return neutral_judge(
                model
            )

        data = json.loads(
            match.group(0)
        )

        aa = clamp_score(
            data.get(
                "A_argument",
                50,
            )
        )

        ar = clamp_score(
            data.get(
                "A_rebuttal",
                50,
            )
        )

        ac = clamp_score(
            data.get(
                "A_clarity",
                50,
            )
        )

        ba = clamp_score(
            data.get(
                "B_argument",
                50,
            )
        )

        br = clamp_score(
            data.get(
                "B_rebuttal",
                50,
            )
        )

        bc = clamp_score(
            data.get(
                "B_clarity",
                50,
            )
        )

        a_total = (
            aa + ar + ac
        ) / 3

        b_total = (
            ba + br + bc
        ) / 3

        return {

            "model": model,

            "A_argument": aa,
            "A_rebuttal": ar,
            "A_clarity": ac,
            "A_total": round(
                a_total,
                2,
            ),

            "B_argument": ba,
            "B_rebuttal": br,
            "B_clarity": bc,
            "B_total": round(
                b_total,
                2,
            ),

            "winner": (
                "A"
                if a_total >= b_total
                else "B"
            ),

        }

    except Exception:

        return neutral_judge(
            model
        )


def evaluate_round(
    judges,
    topic,
    round_num,
    debate_text,
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

            debate_text,

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

                results.append(
                    result
                )

                completed += 1

                print(
                    f"   ✓ Judge "
                    f"{completed}/"
                    f"{len(judges)}"
                )

            except Exception as exc:

                print(
                    f"   ✗ Judge failed: "
                    f"{str(exc)[:100]}"
                )

    if not results:

        results = [
            neutral_judge(
                "Fallback Judge"
            )
        ]

    return results


def calculate_round_average(
    results
):

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

            words.append({

                "text":
                    chunk["text"],

                "start":
                    chunk["offset"]
                    / 10_000_000,

                "duration":
                    chunk["duration"]
                    / 10_000_000,

                "end":
                    (
                        chunk["offset"]
                        +
                        chunk["duration"]
                    )
                    / 10_000_000,

            })

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

    clean_text = clean_for_speech(
        text
    )

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
            f"⚠️ TTS failed: "
            f"{str(exc)[:120]}"
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

def format_ass_time(
    seconds
):

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


def ass_escape(
    text
):

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

    # Larger paragraph blocks reduce visual distraction.
    # Word highlighting is still driven by the exact Edge-TTS
    # WordBoundary timestamps.

    header = r"""[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: DebateSub,DejaVu Sans,43,&H00FFFFFF,&H00FFFF00,&H00000000,&HCC000000,1,0,0,0,100,100,0,0,1,3,1,2,220,220,155,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    if not words:

        with open(
            filename,
            "w",
            encoding="utf-8",
        ) as file:

            file.write(
                header
            )

        return

    # 18 words keeps a paragraph on screen long enough
    # to hide tiny timing variations while remaining readable.
    chunk_size = 18

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
            chunks.append(
                chunk
            )

    events = []

    for chunk in chunks:

        paragraph_end = (
            chunk[-1]["end"]
            + 0.12
        )

        for index, active in enumerate(
            chunk
        ):

            start = active[
                "start"
            ]

            if index + 1 < len(
                chunk
            ):

                end = chunk[
                    index + 1
                ]["start"]

            else:

                end = paragraph_end

            rendered = []

            for word in chunk:

                word_text = ass_escape(
                    word["text"]
                )

                if word is active:

                    rendered.append(

                        r"{\c&H00FFFF&}"
                        +
                        word_text
                        +
                        r"{\c&HFFFFFF&}"

                    )

                else:

                    rendered.append(
                        word_text
                    )

            subtitle = " ".join(
                rendered
            )

            events.append(

                "Dialogue: 0,"
                +
                format_ass_time(
                    start
                )
                +
                ","
                +
                format_ass_time(
                    end
                )
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

    source = os.path.join(
        os.path.dirname(
            os.path.abspath(
                __file__
            )
        ),
        "background.png",
    )

    if os.path.exists(source):

        try:

            image = (

                Image.open(
                    source
                )
                .convert("RGB")
                .resize(
                    (
                        VIDEO_WIDTH,
                        VIDEO_HEIGHT,
                    )
                )

            )

        except Exception:

            image = Image.new(
                "RGB",
                (
                    VIDEO_WIDTH,
                    VIDEO_HEIGHT,
                ),
                (12, 16, 32),
            )

    else:

        image = Image.new(

            "RGB",

            (
                VIDEO_WIDTH,
                VIDEO_HEIGHT,
            ),

            (12, 16, 32),

        )

        draw = ImageDraw.Draw(
            image
        )

        for x in range(
            0,
            VIDEO_WIDTH,
            60,
        ):

            draw.line(

                [
                    (x, 0),
                    (
                        x,
                        VIDEO_HEIGHT,
                    ),
                ],

                fill=(20, 26, 45),

                width=2,

            )

        for y in range(
            0,
            VIDEO_HEIGHT,
            60,
        ):

            draw.line(

                [
                    (0, y),
                    (
                        VIDEO_WIDTH,
                        y,
                    ),
                ],

                fill=(20, 26, 45),

                width=2,

            )

    overlay = Image.new(

        "RGBA",

        (
            VIDEO_WIDTH,
            VIDEO_HEIGHT,
        ),

        (0, 0, 0, 0),

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
            15
            *
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
        ImageFilter.GaussianBlur(
            30
        )
    )

    result = Image.alpha_composite(

        image.convert("RGBA"),

        overlay,

    ).convert("RGB")

    result.save(
        filename
    )


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
):

    image = Image.new(

        "RGBA",

        (
            VIDEO_WIDTH,
            VIDEO_HEIGHT,
        ),

        (0, 0, 0, 0),

    )

    draw = ImageDraw.Draw(
        image
    )

    title_font = load_font(
        28,
        bold=True,
    )

    name_font = load_font(
        27,
        bold=True,
    )

    role_font = load_font(
        18,
        bold=True,
    )

    # Topic

    title = (
        f"TOPIC: {topic}"
    )

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
            (1920 - width) // 2,
            22,
        ),

        title,

        font=title_font,

        fill="white",

    )

    # --------------------------------------------------------
    # CARD
    #
    # Raised slightly so it doesn't collide with subtitles.
    # Sound wave is placed BELOW the text inside the card.
    # --------------------------------------------------------

    card_width = 590

    card_height = 108

    card_y = 850

    if position == "left":

        card_x = 70

    elif position == "right":

        card_x = 1260

    else:

        card_x = (
            1920
            -
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

        width=3,

    )

    # Indicator

    draw.ellipse(

        [
            card_x + 22,
            card_y + 20,
            card_x + 42,
            card_y + 40,
        ],

        fill=glow_color,

    )

    # Name

    draw.text(

        (
            card_x + 58,
            card_y + 13,
        ),

        speaker_name,

        font=name_font,

        fill="white",

    )

    # Role

    draw.text(

        (
            card_x + 58,
            card_y + 49,
        ),

        role_label.upper(),

        font=role_font,

        fill=glow_color,

    )

    image.save(
        filename
    )

    return card_x


# ============================================================
# SIMPLE VISUAL PROMPTS / CARDS
# ============================================================

def detect_visual_keyword(
    text
):

    lowered = (
        text.lower()
    )

    visuals = [

        (
            [
                "adam",
                "garden",
                "eden",
                "apple",
            ],
            "GARDEN OF EDEN",
        ),

        (
            [
                "evolution",
                "human evolution",
                "ape",
            ],
            "EVOLUTION",
        ),

        (
            [
                "universe",
                "cosmos",
                "galaxy",
                "big bang",
            ],
            "THE UNIVERSE",
        ),

        (
            [
                "star",
                "stars",
                "planet",
                "earth",
            ],
            "COSMIC SCALE",
        ),

        (
            [
                "miracle",
                "miracles",
            ],
            "MIRACLE",
        ),

        (
            [
                "resurrection",
                "resurrected",
            ],
            "RESURRECTION",
        ),

        (
            [
                "bible",
                "scripture",
            ],
            "SCRIPTURE",
        ),

    ]

    for keywords, label in visuals:

        if any(
            key in lowered
            for key in keywords
        ):

            return label

    return None


def create_visual_overlay(
    text,
    filename,
):

    image = Image.new(

        "RGBA",

        (
            VIDEO_WIDTH,
            VIDEO_HEIGHT,
        ),

        (0, 0, 0, 0),

    )

    visual = detect_visual_keyword(
        text
    )

    if not visual:

        image.save(
            filename
        )

        return

    draw = ImageDraw.Draw(
        image
    )

    # Simple non-intrusive cinematic visual.
    # This deliberately avoids requiring an external image API.
    # It can later be replaced by generated images.

    box = [

        660,
        500,
        1260,
        690,

    ]

    draw.rounded_rectangle(

        box,

        radius=25,

        fill=(
            8,
            12,
            25,
            225,
        ),

        outline=(
            255,
            215,
            0,
            190,
        ),

        width=3,

    )

    font = load_font(
        48,
        bold=True,
    )

    bbox = draw.textbbox(

        (0, 0),

        visual,

        font=font,

    )

    width = (
        bbox[2]
        -
        bbox[0]
    )

    height = (
        bbox[3]
        -
        bbox[1]
    )

    draw.text(

        (
            (1920 - width) // 2,
            595 - height // 2,
        ),

        visual,

        font=font,

        fill="white",

    )

    image.save(
        filename
    )


# ============================================================
# FFMPEG PATH
# ============================================================

def ffmpeg_filter_path(
    filename
):

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
# VIDEO RENDER
# ============================================================

def render_video_segment(
    background,
    ui,
    visual,
    audio,
    ass,
    output,
    position,
    glow_color,
    card_x,
):

    required = [

        background,
        ui,
        audio,
        ass,
        visual,

    ]

    for path in required:

        if not os.path.exists(
            path
        ):

            raise FileNotFoundError(
                "Required video asset missing: "
                f"{os.path.abspath(path)}"
            )

    ass_path = ffmpeg_filter_path(
        ass
    )

    glow = (
        glow_color
        .lstrip("#")
    )

    # Sound bar deliberately sits inside the card,
    # below the speaker text.

    wave_x = card_x + 350

    wave_y = 926

    if position == "left":

        pan_x = "0"

    elif position == "right":

        pan_x = (
            "iw-(iw/zoom)"
        )

    else:

        pan_x = (
            "(iw-(iw/zoom))/2"
        )

    filter_complex = (

        "[0:v]"
        "scale=1920:1080,"
        "zoompan="
        "z='min(zoom+0.00025,1.08)':"
        f"x='{pan_x}':"
        "y='(ih-(ih/zoom))/2':"
        "d=9000:"
        "s=1920x1080:"
        "fps=30"
        "[bg];"

        "[1:v]"
        "scale=1920:1080"
        "[ui];"

        "[3:v]"
        "scale=1920:1080"
        "[visual];"

        "[2:a]"
        "showwaves="
        "s=210x30:"
        "mode=cline:"
        f"colors=0x{glow}:"
        "rate=30"
        "[wave];"

        "[bg][visual]"
        "overlay=0:0"
        "[withvisual];"

        "[withvisual][ui]"
        "overlay=0:0"
        "[base];"

        "[base][wave]"
        f"overlay={wave_x}:{wave_y}"
        "[wavebase];"

        "[wavebase]"
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

        "-i",
        visual,

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

    visual_file = (
        f"visual_{segment_id}.png"
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

    create_ui_overlay(

        speaker_name,

        role,

        topic,

        position,

        glow,

        ui_file,

    )

    create_visual_overlay(

        text,

        visual_file,

    )

    # Retrieve card position again.
    card_x = (
        70
        if position == "left"
        else
        1260
        if position == "right"
        else
        665
    )

    render_video_segment(

        background_file,

        ui_file,

        visual_file,

        audio_file,

        subtitle_file,

        video_file,

        position,

        glow,

        card_x,

    )

    return video_file


# ============================================================
# SCOREBOARD
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

    image = Image.new(

        "RGB",

        (
            VIDEO_WIDTH,
            VIDEO_HEIGHT,
        ),

        (10, 14, 28),

    )

    draw = ImageDraw.Draw(
        image
    )

    header = load_font(
        38,
        bold=True,
    )

    sub = load_font(
        23,
        bold=True,
    )

    small = load_font(
        17
    )

    tiny = load_font(
        15
    )

    def centered(
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
                (1920 - width) // 2,
                y,
            ),

            text,

            font=font,

            fill=fill,

        )

    judge_count = len(
        results
    )

    centered(

        25,

        f"ROUND {round_num} — AI JUDGING PANEL",

        header,

        "#FFD700",

    )

    centered(

        75,

        (
            f"{judge_count} INDEPENDENT AI JUDGES"
            " • EACH CATEGORY SCORED 0–100"
        ),

        sub,

        "white",

    )

    centered(

        120,

        (
            f"ROUND: "
            f"APOLOGIST {round_a:.1f}"
            f"  VS  "
            f"SKEPTIC {round_b:.1f}"
        ),

        sub,

        "white",

    )

    centered(

        160,

        (
            f"CUMULATIVE: "
            f"APOLOGIST {cumulative_a:.1f}"
            f"  VS  "
            f"SKEPTIC {cumulative_b:.1f}"
        ),

        sub,

        "#FFD700",

    )

    # --------------------------------------------------------
    # LEFT CATEGORY PANEL
    # --------------------------------------------------------

    draw.rounded_rectangle(

        [
            80,
            215,
            900,
            420,
        ],

        radius=20,

        fill=(20, 28, 48),

        outline="#334466",

        width=2,

    )

    draw.text(

        (115, 235),

        "CATEGORY AVERAGES",

        font=sub,

        fill="#FFD700",

    )

    draw.text(

        (530, 275),

        "APOLOGIST",

        font=small,

        fill="#00FFCC",

    )

    draw.text(

        (710, 275),

        "SKEPTIC",

        font=small,

        fill="#FF66FF",

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

    for label, ak, bk in categories:

        a = sum(
            r[ak]
            for r in results
        ) / judge_count

        b = sum(
            r[bk]
            for r in results
        ) / judge_count

        draw.text(

            (115, y),

            label,

            font=small,

            fill="white",

        )

        draw.text(

            (550, y),

            f"{a:.1f}",

            font=small,

            fill="#00FFCC",

        )

        draw.text(

            (730, y),

            f"{b:.1f}",

            font=small,

            fill="#FF66FF",

        )

        y += 32

    # --------------------------------------------------------
    # RIGHT JUDGE PANEL
    # --------------------------------------------------------

    draw.rounded_rectangle(

        [
            950,
            215,
            1840,
            975,
        ],

        radius=20,

        fill=(20, 28, 48),

        outline="#334466",

        width=2,

    )

    draw.text(

        (985, 235),

        "INDIVIDUAL JUDGES",

        font=sub,

        fill="#FFD700",

    )

    draw.text(

        (985, 275),

        "PROVIDER / MODEL",

        font=small,

        fill="white",

    )

    draw.text(

        (1640, 275),

        "A",

        font=small,

        fill="#00FFCC",

    )

    draw.text(

        (1700, 275),

        "B",

        font=small,

        fill="#FF66FF",

    )

    row_height = 21

    start_y = 305

    max_rows = 29

    for index, result in enumerate(
        results[:max_rows]
    ):

        y = (
            start_y
            +
            index
            *
            row_height
        )

        short_name = short_model_name(
            result["model"]
        )

        if len(short_name) > 42:

            short_name = (
                short_name[:39]
                +
                "..."
            )

        draw.text(

            (985, y),

            short_name,

            font=tiny,

            fill="white",

        )

        draw.text(

            (1640, y),

            f"{result['A_total']:.0f}",

            font=tiny,

            fill="#00FFCC",

        )

        draw.text(

            (1700, y),

            f"{result['B_total']:.0f}",

            font=tiny,

            fill="#FF66FF",

        )

    if len(results) > max_rows:

        draw.text(

            (
                985,
                start_y
                +
                max_rows
                *
                row_height
                +
                8,
            ),

            (
                f"+{len(results) - max_rows} "
                "additional judges included"
            ),

            font=tiny,

            fill="#FFD700",

        )

    image.save(
        filename
    )


# ============================================================
# PANEL COMMENTARY
# ============================================================

def generate_panel_commentary(
    model,
    side,
    topic,
    round_num,
    debate_text,
    previous_comments,
):

    provider = get_provider(
        model
    )

    recent = "\n".join(
        previous_comments[-6:]
    )

    preferred = (
        "AI Christian Apologist"
        if side == "A"
        else
        "AI Skeptic"
    )

    prompt = f"""
You are an independent AI judge on a debate panel.

You are representing:
{provider}

Topic:
{topic}

Round:
{round_num}

The side you scored higher was:
{preferred}

Give one short natural commentary observation.

Your commentary should explain a genuinely interesting
reasoning issue.

Do not simply say one side was better.

Do not quote either speaker.

Do not repeat these previous observations:

{recent}

You MAY identify yourself by provider in the introduction,
for example:

"OpenAI's judge found..."

But do not mention a specific model version.

Write 2-3 natural spoken sentences.
"""

    response = query_openrouter(

        prompt,

        model,

        timeout=40,

        max_tokens=180,

        temperature=0.85,

    )

    if response:

        return (
            f"{provider} judge: "
            f"{response}"
        )

    return (
        f"{provider} judge: "
        "The stronger argument was the one that "
        "left fewer important assumptions unexplained."
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

        "Today, an AI Christian Apologist faces "
        "an AI Skeptic on one of humanity's biggest "
        "questions. "

        f"We have {judge_count} independent AI judges "
        "available for the panel. "

        "Each side gets the same speaking time, "
        "with three rounds of genuine back and forth. "

        "The judges will score argument strength, "
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
# CONCAT
# ============================================================

def stitch_segments(
    segments,
    output,
):

    list_file = (
        "concat_list.txt"
    )

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

    available_models = (
        discover_models()
    )

    if not available_models:

        print(
            "⚠️ Dynamic discovery failed."
        )

        available_models = (
            FALLBACK_MODELS.copy()
        )

    # --------------------------------------------------------
    # DEBATE MODELS
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
        f"⚖️ Actual AI judging panel: "
        f"{len(judges)}"
    )

    print(
        "📺 Viewer-facing debaters:"
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

        video = create_segment(

            text,

            role,

            name,

            topic,

            segment_id,

            position,

            glow,

        )

        segments.append(
            video
        )

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

        "center",

        "#FFD700",

    )

    # --------------------------------------------------------
    # ROUND HISTORY
    # --------------------------------------------------------

    previous_round_summary = ""

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

        current_round_history = []

        side_a_words = 0

        side_b_words = 0

        round_segments = []

        # ----------------------------------------------------
        # THREE BACK-AND-FORTH EXCHANGES
        # ----------------------------------------------------

        for turn_num in range(
            1,
            TURNS_PER_SIDE_PER_ROUND + 1,
        ):

            # =================================================
            # APOLOGIST
            # =================================================

            apologist_text = (
                generate_debate_turn(

                    "A",

                    topic,

                    round_num,

                    turn_num,

                    current_round_history,

                    previous_round_summary,

                    apologist_model,

                )
            )

            side_a_words += count_words(
                apologist_text
            )

            current_round_history.append(

                "APOLOGIST: "
                +
                apologist_text

            )

            print(

                f"🟢 A{turn_num}: "
                f"{count_words(apologist_text)} words"

            )

            video = create_segment(

                apologist_text,

                "AI Christian Apologist",

                "AI Christian Apologist",

                topic,

                segment_id,

                "left",

                "#00FFCC",

            )

            segments.append(
                video
            )

            segment_id += 1

            # =================================================
            # SKEPTIC
            # =================================================

            skeptic_text = (
                generate_debate_turn(

                    "B",

                    topic,

                    round_num,

                    turn_num,

                    current_round_history,

                    previous_round_summary,

                    skeptic_model,

                )
            )

            side_b_words += count_words(
                skeptic_text
            )

            current_round_history.append(

                "SKEPTIC: "
                +
                skeptic_text

            )

            print(

                f"🟣 B{turn_num}: "
                f"{count_words(skeptic_text)} words"

            )

            video = create_segment(

                skeptic_text,

                "AI Skeptic",

                "AI Skeptic",

                topic,

                segment_id,

                "right",

                "#FF00FF",

            )

            segments.append(
                video
            )

            segment_id += 1

        # ----------------------------------------------------
        # ROUND WORD COUNT
        # ----------------------------------------------------

        print(
            f"📏 Round {round_num} "
            f"word count:"
        )

        print(
            f"   Apologist: "
            f"{side_a_words}"
        )

        print(
            f"   Skeptic: "
            f"{side_b_words}"
        )

        # ----------------------------------------------------
        # BUILD TEXT FOR JUDGES
        # ----------------------------------------------------

        debate_a = "\n".join(

            item.replace(
                "APOLOGIST: ",
                "",
                1,
            )

            for item in current_round_history

            if item.startswith(
                "APOLOGIST:"
            )

        )

        debate_b = "\n".join(

            item.replace(
                "SKEPTIC: ",
                "",
                1,
            )

            for item in current_round_history

            if item.startswith(
                "SKEPTIC:"
            )

        )

        debate_text = {

            "A": debate_a,

            "B": debate_b,

        }

        # ----------------------------------------------------
        # JUDGING
        # ----------------------------------------------------

        results = evaluate_round(

            judges,

            topic,

            round_num,

            debate_text,

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

            f"The {len(results)} AI judges gave "
            f"the AI Christian Apologist an average "
            f"score of {round_a:.1f}, and the AI Skeptic "
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

        score_ui = (
            f"score_ui_r{round_num}.png"
        )

        score_visual = (
            f"score_visual_r{round_num}.png"
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

        create_visual_overlay(

            score_text,

            score_visual,

        )

        render_video_segment(

            scoreboard_file,

            score_ui,

            score_visual,

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

        if results:

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

                    debate_text,

                    panel_comments,

                )
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

            comment_b = (
                generate_panel_commentary(

                    judge_b["model"],

                    "B",

                    topic,

                    round_num,

                    debate_text,

                    panel_comments,

                )
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

        # ----------------------------------------------------
        # ROUND SUMMARY FOR NEXT ROUND
        # ----------------------------------------------------

        previous_round_summary = (
            generate_round_summary(

                topic,

                current_round_history,

                apologist_model,

            )
        )

    # ========================================================
    # FINAL RESULT
    # ========================================================

    add_segment(

        build_outro(

            len(judges),

            cumulative_a,

            cumulative_b,

        ),

        "Moderator",

        "Moderator",

        "center",

        "#FFD700",

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

        "center",

        "#FFD700",

    )

    # ========================================================
    # STITCH
    # ========================================================

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
