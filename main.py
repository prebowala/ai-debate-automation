import os
import asyncio
import requests
import subprocess
import re
import concurrent.futures
import json
import glob
import time
from difflib import SequenceMatcher

import edge_tts
from PIL import Image, ImageDraw, ImageFont, ImageFilter


# ============================================================
# CONFIGURATION
# ============================================================

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

OUTPUT_VIDEO = "final_debate_output.mp4"
TOPIC_FILE = "topic.txt"
BACKGROUND_FILE = "background.png"

VIDEO_WIDTH = 1920
VIDEO_HEIGHT = 1080
FPS = 30


# ============================================================
# VOICES
# ============================================================

# Christopher is used for GPT/Panelist 1 because it sounds
# considerably more natural for longer spoken passages.
VOICES = {
    "Moderator": "en-US-AndrewMultilingualNeural",
    "AI Christian Apologist": "en-US-ChristopherNeural",
    "AI Skeptic": "en-US-GuyNeural",
    "Panelist 1": "en-US-ChristopherNeural",
    "Panelist 2": "en-US-EmmaMultilingualNeural",
}


# ============================================================
# DEBATE MODELS
#
# Change models here only.
#
# The rest of the program automatically adapts.
# ============================================================

SPEAKER_MODELS = {
    "Apologist": {
        "name": "OpenAI",
        "id": "openai/gpt-4o",
    },

    "Skeptic": {
        "name": "Anthropic",
        "id": "anthropic/claude-3.5-sonnet",
    },
}


# ============================================================
# AI JUDGING PANEL
#
# Add/remove models here freely.
#
# The introduction, scoreboards and final narration will
# automatically use the actual number of models that are
# configured AND successfully respond.
# ============================================================

PANEL_JUDGES = [

    {"name": "OpenAI", "id": "openai/gpt-4o"},
    {"name": "Anthropic", "id": "anthropic/claude-3.5-sonnet"},
    {"name": "Google", "id": "google/gemini-pro-1.5"},
    {"name": "Meta", "id": "meta-llama/llama-3.1-405b-instruct"},
    {"name": "Mistral AI", "id": "mistralai/mistral-large"},
    {"name": "Cohere", "id": "cohere/command-r-plus"},
    {"name": "xAI", "id": "x-ai/grok-2"},
    {"name": "DeepSeek", "id": "deepseek/deepseek-chat"},
    {"name": "Alibaba Cloud", "id": "qwen/qwen-2.5-72b-instruct"},
    {"name": "Microsoft", "id": "microsoft/phi-3-medium-128k-instruct"},
    {"name": "Amazon", "id": "amazon/nova-pro-v1"},
    {"name": "Perplexity", "id": "perplexity/sonar-pro"},
    {"name": "Databricks", "id": "databricks/dbrx-instruct"},
    {"name": "Nous Research", "id": "nousresearch/hermes-3-llama-3.1-405b"},
    {"name": "AllenAI", "id": "allenai/olmo-7b-instruct"},
    {"name": "OpenChat", "id": "openchat/openchat-7b"},
    {"name": "01.AI", "id": "01-ai/yi-large"},
    {"name": "Phind", "id": "phind/phind-model"},
    {"name": "AI21 Labs", "id": "ai21/jamba-1-5-large"},
    {"name": "Hugging Face", "id": "huggingfaceh4/zephyr-7b-beta"},
    {"name": "Nvidia", "id": "nvidia/llama-3.1-nemotron-70b-instruct"},
    {"name": "Moonshot AI", "id": "moonshotai/moonshot-v1-8k"},
    {"name": "MiniMax", "id": "minimax/minimax-text-01"},
    {"name": "Upstage", "id": "upstage/solar-10b-instruct-v1"},
    {"name": "Liquid AI", "id": "liquid/lfm-40b"},
    {"name": "StepFun", "id": "stepfun/step-1-32k"},
    {"name": "Baidu", "id": "baidu/ernie-4.0-8k"},
    {"name": "Tencent", "id": "tencent/hunyuan-standard"},
    {"name": "Xiaomi", "id": "xiaomi/mishiny-v1"},
    {"name": "Novita AI", "id": "novita/llama-3-70b"},
    {"name": "Pygmalion AI", "id": "pygmalionai/mythalion-13b"},
    {"name": "Sao10K", "id": "sao10k/l3-stheno-8b"},
    {"name": "Mlabonne", "id": "mlabonne/neural-chat-7b-v3-3"},
    {"name": "Open-Orca", "id": "open-orca/mistral-7b-openorca"},
    {"name": "Jondurbin", "id": "jondurbin/airoboros-7b-gpt4"},
    {"name": "Aetherius", "id": "aetherius/psyche-7b"},
    {"name": "NeverSleep", "id": "neversleep/llama-3-lumimaid-70b"},
    {"name": "Nexusflow", "id": "nexusflow/nexusraven-v2-13b"},
    {"name": "Sanctum", "id": "sanctumai/mercurial-7b"},
    {"name": "Fimbulvetr", "id": "fimbulvetr/fimbulvetr-v2"},
    {"name": "Kcpp", "id": "kcpp/goliath-120b"},
    {"name": "Ghost", "id": "ghost/ghost-v1"},
    {"name": "Matrix", "id": "matrix/matrix-7b"},
    {"name": "Epsilon", "id": "epsilon/epsilon-lm"},
    {"name": "Open-Thoughts", "id": "open-thoughts/open-thoughts-7b"},
    {"name": "NeuralChat", "id": "openchat/openchat-8b"},
    {"name": "Recursion", "id": "recursion/recursion-7b"},
    {"name": "Vxt", "id": "vxt/vxt-7b"},
    {"name": "Kunoichi", "id": "kunoichi/kunoichi-7b"},
    {"name": "Discute", "id": "discute/discute-model"},
    {"name": "Llama-Factory", "id": "llamafactory/llama-3-instruct"},
    {"name": "PrimeIntellect", "id": "primeintellect/intellect-1"},
    {"name": "Syllogism", "id": "syllogism/syllogism-ai"},
]


