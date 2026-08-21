import os
import re
import json
import math
import glob
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

# HARD CEILING.
#
# This is NOT the target number.
#
# If 37 models are actually available and pass testing,
# there will be 37 judges.
#
# If 100+ work, there will be 100 judges.
MAX_JUDGES = 100

JUDGE_WORKERS = 12

MODEL_DISCOVERY_TIMEOUT = 20
MODEL_REQUEST_TIMEOUT = 45
JUDGE_REQUEST_TIMEOUT = 25
JUDGE_PREFLIGHT_TIMEOUT = 15

ROUNDS = 3

MIN_SKEPTIC_WORDS = 450
MAX_SKEPTIC_WORDS = 700

MIN_APOLOGIST_WORDS = 300
MAX_APOLOGIST_WORDS = 500


# ============================================================
# NATURAL EDGE TTS VOICES
# ============================================================

# Publicly these are simply the voices of the participants.
# They are NOT associated with the underlying AI models.

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

def clean_for_speech(text: str) -> str:

    text = re.sub(
        r"\([^)]*\)",
        "",
        text,
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


def count_words(text: str) -> int:

    return len(
        re.findall(
            r"\b[\w'-]+\b",
            text,
        )
    )


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


def clamp_score(value):

    try:
        value = float(value)
    except Exception:
        value = 50

    return max(
        0,
        min(
            100,
            value,
        ),
    )


# ============================================================
# FONT LOADING
# ============================================================

def load_font(
    size: int,
    bold: bool = False,
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
            continue

    return ImageFont.load_default()


# ============================================================
# OPENROUTER
# ============================================================

def openrouter_headers():

    return {
        "Authorization": (
            f"Bearer {OPENROUTER_API_KEY}"
        ),
        "Content-Type": "application/json",
        "HTTP-Referer": "https://openrouter.ai/",
        "X-Title": "AI Debate Arena",
    }


def discover_models() -> List[str]:

    if not OPENROUTER_API_KEY:

        print(
            "❌ OPENROUTER_API_KEY is missing."
        )

        return []

    try:

        response = requests.get(
            OPENROUTER_MODELS_URL,
            headers=openrouter_headers(),
            timeout=MODEL_DISCOVERY_TIMEOUT,
        )

        if response.status_code != 200:

            print(
                "⚠️ Model discovery failed: "
                f"HTTP {response.status_code}"
            )

            return []

        data = response.json()

        models = []

        for model in data.get(
            "data",
            [],
        ):

            model_id = model.get("id")

            if not model_id:
                continue

            lowered = model_id.lower()

            excluded = [
                ":batch",
                "embed",
                "tts",
                "whisper",
                "audio",
                "image",
                "vision",
                "guard",
                "moderation",
                "rerank",
            ]

            if any(
                item in lowered
                for item in excluded
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
            "⚠️ Model discovery exception: "
            f"{str(exc)[:200]}"
        )

        return []


def query_openrouter(
    prompt: str,
    model_id: str,
    timeout: int = MODEL_REQUEST_TIMEOUT,
    max_tokens: int = 1200,
    temperature: float = 0.7,
):

    if not OPENROUTER_API_KEY:
        return None

    if not model_id:
        return None

    if ":batch" in model_id.lower():
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
                        and len(content.strip()) > 10
                    ):
                        return content.strip()

            else:

                print(
                    f"⚠️ {model_id} "
                    f"returned HTTP "
                    f"{response.status_code}"
                )

        except Exception as exc:

            print(
                f"⚠️ Request failed for "
                f"{model_id}: "
                f"{str(exc)[:100]}"
            )

        if attempt < 2:

            import time

            time.sleep(
                1.5 * (attempt + 1)
            )

    return None


# ============================================================
# PRIMARY DEBATE MODEL SELECTION
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

    usable = set(
        available_models
    )

    found = []

    for model in preference:

        if model in usable:
            found.append(model)

    if len(found) >= 2:

        return (
            found[0],
            found[1],
        )

    if len(found) == 1:

        remaining = [
            model
            for model in available_models
            if model != found[0]
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

    raise RuntimeError(
        "At least two usable OpenRouter models "
        "are required for the debate."
    )


# ============================================================
# JUDGE PREFLIGHT
# ============================================================

def judge_model_preflight(
    model_id: str,
) -> Optional[str]:

    if not model_id:
        return None

    lowered = model_id.lower()

    if ":batch" in lowered:
        return None

    response = query_openrouter(
        """
Reply with exactly one word:

READY
""",
        model_id,
        timeout=JUDGE_PREFLIGHT_TIMEOUT,
        max_tokens=5,
        temperature=0,
    )

    if response:

        return model_id

    return None


def choose_judge_panel(
    available_models: List[str],
    primary_models: Tuple[str, str],
) -> Tuple[List[str], List[str]]:

    excluded = set(
        primary_models
    )

    candidates = []

    blocked_terms = [
        ":batch",
        "embed",
        "tts",
        "whisper",
        "audio",
        "image",
        "vision",
        "moderation",
        "rerank",
    ]

    for model in available_models:

        if model in excluded:
            continue

        lowered = model.lower()

        if any(
            blocked in lowered
            for blocked in blocked_terms
        ):
            continue

        candidates.append(model)

    candidates = list(
        dict.fromkeys(candidates)
    )

    preferred_keywords = [
        "gpt",
        "claude",
        "gemini",
        "grok",
        "mistral",
        "llama",
        "qwen",
        "deepseek",
        "command",
        "nemotron",
        "yi",
        "jamba",
    ]

    preferred = [
        model
        for model in candidates
        if any(
            keyword in model.lower()
            for keyword in preferred_keywords
        )
    ]

    others = [
        model
        for model in candidates
        if model not in preferred
    ]

    candidates = (
        sorted(preferred)
        +
        sorted(others)
    )

    print()
    print("=" * 70)
    print("🔎 BUILDING DYNAMIC AI JUDGING PANEL")
    print("=" * 70)

    print(
        f"OpenRouter candidates available: "
        f"{len(candidates)}"
    )

    validated = []

    # --------------------------------------------------------
    # IMPORTANT:
    #
    # 100 is the maximum, NOT the target.
    #
    # Stop once 100 successful judges exist.
    # --------------------------------------------------------

    for model in candidates:

        if len(validated) >= MAX_JUDGES:
            break

        result = judge_model_preflight(
            model
        )

        if result:

            validated.append(
                result
            )

            print(
                f"  ✓ Judge "
                f"{len(validated):02d}: "
                f"{model}"
            )

        else:

            print(
                f"  ✗ Unavailable: "
                f"{model}"
            )

    if len(validated) < 2:

        raise RuntimeError(
            "Fewer than two AI models passed "
            "judge preflight."
        )

    active_panel = validated[
        :MAX_JUDGES
    ]

    reserve_panel = validated[
        MAX_JUDGES:
    ]

    print()
    print(
        f"✅ FINAL AI JUDGING PANEL: "
        f"{len(active_panel)}"
    )

    print(
        f"🛟 RESERVE JUDGES: "
        f"{len(reserve_panel)}"
    )

    return (
        active_panel,
        reserve_panel,
    )


# ============================================================
# ARGUMENT GENERATION
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
This is the opening argument.

Establish the strongest Christian case for the topic.

Introduce substantive arguments rather than filler.
"""

    else:

        context = f"""
This is a continuing debate.

Previous Apologist argument:

{previous_apologist}

Previous Skeptic response:

{previous_skeptic}

Continue naturally from what has already been said.

Do NOT restart the debate.

Do NOT introduce yourself.

Do NOT say what you are going to do.

Do NOT say "in this round".

Do NOT repeat previous arguments.

Identify the strongest unresolved challenge from the Skeptic
and respond to it directly.

Then develop NEW reasoning that moves the debate forward.
"""

    prompt = f"""
You are the AI Christian Apologist in a serious public debate.

Topic:

{topic}

{context}

Write a natural spoken argument for a general YouTube audience.

Requirements:

- {MIN_APOLOGIST_WORDS}-{MAX_APOLOGIST_WORDS} words.
- Conversational.
- Persuasive but intellectually honest.
- Strong logical reasoning.
- Concrete examples and analogies.
- Address the opposing argument fairly.
- Avoid academic jargon.
- No headings.
- No numbered lists.
- Do not mention being an AI.
- Do not mention the underlying model.
- Do not repeat the debate introduction.
- Do not waste words explaining your structure.

Write ONLY the spoken argument.
"""

    response = query_openrouter(
        prompt,
        model,
        max_tokens=1000,
        temperature=0.75,
    )

    if response:

        return response

    return (
        "The Christian case begins with a simple question: "
        "why is there a universe at all rather than nothing? "
        "If everything within the universe depends on something "
        "else, then it is reasonable to ask whether the chain "
        "of explanations ultimately points beyond the universe."
    )


def generate_skeptic(
    topic,
    round_num,
    apologist_text,
    previous_skeptic,
    model,
):

    continuation = ""

    if round_num > 1:

        continuation = f"""
This is a continuing debate.

Your previous response was:

{previous_skeptic}

Do NOT repeat it.

The next response must introduce new objections,
new implications, or a deeper examination of an
assumption that has not yet been properly dealt with.
"""

    prompt = f"""
You are the AI Skeptic in a serious public debate.

Topic:

{topic}

Round:

{round_num}

The AI Christian Apologist has just said:

{apologist_text}

{continuation}

Produce a FULL and FORCEFUL rebuttal.

CRITICAL LENGTH REQUIREMENT:

Write between {MIN_SKEPTIC_WORDS} and {MAX_SKEPTIC_WORDS} words.

This MUST be a substantial spoken response.

Do NOT produce a short answer.

Do NOT say:

"I cannot answer"

"The model is unavailable"

"I don't have enough information"

"As an AI"

"Text was not generated"

Do not merely say the argument is unconvincing.

Actually rebut it.

Address multiple distinct weaknesses.

Explain why those weaknesses matter.

Respond to the strongest part of the Apologist's argument,
not a weak caricature.

Use simple conversational language.

Use concrete examples and analogies.

Avoid unnecessary academic terminology.

Do not introduce yourself.

Do not say "in this round".

Do not describe your strategy.

Do not repeat the Apologist word-for-word.

Do not finish prematurely.

The final paragraph should leave a strong unresolved challenge
for the Apologist to answer.

Write ONLY the spoken rebuttal.
"""

    response = query_openrouter(
        prompt,
        model,
        timeout=MODEL_REQUEST_TIMEOUT,
        max_tokens=1700,
        temperature=0.8,
    )

    if (
        response
        and count_words(response) >= MIN_SKEPTIC_WORDS
    ):

        return response

    print(
        "⚠️ Skeptic response too short. "
        "Running expansion retry..."
    )

    retry_prompt = f"""
The following Skeptic response is too short.

Expand it into a complete spoken rebuttal of at least
{MIN_SKEPTIC_WORDS} words.

Keep its useful reasoning.

Add genuinely substantive material:

1. A new independent objection.
2. A concrete everyday analogy.
3. A direct response to the strongest part of the
   Apologist's argument.
4. An explanation of why the disagreement matters.
5. A final unresolved challenge.

Do not pad with meaningless repetition.

Do not use headings.

Do not mention AI or models.

Apologist:

{apologist_text}

Existing Skeptic response:

{response or "No usable response was generated."}

Return ONLY the finished spoken rebuttal.
"""

    retry = query_openrouter(
        retry_prompt,
        model,
        timeout=MODEL_REQUEST_TIMEOUT,
        max_tokens=1800,
        temperature=0.8,
    )

    if (
        retry
        and count_words(retry) >= 300
    ):

        return retry

    # --------------------------------------------------------
    # FALLBACK THROUGH OTHER AVAILABLE MODELS
    # --------------------------------------------------------

    fallback_models = [
        "openai/gpt-4o-mini",
        "google/gemini-2.0-flash-001",
        "anthropic/claude-3.5-haiku",
        "mistralai/mistral-small",
        "qwen/qwen-2.5-72b-instruct",
        "deepseek/deepseek-chat",
    ]

    for fallback in fallback_models:

        if fallback == model:
            continue

        fallback_response = query_openrouter(
            prompt,
            fallback,
            timeout=MODEL_REQUEST_TIMEOUT,
            max_tokens=1700,
            temperature=0.8,
        )

        if (
            fallback_response
            and count_words(fallback_response) >= 300
        ):

            print(
                f"✅ Skeptic fallback succeeded: "
                f"{fallback}"
            )

            return fallback_response

    # --------------------------------------------------------
    # LAST RESORT
    # --------------------------------------------------------

    return (
        "The central problem with that argument is that it "
        "moves from the fact that something needs an explanation "
        "to the conclusion that we therefore know what that "
        "explanation must be. Even if the universe has a deeper "
        "explanation, that does not automatically establish that "
        "the explanation is a personal creator. We also have to "
        "ask whether the proposed explanation actually explains "
        "more than the alternatives. A good explanation should "
        "solve the original problem rather than simply move it "
        "one step further. So the important question is not just "
        "whether we can imagine a creator, but whether the evidence "
        "gives us a good reason to believe that a creator is the "
        "best explanation."
    )


# ============================================================
# JUDGING
# ============================================================

def judge_round(
    judge_model,
    topic,
    round_num,
    apologist_text,
    skeptic_text,
):

    prompt = f"""
You are an independent AI judge.

You are NOT a participant.

Topic:

{topic}

Round:

{round_num}

SIDE A:
AI CHRISTIAN APOLOGIST

{apologist_text}

SIDE B:
AI SKEPTIC

{skeptic_text}

Evaluate BOTH sides independently.

Use exactly THREE categories:

1. ARGUMENT STRENGTH

How strong, coherent and persuasive were the arguments?

2. REBUTTAL QUALITY

How effectively did the side address and challenge
the opposing position?

3. CLARITY AND REASONING

How clear, understandable and logically structured
was the reasoning?

Each category must be scored from 0 to 100.

Do NOT score based on whether you personally agree
with Christianity or atheism.

Judge the quality of the actual reasoning.

Then calculate each side's average of its three category scores.

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
        judge_model,
        timeout=JUDGE_REQUEST_TIMEOUT,
        max_tokens=250,
        temperature=0.2,
    )

    if not response:
        return None

    try:

        match = re.search(
            r"\{.*\}",
            response,
            re.DOTALL,
        )

        if not match:
            return None

        data = json.loads(
            match.group(0)
        )

        a_argument = clamp_score(
            data.get(
                "A_argument",
                50,
            )
        )

        a_rebuttal = clamp_score(
            data.get(
                "A_rebuttal",
                50,
            )
        )

        a_clarity = clamp_score(
            data.get(
                "A_clarity",
                50,
            )
        )

        b_argument = clamp_score(
            data.get(
                "B_argument",
                50,
            )
        )

        b_rebuttal = clamp_score(
            data.get(
                "B_rebuttal",
                50,
            )
        )

        b_clarity = clamp_score(
            data.get(
                "B_clarity",
                50,
            )
        )

        a_total = (
            a_argument
            + a_rebuttal
            + a_clarity
        ) / 3

        b_total = (
            b_argument
            + b_rebuttal
            + b_clarity
        ) / 3

        return {
            "model": judge_model,

            "A_argument": a_argument,
            "A_rebuttal": a_rebuttal,
            "A_clarity": a_clarity,
            "A_total": round(
                a_total,
                2,
            ),

            "B_argument": b_argument,
            "B_rebuttal": b_rebuttal,
            "B_clarity": b_clarity,
            "B_total": round(
                b_total,
                2,
            ),

            "winner": (
                "A"
                if a_total > b_total
                else "B"
            ),
        }

    except Exception:

        return None


def evaluate_round(
    judges,
    reserve_judges,
    topic,
    round_num,
    apologist,
    skeptic,
):

    results = []

    active_judges = list(
        judges
    )

    reserves = list(
        reserve_judges
    )

    print()
    print(
        f"⚖️ Round {round_num}: "
        f"{len(active_judges)} AI judges"
    )

    def worker(model):

        return judge_round(
            model,
            topic,
            round_num,
            apologist,
            skeptic,
        )

    # --------------------------------------------------------
    # FIRST PASS
    # --------------------------------------------------------

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=JUDGE_WORKERS
    ) as executor:

        futures = {
            executor.submit(
                worker,
                model,
            ): model
            for model in active_judges
        }

        for future in concurrent.futures.as_completed(
            futures
        ):

            model = futures[future]

            try:

                result = future.result()

                if result:

                    results.append(
                        result
                    )

                else:

                    print(
                        f"⚠️ Judge failed: "
                        f"{model}"
                    )

            except Exception as exc:

                print(
                    f"⚠️ Judge exception "
                    f"{model}: "
                    f"{str(exc)[:100]}"
                )

    # --------------------------------------------------------
    # REPLACE FAILED JUDGES
    # --------------------------------------------------------

    missing = (
        len(active_judges)
        - len(results)
    )

    if missing > 0 and reserves:

        replacement_count = min(
            missing,
            len(reserves),
        )

        replacement_models = reserves[
            :replacement_count
        ]

        reserves = reserves[
            replacement_count:
        ]

        print(
            f"🛟 Trying "
            f"{len(replacement_models)} "
            f"reserve judges..."
        )

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=JUDGE_WORKERS
        ) as executor:

            futures = {
                executor.submit(
                    worker,
                    model,
                ): model
                for model in replacement_models
            }

            for future in concurrent.futures.as_completed(
                futures
            ):

                model = futures[future]

                try:

                    result = future.result()

                    if result:

                        results.append(
                            result
                        )

                        print(
                            f"  ✓ Reserve judge: "
                            f"{model}"
                        )

                except Exception:
                    pass

    if not results:

        raise RuntimeError(
            f"Round {round_num} produced "
            "no valid judge scores."
        )

    return (
        results,
        reserves,
    )