# ============================================================
# GENERAL SETTINGS
# ============================================================

NUMBER_OF_ROUNDS = 3

# Minimum Skeptic response length.
MIN_SKEPTIC_WORDS = 500

# Maximum attempts to obtain an adequately long Skeptic response.
MAX_SKEPTIC_ATTEMPTS = 3

# Number of words displayed in each subtitle block.
SUBTITLE_WORDS_PER_BLOCK = 9

# Small delay added at the end of subtitle blocks.
SUBTITLE_END_PADDING = 0.12


# ============================================================
# CACHE CLEANUP
# ============================================================

def cleanup_cache():

    print("🧹 Cleaning temporary files...")

    extensions = [
        "*.mp4",
        "*.mp3",
        "*.ass",
        "*.png",
        "*_list.txt",
    ]

    protected = {
        OUTPUT_VIDEO,
        BACKGROUND_FILE,
    }

    for ext in extensions:

        for file in glob.glob(ext):

            if file in protected:
                continue

            try:
                os.remove(file)
            except Exception:
                pass

    print("✨ Workspace is clean!")


# ============================================================
# TEXT CLEANING
# ============================================================

def clean_for_speech(text):

    if not text:
        return ""

    text = re.sub(r"\([^)]*\)", "", text)

    text = re.sub(
        r"[*#_`–—]",
        "",
        text
    )

    text = (
        text
        .replace(":", " ")
        .replace(";", " ")
        .replace('"', "")
        .replace("&", "and")
    )

    text = re.sub(r"\s+", " ", text)

    return text.strip()


def word_count(text):

    return len(re.findall(r"\b[\w'-]+\b", text))


# ============================================================
# FONT LOADING
# ============================================================

def load_font(size, bold=True):

    if bold:

        paths = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "C:\\Windows\\Fonts\\arialbd.ttf",
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        ]

    else:

        paths = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "C:\\Windows\\Fonts\\arial.ttf",
            "/System/Library/Fonts/Supplemental/Arial.ttf",
        ]

    for path in paths:

        try:
            return ImageFont.truetype(path, size)

        except IOError:
            continue

    return ImageFont.load_default()


# ============================================================
# OPENROUTER
# ============================================================

def query_openrouter(
    prompt,
    model_id,
    timeout=60,
    max_tokens=1500,
    temperature=0.7
):

    if not OPENROUTER_API_KEY:

        print("❌ OPENROUTER_API_KEY is missing.")

        return None

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }

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
                headers=headers,
                json=payload,
                timeout=timeout,
            )

            if response.status_code == 200:

                data = response.json()

                content = (
                    data
                    .get("choices", [{}])[0]
                    .get("message", {})
                    .get("content")
                )

                if content:

                    content = content.strip()

                    if len(content) > 20:
                        return content

            else:

                print(
                    f"⚠️ Model {model_id} returned "
                    f"HTTP {response.status_code}"
                )

        except Exception as e:

            print(
                f"⚠️ OpenRouter attempt {attempt + 1} "
                f"failed for {model_id}: {e}"
            )

        time.sleep(1.5)

    return None


# ============================================================
# EDGE TTS
# ============================================================

async def _generate_audio_and_words(
    text,
    voice,
    audio_filename
):

    communicate = edge_tts.Communicate(
        text,
        voice
    )

    audio_data = bytearray()

    words = []

    async for chunk in communicate.stream():

        if chunk["type"] == "audio":

            audio_data.extend(chunk["data"])

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

    with open(audio_filename, "wb") as f:

        f.write(audio_data)

    return words


def estimate_word_timings(text):

    """
    Emergency subtitle timing fallback.

    Uses a realistic average speaking rate rather than
    the old fixed 0.35 sec per word.
    """

    raw_words = text.split()

    if not raw_words:
        return []

    total_words = len(raw_words)

    estimated_total_duration = max(
        2.0,
        total_words / 2.45
    )

    time_per_word = (
        estimated_total_duration /
        total_words
    )

    result = []

    for i, word in enumerate(raw_words):

        start = i * time_per_word

        end = start + (
            time_per_word * 0.92
        )

        result.append(
            {
                "text": word,
                "start": start,
                "duration": end - start,
                "end": end,
            }
        )

    return result


def generate_edge_audio(
    text,
    role,
    output_audio
):

    voice = VOICES.get(
        role,
        VOICES["Moderator"]
    )

    safe_text = clean_for_speech(text)

    try:

        words = asyncio.run(
            _generate_audio_and_words(
                safe_text,
                voice,
                output_audio,
            )
        )

    except Exception as e:

        print(
            f"⚠️ TTS failed with {voice}: {e}"
        )

        words = []

    if not words:

        print(
            "⚠️ Edge TTS supplied no word boundaries. "
            "Using timing fallback."
        )

        words = estimate_word_timings(
            safe_text
        )

    return words


# ============================================================
# ASS SUBTITLES
# ============================================================

def format_ass_time(seconds):

    seconds = max(0, seconds)

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


def ass_escape(text):

    return (
        text
        .replace("\\", "\\\\")
        .replace("{", "\\{")
        .replace("}", "\\}")
    )


def generate_karaoke_ass(
    words,
    ass_filename
):

    """
    Creates centred block subtitles.

    Each block contains several words, but the currently
    spoken word is highlighted using ASS karaoke timing.

    This is much more stable visually than creating a new
    subtitle sentence every few words.
    """

    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding

Style: Debate,Arial,48,&H00FFFFFF,&H00FFFF00,&H00000000,&HCC000000,1,0,0,0,100,100,1,0,1,3,1,5,180,180,420,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    lines = []

    for start_index in range(
        0,
        len(words),
        SUBTITLE_WORDS_PER_BLOCK
    ):

        block = words[
            start_index:
            start_index + SUBTITLE_WORDS_PER_BLOCK
        ]

        if not block:
            continue

        block_start = block[0]["start"]

        block_end = (
            block[-1]["end"] +
            SUBTITLE_END_PADDING
        )

        karaoke_words = []

        for word in block:

            duration_cs = max(
                1,
                int(
                    word["duration"] *
                    100
                )
            )

            clean_word = ass_escape(
                word["text"]
            )

            karaoke_words.append(
                f"{{\\k{duration_cs}}}"
                f"{clean_word}"
            )

        text_line = " ".join(
            karaoke_words
        )

        lines.append(
            "Dialogue: 0,"
            f"{format_ass_time(block_start)},"
            f"{format_ass_time(block_end)},"
            f"Debate,,0,0,0,,"
            f"{text_line}"
        )

    with open(
        ass_filename,
        "w",
        encoding="utf-8"
    ) as f:

        f.write(
            header +
            "\n".join(lines) +
            "\n"
        )


# ============================================================
# BACKGROUND
# ============================================================

def hex_to_rgba(
    hex_str,
    alpha
):

    hex_str = hex_str.lstrip("#")

    return (
        int(hex_str[0:2], 16),
        int(hex_str[2:4], 16),
        int(hex_str[4:6], 16),
        alpha,
    )


def create_background(
    pos,
    glow_color,
    bg_out
):

    script_dir = os.path.dirname(
        os.path.abspath(__file__)
    )

    background_path = os.path.join(
        script_dir,
        BACKGROUND_FILE
    )

    if os.path.exists(background_path):

        try:

            base_img = (
                Image.open(background_path)
                .convert("RGB")
                .resize(
                    (
                        VIDEO_WIDTH,
                        VIDEO_HEIGHT
                    )
                )
            )

        except Exception:

            base_img = Image.new(
                "RGB",
                (
                    VIDEO_WIDTH,
                    VIDEO_HEIGHT
                ),
                (12, 16, 32)
            )

    else:

        base_img = Image.new(
            "RGB",
            (
                VIDEO_WIDTH,
                VIDEO_HEIGHT
            ),
            (12, 16, 32)
        )

        draw = ImageDraw.Draw(
            base_img
        )

        for x in range(
            0,
            VIDEO_WIDTH,
            60
        ):

            draw.line(
                [(x, 0), (x, VIDEO_HEIGHT)],
                fill=(20, 26, 45),
                width=2
            )

        for y in range(
            0,
            VIDEO_HEIGHT,
            60
        ):

            draw.line(
                [(0, y), (VIDEO_WIDTH, y)],
                fill=(20, 26, 45),
                width=2
            )

    overlay = Image.new(
        "RGBA",
        base_img.size,
        (0, 0, 0, 0)
    )

    draw = ImageDraw.Draw(
        overlay
    )

    if pos == "left":
        cx = 400

    elif pos == "right":
        cx = 1520

    else:
        cx = 960

    for r in range(
        700,
        50,
        -50
    ):

        alpha = int(
            15 *
            (
                1 -
                r / 700
            )
        )

        draw.ellipse(
            [
                cx - r,
                540 - r,
                cx + r,
                540 + r
            ],
            fill=hex_to_rgba(
                glow_color,
                alpha
            )
        )

    img = Image.alpha_composite(
        base_img.convert("RGBA"),
        overlay.filter(
            ImageFilter.GaussianBlur(30)
        )
    ).convert("RGB")

    img.save(bg_out)


# ============================================================
# UI
# ============================================================

def create_ui_overlay(
    speaker_name,
    role_label,
    topic,
    pos,
    glow_color,
    ui_out
):

    ui_img = Image.new(
        "RGBA",
        (
            VIDEO_WIDTH,
            VIDEO_HEIGHT
        ),
        (0, 0, 0, 0)
    )

    draw = ImageDraw.Draw(
        ui_img
    )

    # Smaller title
    font_title = load_font(
        34,
        True
    )

    font_name = load_font(
        30,
        True
    )

    font_role = load_font(
        22,
        True
    )

    # --------------------------------------------------------
    # TOP TITLE
    # --------------------------------------------------------

    title = f"TOPIC: {topic}"

    bbox = draw.textbbox(
        (0, 0),
        title,
        font=font_title
    )

    title_width = (
        bbox[2] -
        bbox[0]
    )

    draw.text(
        (
            (
                VIDEO_WIDTH -
                title_width
            ) // 2,
            28
        ),
        title,
        fill="white",
        font=font_title
    )

    # --------------------------------------------------------
    # SPEAKER CARD
    #
    # Kept low enough that it does not collide with subtitles.
    # --------------------------------------------------------

    card_width = 600
    card_height = 105

    if pos == "left":

        card_x = 100

    elif pos == "right":

        card_x = (
            VIDEO_WIDTH -
            card_width -
            100
        )

    else:

        card_x = (
            VIDEO_WIDTH -
            card_width
        ) // 2

    card_y = 900

    draw.rounded_rectangle(
        [
            card_x,
            card_y,
            card_x + card_width,
            card_y + card_height
        ],
        radius=16,
        fill=(18, 26, 46, 235),
        outline=glow_color,
        width=3
    )

    draw.ellipse(
        [
            card_x + 28,
            card_y + 40,
            card_x + 50,
            card_y + 62
        ],
        fill=glow_color
    )

    draw.text(
        (
            card_x + 75,
            card_y + 20
        ),
        speaker_name,
        fill="white",
        font=font_name
    )

    draw.text(
        (
            card_x + 75,
            card_y + 62
        ),
        role_label.upper(),
        fill=glow_color,
        font=font_role
    )

    ui_img.save(ui_out)

    return card_x