def calculate_round_average(
    results,
):

    if not results:

        raise RuntimeError(
            "Cannot calculate score "
            "from zero judges."
        )

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
                    )
                    / 10_000_000,
                }
            )

    with open(
        audio_filename,
        "wb",
    ) as file:

        file.write(
            audio_data
        )

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

    clean_text = clean_for_speech(
        text
    )

    try:

        return asyncio.run(
            _generate_audio_and_words(
                clean_text,
                voice,
                output_audio,
            )
        )

    except Exception as exc:

        print(
            f"⚠️ TTS failed: "
            f"{str(exc)[:100]}"
        )

        return asyncio.run(
            _generate_audio_and_words(
                clean_text,
                VOICES["Moderator"],
                output_audio,
            )
        )


# ============================================================
# SUBTITLES
# ============================================================

def ass_escape(text):

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


def format_ass_time(
    seconds,
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


def generate_paragraph_ass(
    words,
    ass_filename,
):

    if not words:

        return

    header = r"""[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Subtitle,DejaVu Sans,45,&H00FFFFFF,&H0000FFFF,&H00000000,&HCC000000,0,0,0,0,100,100,0,0,1,3,1,5,250,250,360,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    lines = []

    # Larger blocks make tiny timing errors far less distracting.
    chunk_size = 18

    for start_index in range(
        0,
        len(words),
        chunk_size,
    ):

        paragraph = words[
            start_index:
            start_index + chunk_size
        ]

        if not paragraph:
            continue

        paragraph_start = paragraph[0][
            "start"
        ]

        paragraph_end = (
            paragraph[-1]["end"]
            + 0.15
        )

        # Keep entire paragraph visible.
        #
        # Highlighting changes inside the same block.
        #

        for i, active in enumerate(
            paragraph
        ):

            active_start = active[
                "start"
            ]

            if i + 1 < len(
                paragraph
            ):

                active_end = paragraph[
                    i + 1
                ]["start"]

            else:

                active_end = paragraph_end

            pieces = []

            for word in paragraph:

                if word is active:

                    pieces.append(
                        r"{\c&H00FFFF&}"
                        +
                        ass_escape(
                            word["text"]
                        )
                        +
                        r"{\c&HFFFFFF&}"
                    )

                else:

                    pieces.append(
                        ass_escape(
                            word["text"]
                        )
                    )

            text_line = " ".join(
                pieces
            )

            lines.append(
                "Dialogue: 0,"
                +
                format_ass_time(
                    max(
                        paragraph_start,
                        active_start,
                    )
                )
                +
                ","
                +
                format_ass_time(
                    active_end
                )
                +
                ",Subtitle,,0,0,0,,"
                +
                text_line
            )

    with open(
        ass_filename,
        "w",
        encoding="utf-8",
    ) as file:

        file.write(
            header
            +
            "\n".join(lines)
            +
            "\n"
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

    if os.path.exists(
        background_path
    ):

        try:

            base = (
                Image.open(
                    background_path
                )
                .convert("RGB")
                .resize(
                    (1920, 1080)
                )
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

        draw = ImageDraw.Draw(
            base
        )

        for x in range(
            0,
            1920,
            60,
        ):

            draw.line(
                [
                    (x, 0),
                    (x, 1080),
                ],
                fill=(20, 26, 45),
                width=2,
            )

        for y in range(
            0,
            1080,
            60,
        ):

            draw.line(
                [
                    (0, y),
                    (1920, y),
                ],
                fill=(20, 26, 45),
                width=2,
            )

    overlay = Image.new(
        "RGBA",
        base.size,
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
        base.convert("RGBA"),
        overlay,
    ).convert("RGB")

    result.save(output)


# ============================================================
# UI
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

    draw = ImageDraw.Draw(
        image
    )

    title_font = load_font(
        32,
        bold=True,
    )

    name_font = load_font(
        29,
        bold=True,
    )

    role_font = load_font(
        20,
        bold=True,
    )

    # --------------------------------------------------------
    # SMALLER TOP TITLE
    # --------------------------------------------------------

    title = f"TOPIC: {topic}"

    bbox = draw.textbbox(
        (0, 0),
        title,
        font=title_font,
    )

    title_width = (
        bbox[2]
        -
        bbox[0]
    )

    draw.text(
        (
            (1920 - title_width)
            // 2,
            24,
        ),
        title,
        fill="white",
        font=title_font,
    )

    # --------------------------------------------------------
    # SPEAKER CARD
    #
    # Kept low enough not to collide with subtitles.
    # --------------------------------------------------------

    card_width = 600
    card_height = 96

    card_y = 955

    if position == "left":

        card_x = 80

    elif position == "right":

        card_x = 1240

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
        radius=14,
        fill=(18, 26, 46, 235),
        outline=glow_color,
        width=3,
    )

    draw.ellipse(
        [
            card_x + 22,
            card_y + 35,
            card_x + 42,
            card_y + 55,
        ],
        fill=glow_color,
    )

    draw.text(
        (
            card_x + 60,
            card_y + 14,
        ),
        speaker_name,
        fill="white",
        font=name_font,
    )

    draw.text(
        (
            card_x + 60,
            card_y + 54,
        ),
        role_label.upper(),
        fill=glow_color,
        font=role_font,
    )

    image.save(output)

    return card_x


# ============================================================
# SCORECARD
# ============================================================

def generate_round_breakdown_image(
    round_num,
    results,
    round_a,
    round_b,
    cumulative_a,
    cumulative_b,
    output,
):

    background_path = os.path.join(
        os.path.dirname(
            os.path.abspath(__file__)
        ),
        "background.png",
    )

    if os.path.exists(
        background_path
    ):

        try:

            image = (
                Image.open(
                    background_path
                )
                .convert("RGB")
                .resize(
                    (1920, 1080)
                )
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
        (0, 0, 0, 215),
    )

    image = Image.alpha_composite(
        image.convert("RGBA"),
        dark,
    ).convert("RGB")

    draw = ImageDraw.Draw(
        image
    )

    header_font = load_font(
        36,
        bold=True,
    )

    sub_font = load_font(
        22,
        bold=True,
    )

    small_font = load_font(
        18
    )

    def centered(
        y,
        text,
        font,
        fill,
    ):

        bbox = draw.textbbox(
            (0, 0),
            text,
            font=font,
        )

        width = (
            bbox[2]
            -
            bbox[0]
        )

        draw.text(
            (
                (1920 - width)
                // 2,
                y,
            ),
            text,
            fill=fill,
            font=font,
        )

    centered(
        22,
        f"ROUND {round_num} — AI PANEL SCORECARD",
        header_font,
        "#FFD700",
    )

    centered(
        70,
        f"{len(results)} AI JUDGES • THREE CATEGORIES • 0–100",
        sub_font,
        "white",
    )

    centered(
        110,
        (
            f"ROUND AVERAGE   "
            f"APOLOGIST {round_a:.1f}   "
            f"VS   "
            f"SKEPTIC {round_b:.1f}"
        ),
        sub_font,
        "white",
    )

    centered(
        150,
        (
            f"CUMULATIVE   "
            f"APOLOGIST {cumulative_a:.1f}   "
            f"VS   "
            f"SKEPTIC {cumulative_b:.1f}"
        ),
        sub_font,
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
        (150, 210),
        "CATEGORY AVERAGES",
        sub_font,
        fill="#FFD700",
    )

    y = 250

    for label, key_a, key_b in categories:

        avg_a = sum(
            r[key_a]
            for r in results
        ) / len(results)

        avg_b = sum(
            r[key_b]
            for r in results
        ) / len(results)

        draw.text(
            (150, y),
            label,
            small_font,
            fill="white",
        )

        draw.text(
            (500, y),
            f"A {avg_a:.1f}",
            small_font,
            fill="#00FFCC",
        )

        draw.text(
            (700, y),
            f"B {avg_b:.1f}",
            small_font,
            fill="#FF66FF",
        )

        y += 34

    # --------------------------------------------------------
    # INDIVIDUAL SCORES
    # --------------------------------------------------------

    draw.text(
        (1030, 210),
        "INDIVIDUAL JUDGE SCORES",
        sub_font,
        fill="#FFD700",
    )

    draw.text(
        (1030, 245),
        "Judge",
        small_font,
        fill="white",
    )

    draw.text(
        (1510, 245),
        "A",
        small_font,
        fill="#00FFCC",
    )

    draw.text(
        (1570, 245),
        "B",
        small_font,
        fill="#FF66FF",
    )

    row_height = 25

    max_visible = 28

    for i, result in enumerate(
        results[:max_visible]
    ):

        name = result[
            "model"
        ]

        if len(name) > 40:

            name = (
                name[:37]
                +
                "..."
            )

        y = (
            275
            +
            i * row_height
        )

        draw.text(
            (1030, y),
            name,
            small_font,
            fill="white",
        )

        draw.text(
            (1510, y),
            f"{result['A_total']:.0f}",
            small_font,
            fill="#00FFCC",
        )

        draw.text(
            (1570, y),
            f"{result['B_total']:.0f}",
            small_font,
            fill="#FF66FF",
        )

    if len(results) > max_visible:

        draw.text(
            (
                1030,
                275
                +
                max_visible
                *
                row_height
                +
                5,
            ),
            (
                f"+ {len(results) - max_visible} "
                "additional judges included in the average"
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
    # DYNAMIC SOUND BAR
    #
    # showwaves responds to the actual audio waveform.
    # --------------------------------------------------------

    wave_width = 220
    wave_height = 45

    wave_x = (
        card_x
        +
        360
    )

    wave_y = 978

    wave_x = max(
        0,
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

    pan_y = (
        "(ih-(ih/zoom))/2"
    )

    ass_path = os.path.abspath(
        ass
    ).replace(
        "\\",
        "/",
    )

    # Escape colon for FFmpeg.
    ass_path = ass_path.replace(
        ":",
        "\\:",
    )

    glow = (
        glow_color
        .lstrip("#")
    )

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
        f"s={wave_width}x{wave_height}:"
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
            "❌ FFmpeg segment failed:"
        )

        print(
            result.stderr[-3000:]
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

    ass_file = (
        f"subs_{segment_id}.ass"
    )

    bg_file = (
        f"bg_{segment_id}.png"
    )

    ui_file = (
        f"ui_{segment_id}.png"
    )

    video_file = (
        f"segment_{segment_id}.mp4"
    )

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

    previous_comments = "\n".join(
        used_comments[-8:]
    )

    side_name = (
        "AI Christian Apologist"
        if side == "A"
        else
        "AI Skeptic"
    )

    prompt = f"""
You are an independent panel judge commenting after Round {round_num}.

Topic:

{topic}

You preferred:

{side_name}

Your job is NOT to summarise what happened.

Do NOT repeat either debater's arguments.

Do NOT quote them.

Do NOT use their wording.

Do NOT repeat another judge's observation.

These are recent comments:

{previous_comments}

Find a genuinely NEW insight.

For example, you might discuss:

- whether an assumption was properly justified
- whether an argument actually follows from its evidence
- whether a distinction was handled well
- whether a claimed conclusion went further than the evidence
- whether a response successfully changed the burden of proof

Keep it natural and conversational.

Give exactly 2 or 3 spoken sentences.

Do not mention your model name.

Do not say "as an AI".

Return ONLY the spoken commentary.
"""

    response = query_openrouter(
        prompt,
        model,
        timeout=30,
        max_tokens=180,
        temperature=0.9,
    )

    if response:

        return response

    return (
        "The stronger performance came from the side "
        "that left fewer important assumptions unexplained. "
        "That matters because a persuasive conclusion depends "
        "on every major step being properly supported."
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
        "Today, an AI Christian Apologist faces an AI Skeptic "
        "over one of humanity's biggest questions. "
        f"An independent panel of {judge_count} "
        "validated AI judges will evaluate the debate. "
        "Every judge scores argument strength, rebuttal quality, "
        "and clarity of reasoning out of one hundred. "
        "After three rounds, the cumulative average will determine "
        "the winner. "
        "Let's begin."
    )


def build_outro(
    topic,
    judge_count,
    cumulative_a,
    cumulative_b,
):

    if math.isclose(
        cumulative_a,
        cumulative_b,
        abs_tol=0.05,
    ):

        result = (
            "the debate ended in a draw"
        )

    elif cumulative_a > cumulative_b:

        result = (
            "the AI Christian Apologist "
            "wins the debate"
        )

    else:

        result = (
            "the AI Skeptic wins the debate"
        )

    return (
        f"After three rounds, the panel of "
        f"{judge_count} validated AI judges "
        f"gave the AI Christian Apologist "
        f"{cumulative_a:.1f} cumulative points, "
        f"compared with {cumulative_b:.1f} "
        f"for the AI Skeptic. "
        f"Therefore, {result}. "
        "But the final verdict is still yours. "
        "Who do you think actually won?"
    )


# ============================================================
# STITCHING
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

            absolute = os.path.abspath(
                segment
            )

            absolute = absolute.replace(
                "'",
                "'\\''",
            )

            file.write(
                f"file '{absolute}'\n"
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
            result.stderr[-3000:]
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
    print()
    print(
        f"TOPIC: {topic}"
    )
    print()

    # --------------------------------------------------------
    # DISCOVER MODELS
    # --------------------------------------------------------

    available_models = (
        discover_models()
    )

    if not available_models:

        print(
            "⚠️ Dynamic discovery failed. "
            "Using fallback models."
        )

        available_models = [
            model
            for model in FALLBACK_MODELS
            if ":batch" not in model.lower()
        ]

    # --------------------------------------------------------
    # DEBATE MODELS
    # --------------------------------------------------------

    (
        apologist_model,
        skeptic_model,
    ) = choose_primary_debate_models(
        available_models
    )

    # --------------------------------------------------------
    # DYNAMIC JUDGES
    # --------------------------------------------------------

    (
        judges,
        reserve_judges,
    ) = choose_judge_panel(
        available_models,
        (
            apologist_model,
            skeptic_model,
        ),
    )

    advertised_judge_count = len(
        judges
    )

    print()
    print(
        "=" * 70
    )

    print(
        f"🎤 Debate models selected."
    )

    print(
        f"⚖️ Actual AI judging panel: "
        f"{advertised_judge_count}"
    )

    print(
        f"🛟 Reserve judges: "
        f"{len(reserve_judges)}"
    )

    print(
        "=" * 70
    )

    # --------------------------------------------------------
    # VIDEO SEGMENTS
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

        segments.append(
            filename
        )

        segment_id += 1

    # --------------------------------------------------------
    # INTRO
    # --------------------------------------------------------

    add_segment(
        build_intro(
            topic,
            advertised_judge_count,
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
    # SCORE TRACKING
    # --------------------------------------------------------

    cumulative_a = 0.0
    cumulative_b = 0.0

    previous_apologist = ""
    previous_skeptic = ""

    panel_comments = []

    # --------------------------------------------------------
    # ROUNDS
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

        apologist_text = (
            generate_apologist(
                topic,
                round_num,
                previous_apologist,
                previous_skeptic,
                apologist_model,
            )
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

        skeptic_text = (
            generate_skeptic(
                topic,
                round_num,
                apologist_text,
                previous_skeptic,
                skeptic_model,
            )
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
        # HISTORY
        # ----------------------------------------------------

        previous_apologist = (
            apologist_text
        )

        previous_skeptic = (
            skeptic_text
        )

        # ----------------------------------------------------
        # JUDGING
        # ----------------------------------------------------

        (
            results,
            reserve_judges,
        ) = evaluate_round(
            judges,
            reserve_judges,
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
        # SCOREBOARD IMAGE
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
            scoreboard,
        )

        # ----------------------------------------------------
        # SCOREBOARD VOICEOVER
        # ----------------------------------------------------

        score_text = (
            f"Round {round_num} is complete. "
            f"{len(results)} AI judges produced "
            f"an average score of "
            f"{round_a:.1f} for the AI Christian Apologist "
            f"and {round_b:.1f} for the AI Skeptic. "
            f"The cumulative score is "
            f"{cumulative_a:.1f} to "
            f"{cumulative_b:.1f}."
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

        words = (
            generate_edge_audio_and_words(
                score_text,
                "Moderator",
                score_audio,
            )
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
        # ----------------------------------------------------

        a_judges = [
            result
            for result in results
            if result["winner"] == "A"
        ]

        b_judges = [
            result
            for result in results
            if result["winner"] == "B"
        ]

        if not a_judges:
            a_judges = results

        if not b_judges:
            b_judges = results

        judge_a = a_judges[
            0
        ]

        judge_b = b_judges[
            0
        ]

        comment_a = (
            generate_panel_commentary(
                judge_a["model"],
                "A",
                topic,
                round_num,
                apologist_text,
                skeptic_text,
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
                apologist_text,
                skeptic_text,
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

    # --------------------------------------------------------
    # FINAL RESULT
    # --------------------------------------------------------

    add_segment(
        build_outro(
            topic,
            advertised_judge_count,
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

    print()
    print("=" * 70)
    print("✅ DEBATE COMPLETE")
    print("=" * 70)

    print(
        f"🎥 Output: {OUTPUT_FILE}"
    )

    print(
        f"⚖️ AI judges used: "
        f"{advertised_judge_count}"
    )

    print(
        f"🏆 Final score: "
        f"Apologist {cumulative_a:.1f} "
        f"vs Skeptic {cumulative_b:.1f}"
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