# ============================================================
# VIDEO SEGMENT
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
    zoom_bg=True
):

    ff_color = (
        "0x" +
        glow_color.lstrip("#")
    )

    safe_ass_path = (
        ass_path
        .replace("\\", "/")
        .replace(":", "\\:")
    )

    if zoom_bg:

        if position == "left":

            pan_x = "0"

        elif position == "right":

            pan_x = "iw-(iw/zoom)"

        else:

            pan_x = (
                "(iw-(iw/zoom))/2"
            )

        pan_y = (
            "(ih-(ih/zoom))/2"
        )

        bg_filter = (
            "[0:v]"
            "scale=1920:1080,"
            "zoompan="
            "z='min(zoom+0.0007,1.12)':"
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

    # Waveform is positioned near speaker card,
    # not inside subtitle area.
    wave_x = card_x + 385
    wave_y = 925

    filter_complex = (
        f"{bg_filter}"

        "[1:v]"
        "scale=1920:1080"
        "[ui];"

        f"[2:a]"
        f"showwaves="
        f"s=160x40:"
        f"mode=cline:"
        f"colors={ff_color}"
        "[wave];"

        "[bg_processed]"
        "[ui]"
        "overlay=0:0"
        "[bg_with_ui];"

        "[bg_with_ui]"
        "[wave]"
        f"overlay={wave_x}:{wave_y},"
        f"ass='{safe_ass_path}'"
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
        "20",

        "-pix_fmt",
        "yuv420p",

        "-c:a",
        "aac",

        "-b:a",
        "192k",

        "-shortest",

        output_path
    ]

    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    if result.returncode != 0:

        print(
            "❌ FFmpeg segment failed:"
        )

        print(
            result.stderr[-3000:]
        )

        raise RuntimeError(
            "FFmpeg failed creating segment."
        )


# ============================================================
# SCOREBOARD
# ============================================================

def generate_round_breakdown_image(
    round_num,
    judge_results,
    total_a,
    total_b,
    cum_a,
    cum_b,
    img_out
):

    script_dir = os.path.dirname(
        os.path.abspath(__file__)
    )

    background_path = os.path.join(
        script_dir,
        BACKGROUND_FILE
    )

    if os.path.exists(
        background_path
    ):

        try:

            img = (
                Image.open(
                    background_path
                )
                .convert("RGB")
                .resize(
                    (
                        VIDEO_WIDTH,
                        VIDEO_HEIGHT
                    )
                )
            )

        except Exception:

            img = Image.new(
                "RGB",
                (
                    VIDEO_WIDTH,
                    VIDEO_HEIGHT
                ),
                (12, 16, 32)
            )

    else:

        img = Image.new(
            "RGB",
            (
                VIDEO_WIDTH,
                VIDEO_HEIGHT
            ),
            (12, 16, 32)
        )

    overlay = Image.new(
        "RGBA",
        (
            VIDEO_WIDTH,
            VIDEO_HEIGHT
        ),
        (0, 0, 0, 225)
    )

    img = Image.alpha_composite(
        img.convert("RGBA"),
        overlay
    ).convert("RGB")

    draw = ImageDraw.Draw(
        img
    )

    font_header = load_font(
        40,
        True
    )

    font_sub = load_font(
        24,
        True
    )

    font_model = load_font(
        17,
        False
    )

    judge_count = len(
        judge_results
    )

    def draw_centered(
        y,
        text,
        font,
        fill
    ):

        bbox = draw.textbbox(
            (0, 0),
            text,
            font=font
        )

        draw.text(
            (
                (
                    VIDEO_WIDTH -
                    (
                        bbox[2] -
                        bbox[0]
                    )
                ) // 2,
                y
            ),
            text,
            fill=fill,
            font=font
        )

    draw_centered(
        55,
        f"ROUND {round_num} — {judge_count}-AI PANEL VERDICT",
        font_header,
        "#FFD700"
    )

    draw_centered(
        115,
        (
            f"Round Average: "
            f"Apologist {total_a}  |  "
            f"Skeptic {total_b}"
        ),
        font_sub,
        "#FFFFFF"
    )

    draw_centered(
        155,
        (
            f"Cumulative Score: "
            f"Apologist {cum_a}  |  "
            f"Skeptic {cum_b}"
        ),
        font_sub,
        "#FFFFFF"
    )

    favored_a = [
        j["name"]
        for j in judge_results
        if j["favored"] == "A"
    ]

    favored_b = [
        j["name"]
        for j in judge_results
        if j["favored"] == "B"
    ]

    draw.text(
        (150, 210),
        f"APOLOGIST — {len(favored_a)}",
        fill="#00FFCC",
        font=font_sub
    )

    draw.text(
        (1050, 210),
        f"SKEPTIC — {len(favored_b)}",
        fill="#FF00FF",
        font=font_sub
    )

    def render_clean_list(
        names,
        start_x,
        start_y,
        accent
    ):

        x_col = start_x
        y_col = start_y

        for i, name in enumerate(names):

            draw.text(
                (
                    x_col,
                    y_col
                ),
                f"• {name}",
                fill=accent,
                font=font_model
            )

            y_col += 28

            if (
                i + 1
            ) % 18 == 0:

                y_col = start_y
                x_col += 240

    render_clean_list(
        favored_a,
        150,
        255,
        "#00FFCC"
    )

    render_clean_list(
        favored_b,
        1050,
        255,
        "#FF00FF"
    )

    img.save(
        img_out
    )


# ============================================================
# REPETITION DETECTION
# ============================================================

def similarity(
    text_a,
    text_b
):

    if not text_a or not text_b:
        return 0

    return SequenceMatcher(
        None,
        text_a.lower(),
        text_b.lower()
    ).ratio()


def is_too_similar(
    candidate,
    previous_texts,
    threshold=0.52
):

    candidate_clean = (
        candidate.lower().strip()
    )

    for previous in previous_texts:

        if similarity(
            candidate_clean,
            previous
        ) >= threshold:

            return True

    return False


# ============================================================
# SKEPTIC GENERATION
# ============================================================

def generate_skeptic_rebuttal(
    topic,
    round_num,
    apologist_text,
    previous_skeptic=None
):

    model = SPEAKER_MODELS[
        "Skeptic"
    ]

    previous_note = ""

    if previous_skeptic:

        previous_note = f"""
Previous Skeptic response:

{previous_skeptic}

You MUST NOT simply repeat the structure,
phrases or arguments from that previous response.
"""

    prompt = f"""
You are the Skeptic in a serious YouTube debate.

TOPIC:
{topic}

ROUND:
{round_num}

THE APOLOGIST'S COMPLETE ARGUMENT:
{apologist_text}

Your task is to produce a genuinely substantial rebuttal.

NON-NEGOTIABLE REQUIREMENTS:

1. Write AT LEAST 500 words.

2. Write at least 6 substantial paragraphs.

3. Address the Apologist's argument point-by-point.

4. Identify the strongest part of their argument before explaining
why it still does not establish the conclusion.

5. Do not merely say "this is an assumption."
Explain exactly why the assumption matters.

6. Use concrete everyday examples and analogies.

7. Use natural conversational language suitable for YouTube.

8. Avoid unnecessary academic terminology.

9. Do not repeat the same argument in different words.

10. Do not end early.

11. Do not provide a conclusion after only two or three paragraphs.
Develop the rebuttal fully.

12. Every paragraph should advance a distinct objection.

13. Explicitly respond to the actual wording and reasoning
of the Apologist rather than giving a generic skeptical speech.

14. The response should sound like a confident debater speaking
naturally, not like an academic essay.

15. Do NOT use headings such as "Point 1", "Point 2", etc.
Make it sound like natural spoken debate.

{previous_note}

Now write the complete rebuttal.
"""

    for attempt in range(
        MAX_SKEPTIC_ATTEMPTS
    ):

        print(
            f"🎙️ Generating Skeptic "
            f"rebuttal attempt {attempt + 1}..."
        )

        text = query_openrouter(
            prompt,
            model["id"],
            timeout=90,
            max_tokens=2200,
            temperature=0.72
        )

        if not text:
            continue

        count = word_count(text)

        print(
            f"   Skeptic response: "
            f"{count} words"
        )

        if count >= MIN_SKEPTIC_WORDS:

            return text

        # ----------------------------------------------------
        # If too short, explicitly tell the model to expand.
        # ----------------------------------------------------

        prompt = f"""
The previous Skeptic response was too short.

It contained approximately {count} words.

You MUST rewrite it as a substantially longer rebuttal
of at least 500 words.

Here is the previous response:

{text}

The original Apologist argument was:

{apologist_text}

Expand the response substantially.

Add genuinely new objections.
Address overlooked parts of the Apologist's reasoning.
Use concrete examples.
Do not pad the answer with repetition.

Return ONLY the complete rewritten rebuttal.
"""

    # Last-resort second model request.
    emergency_prompt = f"""
Write a very detailed skeptical rebuttal to this argument.

TOPIC:
{topic}

APOLOGIST:
{apologist_text}

The response MUST contain at least 500 words and 6 substantial
paragraphs.

Each paragraph must make a distinct objection.

Use simple conversational language.
Use examples and analogies.
Directly address the Apologist.
Do not repeat yourself.
Do not use headings.

Return only the rebuttal.
"""

    text = query_openrouter(
        emergency_prompt,
        model["id"],
        timeout=100,
        max_tokens=2500,
        temperature=0.75
    )

    if text and word_count(text) >= 400:
        return text

    # This should almost never be reached.
    # Better to return whatever was generated than silently
    # manufacture debate content.
    if text:
        return text

    return (
        "The Skeptic's response could not be generated "
        "because the selected AI model was unavailable."
    )


# ============================================================
# JUDGE
# ============================================================

def evaluate_single_judge(
    judge,
    topic,
    round_num,
    text_a,
    text_b
):

    prompt = f"""
You are an independent judge evaluating Round {round_num}
of a debate.

TOPIC:
{topic}

APOLOGIST:
{text_a}

SKEPTIC:
{text_b}

Judge the quality of reasoning, directness, evidence,
internal consistency, clarity and ability to answer the
opposing argument.

Do NOT judge based on whether you personally agree with
Christianity, atheism or any particular worldview.

Score each side from 0 to 100.

IMPORTANT:
Return ONLY valid JSON.

Example:
{{"A": 86, "B": 81}}
"""

    response = query_openrouter(
        prompt,
        judge["id"],
        timeout=30,
        max_tokens=100
    )

    if not response:
        return None

    try:

        match = re.search(
            r"\{.*?\}",
            response,
            re.DOTALL
        )

        if not match:
            return None

        scores = json.loads(
            match.group(0)
        )

        score_a = float(
            scores.get(
                "A",
                scores.get(
                    "Side A"
                )
            )
        )

        score_b = float(
            scores.get(
                "B",
                scores.get(
                    "Side B"
                )
            )
        )

        if not (
            0 <= score_a <= 100 and
            0 <= score_b <= 100
        ):
            return None

        favored = (
            "A"
            if score_a >= score_b
            else "B"
        )

        return {
            "name": judge["name"],
            "id": judge["id"],
            "score_a": score_a,
            "score_b": score_b,
            "favored": favored,
        }

    except Exception:

        return None


# ============================================================
# PANEL EVALUATION
# ============================================================

def evaluate_panel(
    topic,
    round_num,
    text_a,
    text_b
):

    print(
        f"\n⚖️ Evaluating Round {round_num} "
        f"with {len(PANEL_JUDGES)} configured AI judges..."
    )

    results = []

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=12
    ) as executor:

        futures = [
            executor.submit(
                evaluate_single_judge,
                judge,
                topic,
                round_num,
                text_a,
                text_b
            )
            for judge in PANEL_JUDGES
        ]

        for future in concurrent.futures.as_completed(
            futures
        ):

            try:

                result = future.result()

                if result:

                    results.append(result)

            except Exception as e:

                print(
                    f"⚠️ Judge failed: {e}"
                )

    if not results:

        raise RuntimeError(
            "No AI judges returned valid scores. "
            "Check your OpenRouter model IDs/API key."
        )

    results.sort(
        key=lambda x: x["name"]
    )

    print(
        f"✅ {len(results)} AI judges "
        f"successfully evaluated the round."
    )

    return results


# ============================================================
# UNIQUE PANEL COMMENTARY
# ============================================================

def generate_unique_commentary(
    judge,
    side,
    topic,
    round_num,
    text_a,
    text_b,
    previous_commentary
):

    if side == "A":

        side_name = "Apologist"
        role = (
            "Act as a logician. "
            "Focus on why the winning argument "
            "was internally stronger."
        )

    else:

        side_name = "Skeptic"
        role = (
            "Act as a pragmatist. "
            "Focus on why the winning argument "
            "was more convincing when tested against "
            "real-world reasoning."
        )

    previous = "\n".join(
        previous_commentary[-8:]
    )

    prompt = f"""
You are {judge["name"]}, an independent AI judge.

Topic:
{topic}

Round:
{round_num}

You voted for the {side_name}.

Apologist argument:
{text_a}

Skeptic argument:
{text_b}

You are now giving a very short on-camera observation.

{role}

CRITICAL:

Your observation MUST be new.

Do NOT:
- summarize the debate
- repeat either debater's wording
- repeat an analogy used by either debater
- say "the Apologist argued..."
- say "the Skeptic argued..."
- simply announce who won
- repeat an observation already made by another judge

Instead identify ONE subtle feature of the reasoning
that a normal viewer may have missed.

Use exactly 2 or 3 sentences.

Use natural spoken English.

Previously used judge observations:

{previous}

Return ONLY your new observation.
"""

    return query_openrouter(
        prompt,
        judge["id"],
        timeout=35,
        max_tokens=180
    )


def get_unique_commentary(
    judge,
    side,
    topic,
    round_num,
    text_a,
    text_b,
    previous_commentary
):

    for attempt in range(3):

        text = generate_unique_commentary(
            judge,
            side,
            topic,
            round_num,
            text_a,
            text_b,
            previous_commentary
        )

        if not text:
            continue

        if not is_too_similar(
            text,
            previous_commentary,
            threshold=0.48
        ):

            return text

    # Do not create fake commentary.
    return (
        f"{judge['name']} found that the "
        f"{'Apologist' if side == 'A' else 'Skeptic'} "
        f"had the stronger reasoning in this round."
    )


# ============================================================
# MAIN PIPELINE
# ============================================================

def run_debate_pipeline():

    cleanup_cache()

    # --------------------------------------------------------
    # TOPIC
    # --------------------------------------------------------

    if not os.path.exists(
        TOPIC_FILE
    ):

        with open(
            TOPIC_FILE,
            "w",
            encoding="utf-8"
        ) as f:

            f.write(
                "Does the universe require a creator?"
            )

    with open(
        TOPIC_FILE,
        "r",
        encoding="utf-8"
    ) as f:

        topic = (
            f.read()
            .strip()
            .replace(",", " -")
        )

    print(
        f"\n🎬 TOPIC: {topic}\n"
    )

    # --------------------------------------------------------
    # VIDEO SEGMENTS
    # --------------------------------------------------------

    final_segments = []

    frame_counter = 0

    previous_commentary = []

    cumulative_score_a = 0
    cumulative_score_b = 0

    last_skeptic_text = None

    # --------------------------------------------------------
    # DYNAMIC PANEL DESCRIPTION
    # --------------------------------------------------------

    configured_panel_count = len(
        PANEL_JUDGES
    )

    panel_description = (
        f"{configured_panel_count}-AI panel"
    )

    # --------------------------------------------------------
    # SEGMENT FUNCTION
    # --------------------------------------------------------

    def add_video_segment(
        text,
        role,
        name,
        topic_str
    ):

        nonlocal frame_counter

        if not text:
            return

        if "Apologist" in role:

            pos = "left"
            glow = "#00FFCC"

        elif "Skeptic" in role:

            pos = "right"
            glow = "#FF00FF"

        elif "Panelist" in role:

            pos = "center"
            glow = "#3399FF"

        else:

            pos = "center"
            glow = "#FFD700"

        audio_file = (
            f"aud_{frame_counter}.mp3"
        )

        bg_file = (
            f"bg_{frame_counter}.png"
        )

        ui_file = (
            f"ui_{frame_counter}.png"
        )

        ass_file = (
            f"ass_{frame_counter}.ass"
        )

        video_file = (
            f"seg_{frame_counter}.mp4"
        )

        words = generate_edge_audio(
            text,
            role,
            audio_file
        )

        generate_karaoke_ass(
            words,
            ass_file
        )

        create_background(
            pos,
            glow,
            bg_file
        )

        card_x = create_ui_overlay(
            name,
            role,
            topic_str,
            pos,
            glow,
            ui_file
        )

        render_video_segment(
            bg_file,
            ui_file,
            audio_file,
            ass_file,
            video_file,
            pos,
            glow,
            card_x,
            zoom_bg=True
        )

        final_segments.append(
            video_file
        )

        frame_counter += 1

    # ========================================================
    # INTRO
    # ========================================================

    apologist_name = SPEAKER_MODELS[
        "Apologist"
    ]["name"]

    skeptic_name = SPEAKER_MODELS[
        "Skeptic"
    ]["name"]

    intro = f"""
Welcome to the Ultimate AI Debate Arena.

Today we are putting two AI debaters to the test on one of humanity's biggest questions.

Representing the Apologist side is {apologist_name}.

Representing the Skeptic side is {skeptic_name}.

And judging the debate is an independent panel of {configured_panel_count} AI models.

No human applause.
No emotional appeals.
Just arguments, rebuttals, and an independent AI verdict.

Let's get into it.
"""

    add_video_segment(
        intro,
        "Moderator",
        "Moderator",
        topic
    )

    # ========================================================
    # TOPIC INTRODUCTION
    # ========================================================

    add_video_segment(
        f"Today's debate topic is: {topic}.",
        "Moderator",
        "Moderator",
        topic
    )

    add_video_segment(
        (
            f"{apologist_name} will argue the Apologist position, "
            f"while {skeptic_name} will challenge it. "
            f"After each round, our {configured_panel_count} "
            f"AI judges will independently score the arguments."
        ),
        "Moderator",
        "Moderator",
        topic
    )

    # ========================================================
    # ROUNDS
    # ========================================================

    for round_num in range(
        1,
        NUMBER_OF_ROUNDS + 1
    ):

        print(
            f"\n=============================="
        )

        print(
            f"ROUND {round_num}"
        )

        print(
            f"==============================\n"
        )

        add_video_segment(
            (
                f"Round {round_num}. "
                f"The Apologist takes the floor."
            ),
            "Moderator",
            "Moderator",
            topic
        )

        # ----------------------------------------------------
        # APOLOGIST
        # ----------------------------------------------------

        previous_counter = ""

        if last_skeptic_text:

            previous_counter = f"""
The Skeptic's previous response was:

{last_skeptic_text}

Directly address the strongest objection raised there,
but do not simply repeat your previous argument.
"""

        apologist_prompt = f"""
You are the Apologist in a serious YouTube debate.

Topic:
{topic}

Round:
{round_num}

Present a compelling pro argument.

Requirements:

- Speak naturally as if talking to a large YouTube audience.
- Use simple everyday language.
- Avoid unnecessary academic jargon.
- Use concrete examples and analogies.
- Make the reasoning easy to follow.
- Make this a substantial response of approximately
  250 to 350 words.
- Develop one or more clear arguments rather than
  repeating the same claim.
- Anticipate the strongest skeptical objection.
- Do not mention that you are an AI.
- Do not use headings.

{previous_counter}

Return ONLY the spoken debate response.
"""

        text_a = query_openrouter(
            apologist_prompt,
            SPEAKER_MODELS[
                "Apologist"
            ]["id"],
            timeout=90,
            max_tokens=1300,
            temperature=0.72
        )

        if not text_a:

            raise RuntimeError(
                "Apologist model failed to respond."
            )

        add_video_segment(
            text_a,
            "AI Christian Apologist",
            f"Apologist — {apologist_name}",
            topic
        )

        # ----------------------------------------------------
        # SKEPTIC
        # ----------------------------------------------------

        add_video_segment(
            (
                "Now the Skeptic responds. "
                "The challenge is to address the argument "
                "directly rather than simply disagree with it."
            ),
            "Moderator",
            "Moderator",
            topic
        )

        text_b = generate_skeptic_rebuttal(
            topic,
            round_num,
            text_a,
            last_skeptic_text
        )

        last_skeptic_text = text_b

        add_video_segment(
            text_b,
            "AI Skeptic",
            f"Skeptic — {skeptic_name}",
            topic
        )

        # ====================================================
        # PANEL
        # ====================================================

        judge_results = evaluate_panel(
            topic,
            round_num,
            text_a,
            text_b
        )

        round_total_a = round(
            sum(
                j["score_a"]
                for j in judge_results
            ) /
            len(judge_results)
        )

        round_total_b = round(
            sum(
                j["score_b"]
                for j in judge_results
            ) /
            len(judge_results)
        )

        cumulative_score_a += (
            round_total_a
        )

        cumulative_score_b += (
            round_total_b
        )

        # ====================================================
        # SCORECARD
        # ====================================================

        score_summary = (
            f"Round {round_num} is complete. "
            f"Our {len(judge_results)} participating AI judges "
            f"gave the Apologist an average score of "
            f"{round_total_a}, compared with "
            f"{round_total_b} for the Skeptic. "
            f"The cumulative score is now "
            f"{cumulative_score_a} to "
            f"{cumulative_score_b}."
        )

        bg_img = (
            f"score_bg_r{round_num}.png"
        )

        ui_img = (
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
            round_total_a,
            round_total_b,
            cumulative_score_a,
            cumulative_score_b,
            bg_img
        )

        card_x = create_ui_overlay(
            "Moderator",
            "Moderator",
            topic,
            "center",
            "#FFD700",
            ui_img
        )

        words = generate_edge_audio(
            score_summary,
            "Moderator",
            score_audio
        )

        generate_karaoke_ass(
            words,
            score_ass
        )

        render_video_segment(
            bg_img,
            ui_img,
            score_audio,
            score_ass,
            score_video,
            "center",
            "#FFD700",
            card_x,
            zoom_bg=False
        )

        final_segments.append(
            score_video
        )

        # ====================================================
        # SELECT PANELISTS
        # ====================================================

        rep_a_pool = [
            j for j in judge_results
            if j["favored"] == "A"
        ]

        rep_b_pool = [
            j for j in judge_results
            if j["favored"] == "B"
        ]

        # If everybody votes one way, still produce useful
        # commentary from two different judges.
        if rep_a_pool:

            rep_a = rep_a_pool[0]

        else:

            rep_a = judge_results[0]

        if rep_b_pool:

            rep_b = rep_b_pool[0]

        else:

            rep_b = (
                judge_results[1]
                if len(judge_results) > 1
                else judge_results[0]
            )

        # ====================================================
        # UNIQUE PANEL COMMENTARY A
        # ====================================================

        commentary_a = get_unique_commentary(
            rep_a,
            "A",
            topic,
            round_num,
            text_a,
            text_b,
            previous_commentary
        )

        previous_commentary.append(
            commentary_a
        )

        add_video_segment(
            commentary_a,
            "Panelist 1",
            f"Judge — {rep_a['name']}",
            topic
        )

        # ====================================================
        # UNIQUE PANEL COMMENTARY B
        # ====================================================

        commentary_b = get_unique_commentary(
            rep_b,
            "B",
            topic,
            round_num,
            text_a,
            text_b,
            previous_commentary
        )

        previous_commentary.append(
            commentary_b
        )

        add_video_segment(
            commentary_b,
            "Panelist 2",
            f"Judge — {rep_b['name']}",
            topic
        )

    # ========================================================
    # FINAL VERDICT
    # ========================================================

    if cumulative_score_a > cumulative_score_b:

        winner = "Apologist"

    elif cumulative_score_b > cumulative_score_a:

        winner = "Skeptic"

    else:

        winner = "draw"

    if winner == "draw":

        winner_sentence = (
            "The final result is a draw."
        )

    else:

        winner_sentence = (
            f"Victory goes to the {winner}."
        )

    final_score_text = (
        f"After {NUMBER_OF_ROUNDS} rounds, "
        f"the participating AI panel awarded "
        f"the Apologist {cumulative_score_a} total points "
        f"and the Skeptic {cumulative_score_b}. "
        f"{winner_sentence}"
    )

    add_video_segment(
        final_score_text,
        "Moderator",
        "Moderator",
        topic
    )

    # ========================================================
    # OUTRO
    # ========================================================

    outro = (
        f"That concludes today's debate in the AI Debate Arena. "
        f"We put {apologist_name} against {skeptic_name}, "
        f"with {configured_panel_count} AI models acting as judges. "
        f"But the final verdict is up to you. "
        f"Which side made the stronger case? "
        f"Let us know in the comments, and subscribe for more "
        f"AI debates tackling humanity's biggest questions."
    )

    add_video_segment(
        outro,
        "Moderator",
        "Moderator",
        topic
    )

    # ========================================================
    # CONCAT
    # ========================================================

    concat_file = (
        "concat_list.txt"
    )

    with open(
        concat_file,
        "w",
        encoding="utf-8"
    ) as f:

        for segment in final_segments:

            f.write(
                f"file '{segment}'\n"
            )

    print(
        "\n🎬 Stitching final video..."
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
            OUTPUT_VIDEO
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    if result.returncode != 0:

        print(
            result.stderr[-5000:]
        )

        raise RuntimeError(
            "Final FFmpeg concatenation failed."
        )

    # ========================================================
    # FINAL REPORT
    # ========================================================

    print(
        "\n======================================"
    )

    print(
        "🎉 DEBATE COMPLETE"
    )

    print(
        "======================================"
    )

    print(
        f"Topic: {topic}"
    )

    print(
        f"Configured AI judges: "
        f"{configured_panel_count}"
    )

    print(
        f"Rounds: {NUMBER_OF_ROUNDS}"
    )

    print(
        f"Final score: "
        f"Apologist {cumulative_score_a} "
        f"vs Skeptic {cumulative_score_b}"
    )

    print(
        f"Winner: {winner}"
    )

    print(
        f"Output: {OUTPUT_VIDEO}"
    )

    print(
        "======================================"
    )

    cleanup_cache()


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    try:

        run_debate_pipeline()

    except KeyboardInterrupt:

        print(
            "\n⛔ Pipeline cancelled by user."
        )

    except Exception as e:

        print(
            "\n❌ PIPELINE FAILED:"
        )

        print(
            str(e)
        )

        raise
