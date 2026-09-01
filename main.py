import os
import re
import json
import glob
import random
import asyncio
import requests
import subprocess
import concurrent.futures
import time
import base64
import hashlib
from io import BytesIO
import edge_tts
from PIL import Image, ImageDraw, ImageFont, ImageFilter

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
OUTPUT_FILE = "final_debate_output.mp4"
VIDEO_W = 1920
VIDEO_H = 1080
FPS = 30

# Two rounds keeps the model rotation exactly even: each model argues each side
# once. Three exchanges per side per round preserves the total runtime.
ROUNDS = 2
TURNS_PER_SIDE_PER_ROUND = 3
# A wider panel is the honest answer to jurors being thrown out for flip
# flopping: with half a small panel discarded, the remaining marks carry too
# much weight. One seat per lab still holds, so widening means more labs.
MAX_JUDGES = 17

# ---- Runtime budget: the finished video should land between 10 and 15 minutes.
TARGET_TOTAL_SECONDS = 780.0        # aim for 13:00
MIN_TOTAL_SECONDS = 600.0
MAX_TOTAL_SECONDS = 900.0
DEFAULT_WORDS_PER_SEC = 2.55        # edge-tts neural voices run ~150-160 wpm
MIN_TURN_WORDS = 95
MAX_TURN_WORDS = 175
# A real turn shorter than the pacing target is still a real turn. Below this
# it is too thin to use, and nothing is ever substituted for it.
MIN_ACCEPTABLE_TURN_WORDS = 70
# Fewer judges than this is not a panel; the build stops rather than pretend.
MIN_PANEL_SIZE = 2

# Estimates used to reserve room for the non-debate segments while pacing.
EST_INTRO_SEC = 30.0
EST_OUTRO_SEC = 50.0
EST_SCORECARD_SEC = 16.0
EST_COMMENTARY_SEC = 22.0
EST_POLL_SEC = 40.0        # each of the opening and closing poll segments
COMMENTARY_WORDS = 55

USED_ARGUMENTS = set()
USED_JUDGE_EXPLANATIONS = set()

# ---------------------------------------------------------------------------
# Voice. edge-tts is free and needs no key. Set TTS_PROVIDER=elevenlabs and
# ELEVENLABS_API_KEY to use ElevenLabs instead; a roughly thirteen minute debate
# is about eleven thousand characters, so budget accordingly. Voice ids are
# overridable so you can swap in voices from your own ElevenLabs library.
# ---------------------------------------------------------------------------
TTS_PROVIDER = os.environ.get("TTS_PROVIDER", "edge").strip().lower()
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY")
ELEVEN_MODEL = os.environ.get("ELEVEN_MODEL", "eleven_multilingual_v2")
ELEVEN_VOICE_A = os.environ.get("ELEVEN_VOICE_A", "pNInz6obpgDQGcFmaJgB")    # Adam
ELEVEN_VOICE_B = os.environ.get("ELEVEN_VOICE_B", "EXAVITQu4vr4xnSDxMaL")    # Bella
ELEVEN_VOICE_MOD = os.environ.get("ELEVEN_VOICE_MOD", "onwK4e9ZLuTAKqWW03F9")  # Daniel
ELEVEN_JUDGE_VOICES = [
    v.strip() for v in os.environ.get(
        "ELEVEN_JUDGE_VOICES",
        "21m00Tcm4TlvDq8ikWAM,ErXwobaYiN019PkySvjV,TxGEqnHWrfWFTfGW9XjX,"
        "AZnzlk1XvdvUeBnXmlld,VR6AewLTigWG4xSOukaG,MF3mGyEYCl7XYWbV9V6O,"
        "yoZ06aMxZJJ28mgz3iRy,LcfcDJNUP1GQjkzn1xUU,jsCqWAovK2LkecY7zXl4,"
        "ThT5KcBeYPX3keUQqHPh,XB0fDUnXU5powFXDhCwa,pqHfZKP75CvOlQylNhV4"
    ).split(",") if v.strip()
]

# Speaker slots are fixed, so voices stay identical build to build whatever the topic is.
# Plain English voices, never the Multilingual variants. Those detect language
# per phrase and switch accent mid sentence, so "Adam ate" and "serpent" came
# out in French. The ordinary voices stay in English whatever the word.
SIDE_A_VOICE = os.environ.get("SIDE_A_VOICE", "en-US-BrianNeural")
SIDE_B_VOICE = os.environ.get("SIDE_B_VOICE", "en-US-AvaNeural")
MODERATOR_VOICE = os.environ.get("MODERATOR_VOICE", "en-US-AndrewNeural")

# Used only if a configured voice turns out not to exist on the service.
VOICE_FALLBACKS = ["en-US-GuyNeural", "en-US-JennyNeural", "en-GB-RyanNeural",
                   "en-US-AriaNeural", "en-US-DavisNeural"]

JUDGE_VOICES = [
    "en-US-JennyNeural",
    "en-GB-RyanNeural",
    "en-US-GuyNeural",
    "en-GB-LibbyNeural",
    "en-US-DavisNeural",
    "en-AU-WilliamNeural",
    "en-CA-ClaraNeural",
    "en-GB-SoniaNeural",
    "en-IE-ConnorNeural",
    "en-AU-NatashaNeural",
    "en-NZ-MitchellNeural",
    "en-US-AriaNeural",
    "en-GB-ThomasNeural",
    "en-US-MichelleNeural",
    "en-CA-LiamNeural",
    "en-GB-MaisieNeural",
    "en-US-RogerNeural",
    "en-IE-EmilyNeural",
    "en-AU-DuncanNeural",
]
JUDGE_VOICE_MAP = {}

# Speaker slot -> (screen position, accent colour)
SLOT_STYLE = {
    "A": ("left", "#00FFCC"),
    "B": ("right", "#FF00FF"),
    "JUDGE": ("center", "#3399FF"),
    "MOD": ("center", "#FFD700"),
}

# ---------------------------------------------------------------------------
# Model roster. With OpenRouter credits, pin the models: the same debaters and
# the same panel in every video keeps the channel consistent and makes results
# comparable between videos. Leave PAID_MODELS empty to stay on free models.
# Set USE_PAID_MODELS=1 (or put ids in DEBATER_MODELS / PANEL_MODELS) to use them.
# ---------------------------------------------------------------------------
# Paid models are the default now. Set USE_PAID_MODELS=0 to fall back to the
# free tier, which is thin and produces frequent substitutions.
USE_PAID_MODELS = os.environ.get("USE_PAID_MODELS", "1").strip() not in ("0", "false", "no")

# Model ids churn constantly, so the roster is resolved at run time instead of
# hardcoded: these are name fragments in rough capability order within each lab,
# matched against whatever the account can actually see. Anything not matched
# still qualifies through its lab's catch-all at the end of each list.
STRONGEST_BY_LAB = {
    "anthropic": ["claude-opus-5", "claude-opus", "claude-fable", "claude-sonnet-5",
                  "claude-sonnet", "claude"],
    "openai": ["gpt-5.6-sol", "gpt-5.6", "gpt-5", "o4", "gpt-4.1", "gpt-4o", "gpt"],
    "google": ["gemini-3.1-pro", "gemini-3-pro", "gemini-pro", "gemini-3.6",
               "gemini-3", "gemini"],
    "x-ai": ["grok-4.6", "grok-4", "grok"],
    "deepseek": ["deepseek-v4", "deepseek-v3", "deepseek-chat", "deepseek"],
    "meta-llama": ["llama-4", "llama-3.3", "llama"],
    "mistralai": ["mistral-large", "mistral-medium", "mistral"],
    "qwen": ["qwen3-max", "qwen3", "qwen-2.5-72b", "qwen"],
    "nvidia": ["nemotron-3-ultra", "nemotron-3", "nemotron"],
    "cohere": ["command-a", "command-r-plus", "command"],
    "amazon": ["nova-pro", "nova"],
    "ai21": ["jamba-large", "jamba"],
    "moonshotai": ["kimi-k2", "kimi"],
    "01-ai": ["yi-large", "yi"],
    "microsoft": ["phi-4", "phi-3", "phi", "mai-"],
    "perplexity": ["sonar-pro", "sonar"],
    "z-ai": ["glm-5", "glm-4.6", "glm-4", "glm"],
    "minimax": ["minimax-m2", "minimax-m1", "minimax"],
    "liquid": ["lfm-2", "lfm-40b", "lfm"],
    "baidu": ["ernie-5", "ernie-4", "ernie"],
    "tencent": ["hunyuan-large", "hunyuan"],
    "reka": ["reka-core", "reka-flash", "reka"],
    "nousresearch": ["hermes-4", "hermes-3", "hermes"],
    "allenai": ["olmo-3", "olmo-2", "olmo"],
    "inclusionai": ["ling-", "ring-"],
    "stepfun-ai": ["step-3", "step-"],
    "arcee-ai": ["afm-", "arcee"],
}

# The two labs that argue, in order. The rest of the labs make up the panel.
DEBATER_LABS = [l.strip() for l in os.environ.get(
    "DEBATER_LABS", "anthropic,openai").split(",") if l.strip()]

# The two arguers. These are what the audience actually hears, so this is where
# quality shows most. Order matters only for which side each starts on.
DEBATER_MODELS = [m.strip() for m in os.environ.get(
    "DEBATER_MODELS",
    "anthropic/claude-sonnet-4.5,openai/gpt-4o"
).split(",") if m.strip()]

# The panel. Breadth of provider matters more here than raw capability: the
# point is independent judges, so spread them across labs.
PANEL_MODELS = [m.strip() for m in os.environ.get(
    "PANEL_MODELS",
    "google/gemini-2.5-flash,"
    "openai/gpt-4o-mini,"
    "anthropic/claude-3.5-haiku,"
    "meta-llama/llama-3.3-70b-instruct,"
    "mistralai/mistral-large,"
    "deepseek/deepseek-chat,"
    "qwen/qwen-2.5-72b-instruct,"
    "x-ai/grok-2-1212,"
    "cohere/command-r-plus"
).split(",") if m.strip()]

# Extra models polled for consensus but never used to debate or judge. Reasoning
# models belong here: they cannot narrate (they leak their scratchpad into
# speech) but they answer a poll perfectly well.
POLL_EXTRA_MODELS = [m.strip() for m in os.environ.get(
    "POLL_EXTRA_MODELS",
    "deepseek/deepseek-r1,"
    "google/gemini-2.5-pro,"
    "anthropic/claude-opus-4.1,"
    "openai/o4-mini,"
    "amazon/nova-pro-v1,"
    "ai21/jamba-1.5-large"
).split(",") if m.strip()]

# One seat per lab. Models from the same lab share training data and tuning, so
# a second one is close to a duplicate vote, and giving some labs two seats and
# others one is an arbitrary weighting rather than a sampling design. If this is
# raised above one, the aggregation below still counts each lab once.
MAX_POLL_PER_PROVIDER = int(os.environ.get("MAX_POLL_PER_PROVIDER", "1"))

# Rough per million token prices, only used to print a cost estimate before a
# run. Set to a frontier tier since that is what the default roster resolves to.
PRICE_PER_M = {"in": float(os.environ.get("PRICE_IN", "6.0")),
               "out": float(os.environ.get("PRICE_OUT", "24.0"))}

FALLBACK_MODELS = [
    "openai/gpt-4o-mini:free",
    "anthropic/claude-3-haiku:free",
    "google/gemini-flash-1.5-8b:free",
    "meta-llama/llama-3.2-3b-instruct:free",
    "mistralai/mistral-7b-instruct:free",
    "deepseek/deepseek-chat:free",
    "qwen/qwen-2.5-7b-instruct:free",
    "nvidia/llama-3.1-nemotron-70b-instruct:free",
]

# Models that narrate their own deliberation instead of answering. They are
# never used for debate turns, judging or commentary.
REASONING_MODEL_MARKERS = [
    "deepseek-r1", "/r1", "-r1:", "qwq", "o1-", "o3-", "-thinking", "thinking-",
    "reasoner", "reasoning",
]


def is_reasoning_model(mid):
    low = (mid or "").lower()
    return any(marker in low for marker in REASONING_MODEL_MARKERS)


PROVIDER_ALIASES = {
    "openai": "OpenAI", "anthropic": "Anthropic", "google": "Google",
    "x-ai": "xAI", "deepseek": "DeepSeek", "mistralai": "Mistral",
    "meta-llama": "Meta", "qwen": "Qwen", "nvidia": "Nvidia",
    "cohere": "Cohere", "microsoft": "Microsoft", "ai21": "AI21",
    "amazon": "Amazon", "perplexity": "Perplexity", "01-ai": "Yi",
    "gryphe": "Gryphe", "sao10k": "Sao10K", "liquid": "Liquid",
    "thudm": "THUDM", "moonshotai": "Moonshot",
}

# Providers eligible for the panel. A wider pool means a more independent
# panel; the ordering only decides who is picked first when there are more
# candidates than seats.
PANEL_PROVIDERS = [
    "openai", "anthropic", "google", "meta-llama", "mistralai", "deepseek",
    "qwen", "nvidia", "x-ai", "cohere", "microsoft", "ai21", "amazon",
    "01-ai", "liquid", "moonshotai", "thudm",
]

# ---------------------------------------------------------------------------
# Branding. These lines are identical in every video, so the channel opens and
# closes the same way each time. Only the middle of the intro and the result
# sentence of the outro change with the topic.
# ---------------------------------------------------------------------------
CHANNEL_NAME = "Talked Round"
INTRO_OPENING = (
    "Welcome to Talked Round, where we ask a room full of AIs what they think, get two of "
    "them to argue it out, then ask again and see which ones got talked round."
)
INTRO_RULES = (
    "The two debaters swap sides halfway through, so neither one gets the easier job. And "
    "before a word is said, we ask a separate room of AIs, one from each of the big labs, "
    "where they already stand, so at the end we can see who changed their mind. Let's get "
    "into it."
)
OUTRO_SIGNOFF = (
    "Every round is marked twice, once each way round, and any juror who flip flops is "
    "thrown out. Tell us what you want settled next in the comments, subscribe, and we "
    "will see you next time."
)

# ---------------------------------------------------------------------------
# What this video actually shows, in plain words. Every line here is something
# the pipeline genuinely does. build_method_note() fills in the real numbers and
# writes it to video_description.txt.
# ---------------------------------------------------------------------------
METHOD_CLAIMS = [
    "Two AIs argue the question, one on each side. They are told which side to take, so "
    "what they say is not what they personally think. They swap sides halfway through, so "
    "neither side gets carried by the better arguer.",

    "A separate jury of AIs marks the arguing. They are never told which side is which. "
    "Every round is marked twice, with the two sides swapped round, and any juror who "
    "picks a different winner the second time is thrown out instead of counted.",

    "Separately, one AI from each company is asked what it personally thinks, once before "
    "the debate and once after reading the whole thing. Where they end up after hearing "
    "both sides is the headline result. How many of them moved, and which side the jury "
    "marked as the better arguer, are side notes to that.",

    "Nothing here is scripted or made up. If an AI gives no usable answer it is left out "
    "and we say so. No argument, score or verdict in this video was written by a person.",
]

METHOD_LIMITS = [
    "This shows how these particular AIs reacted to this particular debate. It does not "
    "show whether the answer is true.",

    "These AIs are not really separate opinions. They are built and trained in similar "
    "ways, so when they agree it means less than the number of them suggests.",

    "If an AI refuses to pick a side, we report that. It is never counted as agreement.",
]

SPEECH_SYSTEM_PROMPT = (
    "You are a person speaking out loud on a live debate stage. Everything you produce is "
    "spoken word that goes straight to a text to speech engine, so it is heard, never read.\n"
    "Give only the finished speech. Never think out loud on the page: no weighing of options, "
    "no 'maybe I could use this', no 'that's too technical', no considering an example and then "
    "rejecting it, no commentary on your own wording or length. Decide silently, then say the "
    "one thing you decided on.\n"
    "Never describe what you are about to do, never announce your structure, never say you will "
    "address or discuss something. No headings, numbered lists, bullet points, stage directions, "
    "word counts or markdown.\n"
    "Write it the way it should sound. No slashes, no emoji, no symbols, no abbreviations: "
    "write 'and' not '/', 'percent' not '%'. Use contractions and plain language, and start "
    "with the substance."
)


class DebateGenerationError(RuntimeError):
    """Raised when real content could not be generated. Nothing is faked."""


# Every model the build may fall back to for one turn, in order, deduplicated.
AVAILABLE_MODELS = []
# Models sitting on the judging panel this build.
JUDGE_MODELS = set()


def turn_model_chain(preferred):
    """Models to try for one debate turn, best first.

    Panel models go last: a judge that writes a turn would otherwise score its
    own text. If one is used anyway, the caller recuses it from that round.
    """
    chain = [preferred] + [m for m in AVAILABLE_MODELS if m != preferred]
    chain += [m for m in FALLBACK_MODELS if m not in chain]
    chain = [m for m in dict.fromkeys(chain) if m and not is_reasoning_model(m)]
    non_panel = [m for m in chain if m not in JUDGE_MODELS]
    panel = [m for m in chain if m in JUDGE_MODELS]
    return non_panel + panel


def provider_from_model(mid):
    if not mid:
        return "Unknown"
    return PROVIDER_ALIASES.get(mid.split("/", 1)[0].lower().strip(), mid.split("/", 1)[0].title())


def get_judge_short_name(mid):
    """A short label for the scorecard, specific enough to tell variants apart."""
    low = (mid or "").lower()
    name = low.split("/", 1)[1] if "/" in low else low

    if "claude" in name:
        for tier in ("opus", "fable", "mythos", "sonnet", "haiku"):
            if tier in name:
                ver = re.search(r"(\d+(?:\.\d+)?)", name)
                return f"Claude {tier.title()}" + (f" {ver.group(1)}" if ver else "")
        return "Claude"
    if name.startswith("o1") or name.startswith("o3") or name.startswith("o4"):
        return name.split("-")[0]
    if "gpt" in name:
        ver = re.search(r"gpt-?(\d+(?:\.\d+)?o?)", name)
        track = next((t for t in ("sol", "luna", "terra", "mini", "nano") if t in name), "")
        base = f"GPT-{ver.group(1)}" if ver else "GPT"
        return f"{base} {track.title()}".strip()
    if "gemini" in name:
        ver = re.search(r"gemini-?(\d+(?:\.\d+)?)", name)
        track = next((t for t in ("pro", "flash", "ultra") if t in name), "")
        base = f"Gemini {ver.group(1)}" if ver else "Gemini"
        return f"{base} {track.title()}".strip()
    if "grok" in name:
        ver = re.search(r"grok-?(\d+(?:\.\d+)?)", name)
        return f"Grok {ver.group(1)}" if ver else "Grok"
    if "nemotron" in name:
        ver = re.search(r"nemotron-?(\d+(?:\.\d+)?)", name)
        return f"Nemotron {ver.group(1)}" if ver else "Nemotron"
    if "deepseek" in name:
        if "r1" in name:
            return "DeepSeek R1"
        ver = re.search(r"v(\d+(?:\.\d+)?)", name)
        return f"DeepSeek V{ver.group(1)}" if ver else "DeepSeek"
    if "llama" in name:
        ver = re.search(r"llama-?(\d+(?:\.\d+)?)", name)
        return f"Llama {ver.group(1)}" if ver else "Llama"
    if "qwen" in name:
        ver = re.search(r"qwen-?(\d+(?:\.\d+)?)", name)
        return ("Qwen Max" if "max" in name
                else f"Qwen {ver.group(1)}" if ver else "Qwen")
    if "mistral" in name or "magistral" in name:
        return ("Mistral Large" if "large" in name
                else "Mistral Medium" if "medium" in name else "Mistral")
    if "command" in name:
        return "Command A" if re.search(r"command-a\b", name) else "Command R"
    if "nova" in name:
        return "Nova Pro" if "pro" in name else "Nova"
    if "jamba" in name:
        return "Jamba"
    if "kimi" in name:
        return "Kimi"
    if "phi" in name:
        ver = re.search(r"phi-?(\d+(?:\.\d+)?)", name)
        return f"Phi {ver.group(1)}" if ver else "Phi"
    if "sonar" in name:
        return "Sonar"
    if "glm" in name:
        ver = re.search(r"glm-?(\d+(?:\.\d+)?)", name)
        return f"GLM {ver.group(1)}" if ver else "GLM"
    if "minimax" in name:
        return "MiniMax"
    if "lfm" in name:
        return "LFM"
    if "ernie" in name:
        return "ERNIE"
    if "hunyuan" in name:
        return "Hunyuan"
    if "reka" in name:
        return "Reka"
    if "hermes" in name:
        return "Hermes"
    if "olmo" in name:
        return "OLMo"
    if "yi-" in name or name == "yi":
        return "Yi"
    return provider_from_model(mid)


def cleanup_cache():
    for pat in ["*.mp4", "*.mp3", "*.ass", "*.png", "*_list.txt"]:
        for fn in glob.glob(pat):
            if fn in [OUTPUT_FILE, "background.png", "topic.txt",
                      "video_description.txt"]:
                continue
            try:
                os.remove(fn)
            except OSError:
                pass


NUMBER_WORDS = {1: "One", 2: "Two", 3: "Three", 4: "Four", 5: "Five",
                6: "Six", 7: "Seven", 8: "Eight", 9: "Nine", 10: "Ten",
                11: "Eleven", 12: "Twelve", 13: "Thirteen", 14: "Fourteen",
                15: "Fifteen", 16: "Sixteen", 17: "Seventeen", 18: "Eighteen",
                19: "Nineteen", 20: "Twenty"}


def number_word(n):
    return NUMBER_WORDS.get(n, str(n))


def sentence_case(s):
    """Uppercase the first letter only, leaving side labels like YES intact."""
    return s[:1].upper() + s[1:] if s else s


def count_words(t):
    return len(re.findall(r"\b[\w'-]+\b", t or ""))


def trim_to_words(text, max_words):
    """Cut on a sentence boundary rather than mid-thought."""
    if count_words(text) <= max_words:
        return text
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    kept, total = [], 0
    for s in sentences:
        w = count_words(s)
        if kept and total + w > max_words:
            break
        kept.append(s)
        total += w
        if total >= max_words:
            break
    out = " ".join(kept).strip()
    if not out:
        out = " ".join(text.split()[:max_words]).rstrip(",;: ") + "."
    return out


# All-caps words a voice should keep spelling out. Everything else in capitals
# gets softened, because a voice reads a bare "NO" as the two letters N, O.
SPOKEN_ACRONYMS = {
    "AI", "DNA", "RNA", "US", "USA", "UK", "EU", "UN", "NASA", "NHS", "GDP",
    "IQ", "CO2", "HIV", "AIDS", "FBI", "CIA", "NATO", "CEO", "TV", "PC", "GPT",
    "LGBT", "PTSD", "ADHD", "COVID", "UFO", "USB", "GPS", "PHD", "MP", "MRI",
}


def soften_caps(t):
    """Turn shouted words into ordinary ones so the voice says them, not spells them.

    Side labels are stored in capitals for the on-screen cards. Left as they
    are, a text to speech engine treats a short all-caps word as an initialism,
    so "NO" comes out as "en oh".
    """
    def fix(m):
        w = m.group(0)
        if w.upper() in SPOKEN_ACRONYMS:
            return w
        if any(ch.isdigit() for ch in w):
            return w
        return w.capitalize()

    return re.sub(r"\b[A-Z][A-Z'’]{1,}\b", fix, t)


def clean_for_speech(t):
    if not t:
        return ""
    t = re.sub(r"https?://\S+", " ", t)
    t = re.sub(r"www\.\S+", " ", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)
    t = re.sub(r"```.*?```", " ", t, flags=re.DOTALL)
    t = EMOJI_RE.sub(" ", t)
    t = soften_caps(t)
    # The voice reads these literally ("slash", "percent sign"), so spell them out.
    t = re.sub(r"(\d)\s*/\s*(\d)", r"\1 out of \2", t)
    t = re.sub(r"\s*/\s*", " or ", t)
    t = re.sub(r"(\d)\s*%", r"\1 percent", t)
    t = t.replace("%", " percent")
    t = re.sub(r"\$\s*([\d][\d,.]*)\s*(billion|million|trillion|thousand|bn|m|k)\b",
               r"\1 \2 dollars", t, flags=re.IGNORECASE)
    t = re.sub(r"\$\s*([\d][\d,.]*)", r"\1 dollars", t)
    t = re.sub(r"(?<![\w.])-\s*(\d)", r"minus \1", t)
    t = re.sub(r"\s*\+\s*(\d)", r" plus \1", t)
    t = re.sub(r"\s*\+\s*", " plus ", t)
    t = re.sub(r"\s*=\s*", " equals ", t)
    t = t.replace("(", " ").replace(")", " ").replace("[", " ").replace("]", " ")
    t = t.replace("–", ", ").replace("—", ". ").replace(" - ", ". ")
    for o, n in {"*": "", "#": "", "_": "", "`": "", "\"": "", ":": ", ", ";": ", ", "&": " and"}.items():
        t = t.replace(o, n)
    t = re.sub(r"\s+", " ", t).strip()
    if t and t[-1] not in ".!?":
        t += "."
    t = re.sub(r"\.{2,}", ".", t)
    return t


def clamp_half(v):
    """One rubric dimension, scored out of fifty."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        v = 25.0
    return max(0.0, min(50.0, v))


def clamp_score(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        v = 50.0
    return max(0.0, min(100.0, v))


def load_font(sz, bold=False):
    p = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    try:
        return ImageFont.truetype(p, sz)
    except OSError:
        return ImageFont.load_default()


def hex_to_rgba(h, a):
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), a)


def openrouter_headers():
    return {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://openrouter.ai/",
        "X-Title": CHANNEL_NAME,
    }


def discover_models():
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY missing")
    try:
        r = requests.get(OPENROUTER_MODELS_URL, headers=openrouter_headers(), timeout=20)
        free = []
        for it in r.json().get("data", []):
            mid = it.get("id", "")
            if not mid:
                continue
            if not USE_PAID_MODELS and ":free" not in mid.lower():
                continue
            if any(x in mid.lower() for x in ["embed", "tts", "whisper", "audio"]):
                continue
            if is_reasoning_model(mid):
                continue
            if mid.split("/", 1)[0].lower() not in PANEL_PROVIDERS:
                continue
            free.append(mid)
        if free:
            return list(dict.fromkeys(free))
        return FALLBACK_MODELS.copy()
    except Exception:
        return FALLBACK_MODELS.copy()


def query_openrouter(prompt, mid, timeout=60, max_tokens=800, temperature=0.82,
                     system=None, min_chars=60):
    if not OPENROUTER_API_KEY:
        return None
    if not mid:
        return None
    if not USE_PAID_MODELS and ":free" not in mid.lower():
        return None
    payload = {
        "model": mid,
        "messages": [
            {"role": "system", "content": system or SPEECH_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    for _ in range(2):
        try:
            resp = requests.post(OPENROUTER_URL, headers=openrouter_headers(), json=payload, timeout=timeout)
            if resp.status_code == 200:
                c = resp.json().get("choices", [])[0].get("message", {}).get("content", "")
                # min_chars guards against a truncated prose turn. JSON replies
                # are legitimately short, so callers asking for JSON lower it.
                if c and len(c.strip()) >= min_chars:
                    return c.strip()
        except Exception:
            pass
        time.sleep(1)
    return None


def extract_json_object(text):
    """Pull the first balanced {...} block out of a model reply."""
    if not text:
        return None
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    blob = text[start:i + 1]
                    for candidate in (blob, blob.replace("'", '"')):
                        try:
                            return json.loads(candidate)
                        except (ValueError, TypeError):
                            continue
                    break
        start = text.find("{", start + 1)
    return None


def choose_primary_models(avail):
    free = [m for m in avail if USE_PAID_MODELS or ":free" in m] or FALLBACK_MODELS
    used = set()
    picks = []
    for m in free:
        prov = provider_from_model(m)
        if prov not in used:
            picks.append(m)
            used.add(prov)
        if len(picks) >= 2:
            break
    if len(picks) < 2:
        picks = (free + FALLBACK_MODELS)[:2]
    return picks[0], picks[1]


def choose_judges(avail, primary):
    global JUDGE_VOICE_MAP
    primary_providers = set(provider_from_model(m) for m in primary)
    excl = set(primary)
    top_providers = set(PANEL_PROVIDERS)
    cands = [m for m in avail if m not in excl
             and (USE_PAID_MODELS or ":free" in m)
             and m.split("/")[0].lower() in top_providers
             and provider_from_model(m) not in primary_providers]
    if len(cands) < 4:
        cands = [m for m in avail if m not in excl
                 and (USE_PAID_MODELS or ":free" in m)
                 and provider_from_model(m) not in primary_providers]
    groups = {}
    for m in cands:
        prov = provider_from_model(m)
        if prov not in groups:
            groups[prov] = m
    order = [PROVIDER_ALIASES[p] for p in PANEL_PROVIDERS if p in PROVIDER_ALIASES]
    sel = []
    for name in order:
        if name in groups:
            sel.append(groups.pop(name))
        if len(sel) >= MAX_JUDGES:
            break
    for m in list(groups.values()):
        if len(sel) >= MAX_JUDGES:
            break
        sel.append(m)
    seen_prov, seen_disp, uniq = set(), set(), []
    for m in sel:
        prov, disp = provider_from_model(m), get_judge_short_name(m)
        if prov in seen_prov or disp in seen_disp:
            continue
        uniq.append(m)
        seen_prov.add(prov)
        seen_disp.add(disp)
    result = uniq[:MAX_JUDGES]
    if len(result) < MAX_JUDGES:
        for m in FALLBACK_MODELS:
            if len(result) >= MAX_JUDGES:
                break
            prov, disp = provider_from_model(m), get_judge_short_name(m)
            if prov in seen_prov or disp in seen_disp or m in primary:
                continue
            result.append(m)
            seen_prov.add(prov)
            seen_disp.add(disp)
    JUDGE_VOICE_MAP = {mid: idx % len(JUDGE_VOICES) for idx, mid in enumerate(result)}
    return result


# ----------------------------------------------------------------------------
# Topic -> sides. Nothing below is tied to any particular subject.
# ----------------------------------------------------------------------------

QUESTION_OPENERS = {"is", "are", "was", "were", "do", "does", "did", "should",
                    "can", "could", "will", "would", "has", "have", "must", "shall"}

STOP_WORDS = {"a", "an", "the", "is", "are", "was", "were", "be", "been", "do", "does",
              "did", "should", "would", "could", "can", "will", "shall", "must", "has",
              "have", "had", "there", "really", "actually", "ever"}


LABEL_DISPLAY_CHARS = 18


def shorten_labels(a, b):
    """Drop shared opening words, but only when the labels need it.

    A model asked to name the sides often answers "CHRISTIANITY IS TRUE" and
    "CHRISTIANITY IS FALSE". Cut to fit a table column both read "CHRISTIANITY
    I", which tells a viewer nothing. Removing the shared opening leaves TRUE
    and FALSE. Labels that already read differently are left alone.
    """
    too_long = max(len(a), len(b)) > LABEL_DISPLAY_CHARS
    looks_same = a[:LABEL_DISPLAY_CHARS] == b[:LABEL_DISPLAY_CHARS]
    if not (too_long or looks_same):
        return a, b

    aw, bw = a.split(), b.split()
    while len(aw) > 1 and len(bw) > 1 and aw[0] == bw[0]:
        aw, bw = aw[1:], bw[1:]
    na, nb = " ".join(aw), " ".join(bw)
    if not na or not nb or na == nb:
        return a, b
    if na in ("IS", "IS NOT", "NOT") or nb in ("IS", "IS NOT", "NOT"):
        return a, b
    return na, nb


def _titlecase_label(s, limit=18):
    s = re.sub(r"[^\w\s'-]", " ", s or "").strip()
    s = re.sub(r"\s+", " ", s)
    if not s:
        return ""
    if len(s) > limit:
        s = s[:limit].rsplit(" ", 1)[0]
    return s.upper()


def fallback_roles(topic):
    """Derive two opposing side labels from the wording of the topic itself."""
    raw = (topic or "").strip().rstrip("?.!")
    low = raw.lower()

    m = re.match(r"^(does|do|is|are|was|were)\s+(.+?)\s+(?:exists?|real)$", low)
    if m:
        aux = m.group(1)
        subject = _titlecase_label(m.group(2))
        plural = aux in ("do", "are", "were")
        if subject:
            verb = "EXIST" if plural else "EXISTS"
            neg = "DON'T EXIST" if plural else "DOESN'T EXIST"
            does = "do" if plural else "does"
            return {
                "side_a_label": f"{subject} {verb}",
                "side_a_stance": f"{subject.title()} really {does} exist",
                "side_b_label": f"{subject} {neg}",
                "side_b_stance": f"{subject.title()} {does} not exist",
            }

    for sep in [" versus ", " vs. ", " vs ", " or "]:
        if sep not in low:
            continue
        left, right = low.split(sep, 1)
        left_words, right_words = left.split(), right.split()

        # "Is Christianity true or false" splits into "is christianity true"
        # and "false". Drop the question word and keep only as many trailing
        # words as the other side has, so the two labels match: TRUE / FALSE.
        if left_words and left_words[0] in QUESTION_OPENERS:
            left_words = left_words[1:]
        if len(left_words) > len(right_words):
            tail = left_words[-len(right_words):]
            if tail and not any(w in STOP_WORDS for w in tail):
                left_words = tail

        la, lb = _titlecase_label(" ".join(left_words)), _titlecase_label(right)
        if la and lb and la != lb:
            return {
                "side_a_label": la,
                "side_a_stance": f"the answer to this question is {la.lower()}",
                "side_b_label": lb,
                "side_b_stance": f"the answer to this question is {lb.lower()}",
            }

    if re.match(r"^(should|is|are|was|were|does|do|did|can|could|will|would|has|have|must|shall)\b", low):
        return {
            "side_a_label": "YES",
            "side_a_stance": f"the answer to \"{raw}\" is yes",
            "side_b_label": "NO",
            "side_b_stance": f"the answer to \"{raw}\" is no",
        }

    return {
        "side_a_label": "FOR",
        "side_a_stance": f"you support the motion \"{raw}\"",
        "side_b_label": "AGAINST",
        "side_b_stance": f"you oppose the motion \"{raw}\"",
    }


def get_debate_roles(topic, model):
    """Ask a model to name the two sides; fall back to grammar-based labels."""
    prompt = (
        f'A televised debate is being staged on this question or motion: "{topic}".\n'
        "Name the two opposing sides.\n"
        "Return ONLY JSON, no other text:\n"
        '{"side_a_label":"...","side_a_stance":"...","side_b_label":"...","side_b_stance":"..."}\n'
        "side_a_label and side_b_label: the position itself in at most three words, all caps, "
        "readable on a name card (for example GOD EXISTS, YES, BAN IT, FREE WILL). They must be "
        "genuine opposites and must not be identical.\n"
        "side_a_stance and side_b_stance: one short clause completing the sentence "
        "\"You believe that ...\", written for the debater on that side."
    )
    for m in [model] + FALLBACK_MODELS[:3]:
        resp = query_openrouter(prompt, m, timeout=35, max_tokens=250, temperature=0.3,
                                system="You return only valid JSON. No commentary.",
                                min_chars=2)
        d = extract_json_object(resp)
        if not d:
            continue
        la = _titlecase_label(str(d.get("side_a_label", "")))
        lb = _titlecase_label(str(d.get("side_b_label", "")))
        if not la or not lb or la == lb:
            continue
        la, lb = shorten_labels(la, lb)
        return {
            "side_a_label": la,
            "side_a_stance": str(d.get("side_a_stance", "")).strip() or f"{la.title()} is correct",
            "side_b_label": lb,
            "side_b_stance": str(d.get("side_b_stance", "")).strip() or f"{lb.title()} is correct",
        }
    return fallback_roles(topic)


# ----------------------------------------------------------------------------
# Turn generation
# ----------------------------------------------------------------------------

# A sentence matching any of these is scaffolding, not speech, and is dropped whole.
META_SENTENCE_PATTERNS = [
    r"^i\s*(?:'ll|will|am going to|'m going to|need to|want to|have to|must|should|shall|"
    r"would like to|plan to|intend to)\s+(?:first|now|also|then|briefly|quickly)?\s*"
    r"(?:discuss|address|talk about|argue against|respond|reply|counter|rebut|start by|"
    r"begin by|open with|point out|highlight|emphasi[sz]e|focus on|tackle|cover|lay out|"
    r"set out|establish|demonstrate|turn to|move on|outline|structure|walk (?:you )?through|"
    r"break (?:this|it) down|summari[sz]e|conclude|unpack)\b",
    r"^let me\s+(?:think|start|begin|first|break|unpack|walk|structure|outline|recap|"
    r"summari[sz]e|address|respond|counter|rebut|lay out|set out|explain my|clarify my|"
    r"restate|reframe)\b",
    r"^(?:my|the|this)\s+(?:\w+\s+)?(?:thinking process|process|structure|outline|"
    r"approach|strategy|framework|breakdown)\s+(?:is|will|goes|breaks|looks|here)\b",
    r"^(?:my|the|this)\s+(?:argument|response|answer|point)s?\s+"
    r"(?:structure|plan|outline|process|breakdown)\b",
    r"^here(?:'s| is)\s+(?:my|a|the|how|what)\b",
    r"^(?:in|for)\s+this\s+(?:round|turn|response|rebuttal|segment|answer|opening)\b",
    r"^(?:as|being)\s+the\s+\w+\s+(?:side|speaker|debater|position)\b",
    r"^(?:thinking process|constraints?|relevant points?|word count|task|instructions?|"
    r"position|note to self|draft)\b",
    r"^(?:i\s+)?(?:need|want|have)\s+to\s+(?:make sure|remember|keep|stay|hit|reach)\b",
    # The model describing the job it was given rather than doing it:
    # "I should score this as a person would", "I need to sound natural".
    r"^i\s*(?:'ll|will|should|need to|must|have to|want to|am going to|'m going to)\s+"
    r"(?:now\s+|just\s+|also\s+)?"
    r"(?:score|judge|rate|grade|evaluate|assess|weigh|mark|react|sound|come across|"
    r"speak|talk|write|phrase|word|keep|avoid|remember|ensure|make sure|be careful|"
    r"stay|stick|aim|try to)\b",
    r"^(?:my|the)\s+(?:job|task|role|brief|instruction|goal)\s+(?:here\s+)?is\b",
    r"^(?:the\s+)?(?:prompt|instructions?|brief|user|question)\s+(?:says|asks|wants|"
    r"tells|requires)\b",
    r"^(?:i\s+)?(?:should|need to|must)\s+(?:sound|seem|appear|come across)\b",
]

# The model weighing its own options out loud: "Option 2. Maybe I could use the
# 2008 crash, but that's too technical." These are dropped AND counted, because
# a response full of them is deliberation, not an answer.
DELIBERATION_PATTERNS = [
    r"^options?\s*\d*\s*[:.\-]",
    r"^option\s+(?:one|two|three|a|b|c)\b",
    r"^(?:i|we)\s+(?:have|see|could go with|am considering)\s+(?:a few|some|two|three|several)\s+"
    r"(?:options|ideas|angles|choices|possibilities|directions)\b",
    r"^(?:maybe|perhaps|possibly)\s+(?:i|we)\s+(?:could|should|can|might|ought to)\b",
    r"^(?:i|we)\s+(?:could|might|can)\s+(?:say|use|mention|cite|go with|talk about|start with|"
    r"try|pick|choose|open with|lead with|bring up)\b",
    r"^(?:but|and|though|however)?\s*(?:that|this|it)\s*(?:'s|s\b| is| was| would be| might be|"
    r"| may be| sounds| seems| feels| gets)\s+(?:\w+\s+){0,3}?(?:too|overly|a bit|a little|"
    r"kind of|sort of|rather)\s+(?:technical|scientific|academic|scholarly|niche|obscure|"
    r"complex|complicated|convoluted|dry|dense|nerdy|wonky|jargon\w*|abstract|theoretical|"
    r"wordy|verbose|long|short|formal|informal|casual|stiff|aggressive|harsh|weak|generic|"
    r"vague|bland|obvious|on the nose|cliche\w*|preachy|dark|heavy|controversial|much)\b",
    r"^(?:hmm+|wait|hold on|actually,? no|on second thought|scratch that|let'?s see|"
    r"let me see|okay let'?s|alright let'?s)\b",
    r"^alternatively\b",
    r"^(?:i|we)\s+(?:should|need to|must|want to|ought to)\s+(?:avoid|steer clear|not use|"
    r"stay away|keep away)\b",
    r"^(?:that|this|the (?:first|second|third|last) one)\s+"
    r"(?:works|is good|is better|is best|sounds good|is fine|is stronger|feels right)\b",
    r"^(?:another|one more|a better|a different)\s+(?:option|idea|angle|approach|choice|way)\b",
    r"^(?:for|as)\s+(?:the|my|an?)\s+(?:evidence|example|statistic|number|point|hook|opener),?\s+"
    r"(?:i|we)\s+(?:could|might|should|will)\b",
    r"^(?:i|we)\s*(?:'ll|will)\s+(?:go|run|stick)\s+with\b",
    r"^(?:that|this)\s+(?:covers|hits|checks)\s+(?:the|all|both)\b",
    r"^(?:word count|length)\s*[:\-]",
    r"^(?:good|great|perfect|nice|okay|ok)[.,!]?\s*(?:that|this|now)\b.*"
    r"(?:next|move on|now for|then i)\b",
]

# Planning verbs that only look like scaffolding in a short, clipped sentence.
PLANNING_STUB = re.compile(
    r"^(?:analy[sz]e|identify|draft|outline|brainstorm|structure|plan|review|check|"
    r"restate|paraphrase|summari[sz]e)\b", re.IGNORECASE)

# Openers to shave off the front of an otherwise good sentence.
LEADING_NOISE = [
    r"^(?:okay|ok|alright|sure|certainly|of course)\b[,.!]?\s+(?:so\b[,]?\s+)?",
    r"^(?:ladies and gentlemen|my friends|good evening|good morning|good afternoon|"
    r"thank you|thanks|hello|hi there)\b[,.!]?\s*",
    r"^(?:rebuttal|counterpoint|counter-argument|counter argument|opening|closing|response|"
    r"new (?:point|argument|evidence)|point \d+|argument \d+|part \d+|first point|"
    r"second point|final point)\s*[:\-\u2013]\s*",
]

# Connectives that can sit in front of scaffolding and hide it from the patterns above.
LEADING_CONNECTIVE = re.compile(
    r"^(?:and|but|so|then|now|next|first(?:ly)?|second(?:ly)?|third(?:ly)?|finally|lastly|"
    r"also|additionally|furthermore|moreover|well|right|look|listen|okay|ok|alright|"
    r"sure|certainly|honestly|frankly)\b[,]?\s+", re.IGNORECASE)

LEAK_FRAGMENTS = [
    "thinking process", "here's a thinking", "as an ai", "as a language model",
    "step 1", "word count", "user constraints", "the user wants", "the prompt",
    "my instructions", "i cannot", "i'm unable to",
    # Describing the performance instead of performing it.
    "i should score", "i should judge", "i should rate", "i should sound",
    "i need to sound", "i should react", "i should speak", "i should talk",
    "sound like a person", "sound like a human", "sound natural", "sound human",
    "as a real person", "like a real person", "in a human way", "human sounding",
    "one spoken sentence", "two or three short sentences", "in plain english",
    "without being robotic", "not too robotic", "conversational tone",
    "as a youtuber", "like a youtuber", "my persona", "stay in character",
]


def _is_deliberation_sentence(sentence):
    """True when the sentence is the model weighing options rather than arguing."""
    s = sentence.strip()
    for _ in range(3):
        stripped = LEADING_CONNECTIVE.sub("", s, count=1).strip()
        if stripped == s:
            break
        s = stripped
    low = s.lower()
    return any(re.match(pat, low, flags=re.IGNORECASE) for pat in DELIBERATION_PATTERNS)


def _is_meta_sentence(sentence):
    """True when the sentence is the model narrating its own process."""
    s = sentence.strip()
    for _ in range(3):
        stripped = LEADING_CONNECTIVE.sub("", s, count=1).strip()
        if stripped == s:
            break
        s = stripped
    low = s.lower()
    for pat in META_SENTENCE_PATTERNS:
        if re.match(pat, low, flags=re.IGNORECASE):
            return True
    if PLANNING_STUB.match(low) and count_words(s) <= 8:
        return True
    if re.search(r"\b(?:is|are|goes)\s+as follows\b", low):
        return True
    return False


# Pictographs and dingbats: DejaVu has no glyphs for these, so anything that
# survives into a subtitle renders as a blank rectangle. They are also read
# aloud by the voice ("sparkles"), so they come out of the spoken text too.
EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"
    "\U0001F1E6-\U0001F1FF"
    "\U00002190-\U000021FF"
    "\U00002300-\U000023FF"
    "\U00002460-\U000024FF"
    "\U000025A0-\U000027BF"
    "\U00002B00-\U00002BFF"
    "\U00002600-\U000026FF"
    "\U0000FE00-\U0000FE0F"
    "\U0000200D"
    "]+"
)

# Reasoning models wrap their scratchpad in these before answering.
REASONING_BLOCK_RE = re.compile(
    r"<\s*(?:think|thinking|thought|scratchpad|reasoning)\s*>.*?"
    r"<\s*/\s*(?:think|thinking|thought|scratchpad|reasoning)\s*>",
    re.DOTALL | re.IGNORECASE,
)
# An unclosed opener means everything after it is scratchpad.
REASONING_OPEN_RE = re.compile(
    r"<\s*(?:think|thinking|thought|scratchpad|reasoning)\s*>.*",
    re.DOTALL | re.IGNORECASE,
)


def clean_response(text):
    """Strip scaffolding and deliberation from a model reply.

    Returns (spoken_text, deliberation_count, sentence_count) so the caller can
    reject a reply that was mostly the model thinking out loud.
    """
    if not text:
        return "", 0, 0
    t = REASONING_BLOCK_RE.sub(" ", text)
    t = REASONING_OPEN_RE.sub(" ", t)
    t = re.sub(r"```.*?```", " ", t, flags=re.DOTALL)
    t = EMOJI_RE.sub(" ", t)
    t = re.sub(r"^\s*#{1,6}\s*", "", t, flags=re.MULTILINE)
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t)
    t = re.sub(r"^\s*(?:[-*\u2022]|\d+[.)])\s+", "", t, flags=re.MULTILINE)
    t = re.sub(r"\(\s*(?:about|approx\.?|roughly)?\s*\d+\s*words?\s*\)", " ", t, flags=re.IGNORECASE)
    t = re.sub(r"\s+", " ", t).strip()

    kept = []
    deliberation = 0
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", t) if s.strip()]
    for sentence in sentences:
        s = sentence.strip()
        low = s.lower()
        if any(frag in low for frag in LEAK_FRAGMENTS):
            deliberation += 1
            continue

        # Shave openers, then re-test: "Okay, so I need to discuss X" must still die.
        trimmed = s
        for _ in range(3):
            before = trimmed
            for pat in LEADING_NOISE:
                trimmed = re.sub(pat, "", trimmed, count=1, flags=re.IGNORECASE).strip()
            if trimmed == before:
                break
        if not trimmed:
            continue
        # Orphaned list markers ("1.", "2.") survive sentence splitting; drop them.
        if count_words(trimmed) < 2 or not re.search(r"[A-Za-z]{2}", trimmed):
            continue
        if _is_deliberation_sentence(trimmed):
            deliberation += 1
            continue
        if _is_meta_sentence(trimmed):
            deliberation += 1
            continue
        if trimmed and trimmed[0].islower():
            trimmed = trimmed[0].upper() + trimmed[1:]
        kept.append(trimmed)

    out = " ".join(kept).strip()
    out = re.sub(r"\s+([,.!?;])", r"\1", out)
    out = re.sub(r"\s+", " ", out).strip()
    if out and out[-1] not in ".!?":
        out += "."
    return out, deliberation, len(sentences)


def is_deliberating(deliberation, sentence_count):
    """A reply that is mostly the model weighing options is not usable."""
    if deliberation >= 4:
        return True
    return sentence_count > 0 and deliberation / sentence_count > 0.34


def strip_meta(text):
    """Spoken text only, with the scaffolding and deliberation removed."""
    return clean_response(text)[0]


# The register the debaters must hit. This is the difference between an argument
# that lands with a general audience and one that reads like a seminar paper.
PLAIN_SPEECH_RULES = (
    "How to say it:\n"
    "- Talk the way you would to a friend in a pub who is smart but knows nothing about "
    "this subject. Not a lecture, not an essay, not a press release.\n"
    "- Short sentences. Mix a long one in occasionally so it does not get choppy, but "
    "most should be under fifteen words.\n"
    "- Everyday words only. If you must use a technical term, explain it in the same "
    "breath in words a fourteen year old would follow.\n"
    "- Use pictures people can see: a kid at two in the morning, a bill on a kitchen "
    "table, a queue outside a hospital. Not abstractions.\n"
    "- One number, said simply. Nine out of ten, not 89.4 percent. A number a listener "
    "can hold in their head.\n"
    "- Contractions throughout. Don't, isn't, they're, that's.\n"
    "- Never use these words: furthermore, moreover, consequently, thus, hence, "
    "nevertheless, utilise, facilitate, paradigm, framework, empirical, methodology, "
    "nuanced, multifaceted, underscores, salient, myriad, plethora, aforementioned.\n"
    "- Keep the argument just as strong. Simple words, hard punches. Plain speech is not "
    "a weaker case, it is the same case that more people can follow."
)

# Words that make a spoken argument sound like a paper being read out.
ACADEMIC_MARKERS = {
    "furthermore", "moreover", "consequently", "nevertheless", "notwithstanding",
    "thus", "hence", "whereby", "wherein", "aforementioned", "utilize", "utilise",
    "facilitate", "paradigm", "framework", "empirical", "efficacy", "methodology",
    "fundamentally", "inherently", "substantive", "multifaceted", "nuanced",
    "underscores", "underscore", "posits", "postulates", "asserts", "delineate",
    "dichotomy", "ontological", "epistemic", "normative", "salient", "pertinent",
    "requisite", "myriad", "plethora", "predicated", "contingent", "requisite",
    "insofar", "vis-a-vis", "heretofore", "thereby", "therein", "albeit",
}

# Straight swaps that change register without changing meaning.
PLAIN_SWAPS = {
    "utilize": "use", "utilise": "use", "commence": "start", "endeavour": "try",
    "endeavor": "try", "numerous": "many", "furthermore": "and", "moreover": "and",
    "consequently": "so", "therefore": "so", "thus": "so", "hence": "so",
    "nevertheless": "even so", "notwithstanding": "even so", "facilitate": "help",
    "ascertain": "find out", "demonstrates": "shows", "demonstrate": "show",
    "indicates": "shows", "indicate": "show", "sufficient": "enough",
    "additional": "more", "approximately": "about", "individuals": "people",
    "purchase": "buy", "obtain": "get", "requires": "needs", "require": "need",
    "assist": "help", "attempt": "try", "initiate": "start", "terminate": "end",
    "subsequently": "then", "prior to": "before", "in order to": "to",
    "with regard to": "about", "in the event that": "if", "a myriad of": "many",
    "a plethora of": "plenty of", "the majority of": "most",
    "substantial": "big", "significant": "big", "considerable": "big",
}


def plain_words(text):
    """Swap paper words for spoken ones, keeping the sentence's capitalisation."""
    def swap(m):
        word = m.group(0)
        repl = PLAIN_SWAPS[word.lower()]
        return repl.capitalize() if word[0].isupper() else repl

    for phrase in sorted(PLAIN_SWAPS, key=len, reverse=True):
        text = re.sub(rf"\b{re.escape(phrase)}\b", swap, text, flags=re.IGNORECASE)
    return text


def reads_academic(text):
    """True when a turn reads like an essay rather than someone talking."""
    words = re.findall(r"\b[\w'-]+\b", text.lower())
    if len(words) < 30:
        return False
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    avg_sentence = len(words) / max(1, len(sentences))
    jargon = sum(1 for w in words if w in ACADEMIC_MARKERS)
    long_words = sum(1 for w in words if len(w) >= 13)
    return (avg_sentence > 27
            or jargon >= 2
            or (jargon + long_words) / len(words) > 0.06)


def generate_turn(topic, roles, side, round_num, turn_num, opponent_last, target_words, model):
    label = roles["side_a_label"] if side == "A" else roles["side_b_label"]
    other = roles["side_b_label"] if side == "A" else roles["side_a_label"]
    stance = roles["side_a_stance"] if side == "A" else roles["side_b_stance"]
    used_str = "; ".join(list(USED_ARGUMENTS)[-6:])[:400] if USED_ARGUMENTS else "nothing yet"

    if not opponent_last:
        prompt = (
            f"The debate question is: {topic}\n"
            f"You are arguing the {label} side. You believe that {stance}.\n\n"
            "This is your opening. Say the one thing that most convinces you, and make it "
            "real: a specific case, a number, something that actually happened, with enough "
            "detail that someone could go and check it.\n\n"
            + PLAIN_SPEECH_RULES
            + f"\nSpeak for roughly {target_words} words. Start with the substance in your "
            "very first sentence. Do not greet anyone, do not name your side, do not "
            "describe what you are about to say, and do not reason about which example to "
            "pick. Pick one and say it as if you always meant to."
        )
    else:
        prompt = (
            f"The debate question is: {topic}\n"
            f"You are arguing the {label} side. You believe that {stance}.\n\n"
            f"Your opponent on the {other} side just finished saying this:\n"
            f"\"{opponent_last[:1200]}\"\n\n"
            "Answer them the way a real debater does, as one flowing spoken paragraph with no "
            "headings and no numbering:\n"
            "- Open by repeating back a few of their own words, the specific claim they made, and "
            "say straight away why it doesn't hold.\n"
            "- Give the counter-evidence: a specific case, figure or example that cuts against it.\n"
            "- Then push forward with one fresh reason for your own side that hasn't come up yet.\n\n"
            f"Points already used in this debate, so find something new: {used_str}\n\n"
            + PLAIN_SPEECH_RULES
            + f"\nSpeak for roughly {target_words} words. Your first sentence must already be "
            "engaging what they said. Never announce that you are about to respond, counter, "
            "address or discuss anything, and never weigh options out loud. Choose your "
            "evidence silently and say it like you mean it."
        )

    attempted = []
    best = ""
    best_model = None
    stiff = ""          # usable, but reads like an essay
    stiff_model = None
    for m in turn_model_chain(model):
        attempted.append(get_judge_short_name(m))
        resp = query_openrouter(prompt, m, max_tokens=900,
                                temperature=0.84 + random.random() * 0.08)
        if not resp:
            continue
        cleaned, deliberation, sentence_count = clean_response(resp)
        if is_deliberating(deliberation, sentence_count):
            # The model talked itself through the answer instead of giving it.
            continue
        low = cleaned.lower()
        if any(frag in low for frag in LEAK_FRAGMENTS):
            continue
        if count_words(cleaned) < MIN_ACCEPTABLE_TURN_WORDS:
            continue

        repeated = False
        for used in USED_ARGUMENTS:
            if len(used) > 40 and used.lower() in low:
                repeated = True
                break
        if repeated:
            continue

        # Short of the pacing target but genuine: hold it, and keep looking for
        # a fuller one rather than discarding real content.
        if count_words(cleaned) < target_words - 40:
            if count_words(cleaned) > count_words(best):
                best, best_model = cleaned, m
            continue

        cleaned = plain_words(cleaned)
        # An essay-sounding turn is real content but poor television. Hold it
        # and try another model before settling for it.
        if reads_academic(cleaned) and not stiff:
            stiff, stiff_model = cleaned, m
            continue

        cleaned = trim_to_words(cleaned, target_words + 25)
        for s in re.split(r"(?<=[.!?])\s+", cleaned)[:3]:
            if len(s) > 40:
                USED_ARGUMENTS.add(s[:90])
        return cleaned, m

    if stiff and count_words(stiff) >= count_words(best):
        print(f"    {label} turn reads formally and no model did better; using it.")
        stiff = trim_to_words(stiff, target_words + 25)
        for s in re.split(r"(?<=[.!?])\s+", stiff)[:3]:
            if len(s) > 40:
                USED_ARGUMENTS.add(s[:90])
        return stiff, stiff_model

    if best:
        best = plain_words(best)
        print(f"    {label} turn came in at {count_words(best)} words against a "
              f"{target_words} target; using it as spoken rather than padding.")
        for s in re.split(r"(?<=[.!?])\s+", best)[:3]:
            if len(s) > 40:
                USED_ARGUMENTS.add(s[:90])
        return best, best_model

    raise DebateGenerationError(
        f"No model produced a usable {label} turn for round {round_num}, turn {turn_num}. "
        f"Tried: {', '.join(attempted)}. Every reply was empty, too short, or was the model "
        f"reasoning out loud rather than arguing. Nothing was substituted - rerun, or widen "
        f"FALLBACK_MODELS."
    )


# ----------------------------------------------------------------------------
# Judging
# ----------------------------------------------------------------------------

JUDGE_TEXT_LIMIT = 6000
# A margin swing this large between the two orderings is reported as a warning
# in the log. It does not by itself disqualify a judge: what disqualifies a
# judge is actually changing its mind when the sides are swapped.
ORDER_GAP_WARN = 25.0


def fair_excerpts(a_text, b_text):
    """Cut both sides at the same length so neither is judged on less material."""
    cap = min(JUDGE_TEXT_LIMIT, max(len(a_text), len(b_text)))
    return a_text[:cap], b_text[:cap]


def _num(value):
    """A number out of whatever the model actually put in the field."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        m = re.search(r"-?\d+(?:\.\d+)?", value)
        if m:
            return float(m.group(0))
    return None


def _find_key(d, *names):
    """Look up a value by any of several spellings, at the top level or nested."""
    flat = {}

    def walk(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                key = re.sub(r"[^a-z0-9]", "", str(k).lower())
                if key not in flat:
                    flat[key] = v
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(d)
    for n in names:
        key = re.sub(r"[^a-z0-9]", "", n.lower())
        if key in flat:
            got = _num(flat[key])
            if got is not None:
                return got
    return None


def _scores_from_json(d):
    """Pull a pair of 0-100 scores out of a judge reply, whatever it called them.

    Models are inconsistent about field names even when told exactly what to
    return, so rather than insist on one spelling this accepts the shapes they
    actually produce: the two part rubric, a single total each, or a bare pair.
    """
    if not isinstance(d, dict):
        return None
    ev1 = _find_key(d, "one_evidence", "debater_one_evidence", "evidence_one", "1_evidence")
    rb1 = _find_key(d, "one_rebuttal", "debater_one_rebuttal", "rebuttal_one", "1_rebuttal")
    ev2 = _find_key(d, "two_evidence", "debater_two_evidence", "evidence_two", "2_evidence")
    rb2 = _find_key(d, "two_rebuttal", "debater_two_rebuttal", "rebuttal_two", "2_rebuttal")
    if None not in (ev1, rb1, ev2, rb2):
        return (clamp_score(clamp_half(ev1) + clamp_half(rb1)),
                clamp_score(clamp_half(ev2) + clamp_half(rb2)))

    one = _find_key(d, "one_total", "debater_one_total", "debater_one", "one", "total_one",
                    "score_one", "one_score", "a_total", "score_a")
    two = _find_key(d, "two_total", "debater_two_total", "debater_two", "two", "total_two",
                    "score_two", "two_score", "b_total", "score_b")
    if one is not None and two is not None:
        return clamp_score(one), clamp_score(two)
    return None


def _score_once(model, topic, rn, first_text, second_text, simple=False):
    """One anonymised scoring pass. Returns (first_score, second_score, reason)."""
    header = (
        f"You are judging round {rn} of a debate on this question: {topic}\n\n"
        f"Debater One said:\n{first_text}\n\n"
        f"Debater Two said:\n{second_text}\n\n"
    )
    if simple:
        # Fallback for models that cannot hold the two part rubric.
        prompt = header + (
            "Give each debater a score out of 100 for how well they argued. Judge the "
            "arguing, not whether you agree with the position. Longer is not better.\n"
            'Reply with nothing but this JSON: {"one_total": 0, "two_total": 0}'
        )
    else:
        prompt = header + (
            "Score each debater on two things separately.\n"
            "Evidence, 0 to 50: how specific, checkable and relevant their support was.\n"
            "Rebuttal, 0 to 50: how directly they engaged what the other debater said.\n"
            "Judge the argument as it was made. Do not score based on whether you agree "
            "with the position being defended, only on how well it was argued.\n"
            "Length is not quality. Do not reward a debater for saying more, only for "
            "saying something better supported.\n"
            'Return ONLY JSON: {"one_evidence": 0, "one_rebuttal": 0, "two_evidence": 0, '
            '"two_rebuttal": 0, '
            '"reason": "one spoken sentence naming the specific point that decided it"}'
        )
    resp = query_openrouter(prompt, model, timeout=40, max_tokens=320, temperature=0.0,
                            system="You return only valid JSON. No commentary.",
                            min_chars=2)
    if not resp:
        return None
    pair = _scores_from_json(extract_json_object(resp))
    if pair is None:
        # Last resort: the reply is prose but contains the two numbers.
        if simple:
            nums = re.findall(r"\b(\d{1,3}(?:\.\d+)?)\b", resp)
            vals = [float(n) for n in nums if 0 <= float(n) <= 100]
            if len(vals) >= 2:
                pair = (clamp_score(vals[0]), clamp_score(vals[1]))
    # Zero for both sides is a failed answer, not a considered draw. Counting
    # it put a 0.0 to 0.0 TIE on the scorecard as though it were a verdict.
    if pair is not None and pair[0] <= 0 and pair[1] <= 0:
        pair = None
    if pair is None:
        return None, resp[:160]
    d = extract_json_object(resp) or {}
    reason = ""
    if isinstance(d, dict):
        for k, v in d.items():
            if "reason" in str(k).lower() and isinstance(v, str):
                reason = v[:200]
                break
    return pair[0], pair[1], reason


def judge_round(model, topic, rn, ap, sk, roles):
    """Score a round with this judge, correcting for presentation bias.

    Language models favour whichever option they are shown first, so each judge
    scores the round twice with the two sides swapped, and the passes are
    averaged. The sides are also anonymised as Debater One and Two, so a judge
    grades the arguing rather than its own opinion of the position. Returns None
    if the judge produced nothing usable; no score is ever invented.
    """
    if is_reasoning_model(model):
        return None
    if not USE_PAID_MODELS and ":free" not in model:
        return None
    a_text, b_text = fair_excerpts(ap, sk)

    a_scores, b_scores, reasons = [], [], []
    last_raw = ""
    # Pass 1 shows A first, pass 2 shows B first. Averaging cancels the bias.
    # Each pass tries the rubric, then a simpler two number form for models
    # that cannot hold it.
    for a_first in (True, False):
        for simple in (False, True):
            first, second = (a_text, b_text) if a_first else (b_text, a_text)
            out = _score_once(model, topic, rn, first, second, simple=simple)
            if out and len(out) == 2:
                last_raw = out[1] or last_raw
                continue
            if out:
                if a_first:
                    a_scores.append(out[0])
                    b_scores.append(out[1])
                else:
                    b_scores.append(out[0])
                    a_scores.append(out[1])
                reasons.append(out[2])
                break

    if not a_scores or not b_scores:
        if last_raw:
            print(f"    {get_judge_short_name(model)} returned nothing scoreable. "
                  f"It said: {last_raw[:110]}")
        return None

    a = round(sum(a_scores) / len(a_scores), 1)
    b = round(sum(b_scores) / len(b_scores), 1)
    # How far the two orderings disagreed: a large gap means this judge's score
    # was driven by presentation order more than by the arguments.
    order_gap = 0.0
    if len(a_scores) == 2:
        order_gap = round(abs((a_scores[0] - b_scores[0]) - (a_scores[1] - b_scores[1])), 1)

    # The real test of a judge: did swapping the sides change who it picked?
    # A judge that reverses has scores but no verdict, so it abstains.
    reversed_verdict = False
    if len(a_scores) == 2:
        per_pass = set()
        for i in (0, 1):
            if a_scores[i] > b_scores[i]:
                per_pass.add("A")
            elif b_scores[i] > a_scores[i]:
                per_pass.add("B")
        reversed_verdict = {"A", "B"} <= per_pass

    if reversed_verdict:
        winner = "UNSTABLE"
    elif a > b:
        winner = "A"
    elif b > a:
        winner = "B"
    else:
        winner = "TIE"
    return {"model": model, "provider": provider_from_model(model),
            "display_name": get_judge_short_name(model),
            "A_total": a, "B_total": b, "winner": winner,
            "passes": len(a_scores), "order_gap": order_gap,
            "reason": next((r for r in reasons if r), "")}


def evaluate_round(judges, topic, rn, ap, sk, roles, recused=()):
    """Score a round with the judges that actually responded.

    The panel is never padded. A model that fails to return scores simply does
    not appear on the scorecard. Any model that wrote a turn this round is
    recused, so nothing scores its own text.
    """
    recused = set(recused)
    recused_providers = {provider_from_model(m) for m in recused}
    sitting = [m for m in judges
               if m not in recused and provider_from_model(m) not in recused_providers]
    for m in judges:
        if m not in sitting:
            print(f"    {get_judge_short_name(m)} is recused this round "
                  f"({provider_from_model(m)} wrote a turn).")
    if not sitting:
        raise DebateGenerationError(
            f"Every judge on the panel wrote a turn in round {rn}, so none can score it "
            f"impartially. Widen FALLBACK_MODELS so debaters and judges do not overlap."
        )

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(7, len(sitting))) as ex:
        futs = {ex.submit(judge_round, m, topic, rn, ap, sk, roles): m for m in sitting}
        for f in concurrent.futures.as_completed(futs):
            try:
                r = f.result()
            except Exception:
                r = None
            if r:
                results.append(r)

    missing = len(sitting) - len(results)
    if missing:
        print(f"    {missing} of {len(sitting)} sitting judges returned no score and were "
              f"left off the round {rn} scorecard.")
    if not results:
        # Omitting a score is not the same as inventing one. The round is
        # reported as unscored and the build carries on: the consensus poll is
        # a separate measurement and still stands.
        print(f"    No judge returned a usable score for round {rn}. The round is "
              f"recorded as unscored rather than given invented numbers.")
        return []
    results.sort(key=lambda r: r["display_name"])
    for r in results:
        note = "" if r["passes"] == 2 else f", {r['passes']} of 2 passes only"
        flag = "  <-- order sensitive" if r["order_gap"] >= ORDER_GAP_WARN else ""
        print(f"    judge {r['display_name']} [{r['model']}]: "
              f"{roles['side_a_label']} {r['A_total']:.1f}, "
              f"{roles['side_b_label']} {r['B_total']:.1f} -> {r['winner']}"
              f" (order gap {r['order_gap']:.1f}{note}){flag}")
    return results


def round_votes(res):
    """How the panel split, independent of each judge's private scoring scale.

    Judges whose verdict reversed when the sides were swapped are counted as
    abstentions, not as votes: an order driven result is not an opinion.
    """
    a = sum(1 for r in res if r["winner"] == "A")
    b = sum(1 for r in res if r["winner"] == "B")
    t = sum(1 for r in res if r["winner"] == "TIE")
    u = sum(1 for r in res if r["winner"] == "UNSTABLE")
    return a, b, t, u


def spoken_split(a, b, t, u, roles):
    parts = []
    if a:
        parts.append(f"{a} for {roles['side_a_label']}")
    if b:
        parts.append(f"{b} for {roles['side_b_label']}")
    if t:
        parts.append(f"{t} calling it a draw")
    if not parts:
        base = "nobody could pick a winner"
    elif len(parts) == 1:
        base = parts[0]
    else:
        base = ", ".join(parts[:-1]) + " and " + parts[-1]
    if u:
        base += f", and {u} thrown out for flip flopping"
    return base


def calculate_round_average(res):
    return (round(sum(r["A_total"] for r in res) / len(res), 2),
            round(sum(r["B_total"] for r in res) / len(res), 2))


def generate_panel_commentary(model, side, topic, rn, a_text, b_text, roles):
    """A judge who actually scored `side` ahead explains, briefly, why."""
    name = get_judge_short_name(model)
    winner = roles["side_a_label"] if side == "A" else roles["side_b_label"]
    loser = roles["side_b_label"] if side == "A" else roles["side_a_label"]
    win_text = a_text if side == "A" else b_text
    lose_text = b_text if side == "A" else a_text

    def tail(t, mw=220):
        wl = t.split()
        return t if len(wl) <= mw else " ".join(wl[-mw:])

    prompt = (
        f"You are {name}, one of the judges on a debate about: {topic}\n"
        f"You have just scored round {rn} and you had {winner} ahead of {loser}.\n\n"
        f"What {winner} said:\n{tail(win_text)}\n\n"
        f"What {loser} said:\n{tail(lose_text)}\n\n"
        "Say out loud, in two or three short sentences, why it went that way for you. Name the "
        "actual thing the winning side said, repeat a phrase of theirs back, and say the specific "
        "question the other side left sitting there unanswered.\n"
        f"About {COMMENTARY_WORDS} words. Talk like someone who just watched it and is "
        "telling a mate what swung it. Everyday words, short sentences, contractions. "
        "No jargon, no scores, no numbers, no round number, no preamble, no describing "
        "what you are doing, and no thinking out loud about what you might say. Just say it."
    )
    resp = query_openrouter(prompt, model, timeout=40, max_tokens=320, temperature=0.88)
    if not resp:
        return None
    cleaned, deliberation, sentence_count = clean_response(resp)
    if is_deliberating(deliberation, sentence_count):
        return None
    cleaned = plain_words(cleaned)
    if count_words(cleaned) < 18:
        return None
    # A reaction nobody can follow is worse than no reaction, and there are
    # other judges on the same side who can be asked instead.
    if reads_academic(cleaned):
        print(f"    {name} reacted in a stiff, essayish way; trying another juror.")
        return None
    if not re.search(r"[.!?]", cleaned) or count_words(cleaned) > COMMENTARY_WORDS + 60:
        return None
    key = cleaned.lower()[:80]
    if key in USED_JUDGE_EXPLANATIONS:
        return None
    USED_JUDGE_EXPLANATIONS.add(key)
    return trim_to_words(cleaned, COMMENTARY_WORDS + 30)


# ----------------------------------------------------------------------------
# Consensus poll
#
# The scorecards measure who argued better. This measures what the models
# actually think about the question, asked cold before the debate and again
# after they have read every word. The movement between the two is the result.
# ----------------------------------------------------------------------------

POLL_SCALE = 5          # positions run from -5 to +5
LEAN_THRESHOLD = 0.5    # inside this band a model counts as undecided


def _poll_once(model, topic, roles, transcript, flip):
    """Ask one model where it stands. `flip` reverses the meaning of the scale.

    Asking both ways and averaging cancels any pull toward the positive end of
    the scale, the same correction the judging uses for running order.
    """
    if flip:
        pos_label, neg_label = roles["side_b_label"], roles["side_a_label"]
    else:
        pos_label, neg_label = roles["side_a_label"], roles["side_b_label"]

    preamble = ""
    if transcript:
        preamble = (f"Here is the full transcript of a debate on this question.\n\n"
                    f"{transcript[:14000]}\n\n"
                    "Having read it, answer for yourself.\n\n")

    prompt = (
        f"{preamble}"
        f"Question: {topic}\n\n"
        f"Where do you personally stand? Give a whole number from -{POLL_SCALE} to "
        f"+{POLL_SCALE}, where +{POLL_SCALE} means you are confident that "
        f"{pos_label} is correct, -{POLL_SCALE} means you are confident that "
        f"{neg_label} is correct, and 0 means you genuinely have no lean either way.\n"
        "This is your own view, not a summary of what others think.\n"
        "If you are unwilling to take any position on this question, set position to null "
        "instead of picking a number. That is a legitimate answer and will be reported "
        "as such.\n"
        'Return ONLY JSON: {"position": 0, "confidence": 0, '
        '"comment": "one plain sentence saying why"}'
    )
    resp = query_openrouter(prompt, model, timeout=60, max_tokens=300, temperature=0.0,
                            system="You return only valid JSON. No commentary.",
                            min_chars=2)
    d = extract_json_object(resp)
    if d is None:
        return None
    if "position" not in d or d.get("position") is None:
        return {"declined": True, "position": None, "confidence": 0.0,
                "comment": str(d.get("comment", ""))[:200]}
    try:
        pos = float(d.get("position"))
    except (TypeError, ValueError):
        return None
    pos = max(-POLL_SCALE, min(POLL_SCALE, pos))
    if flip:
        pos = -pos     # back into side-A-positive terms
    try:
        conf = max(0.0, min(100.0, float(d.get("confidence", 0))))
    except (TypeError, ValueError):
        conf = 0.0
    return {"declined": False, "position": pos, "confidence": conf,
            "comment": str(d.get("comment", ""))[:200]}


def poll_panel(models, topic, roles, transcript=None):
    """Ask the whole panel where it stands, each model independently."""
    results = []

    def ask(model):
        answers = []
        declined = 0
        comment = ""
        for flip in (False, True):
            out = _poll_once(model, topic, roles, transcript, flip)
            if out is None:
                continue
            if out["declined"]:
                declined += 1
                comment = comment or out["comment"]
                continue
            answers.append(out)
            comment = comment or out["comment"]
        if not answers:
            if declined:
                return {"model": model, "provider": provider_from_model(model),
                        "display_name": get_judge_short_name(model),
                        "position": None, "declined": True,
                        "confidence": 0.0, "comment": comment}
            return None
        pos = sum(a["position"] for a in answers) / len(answers)
        conf = sum(a["confidence"] for a in answers) / len(answers)
        # Disagreement between the two scale directions is a sign the answer
        # was anchored on the scale rather than on the question.
        spread = 0.0
        if len(answers) == 2:
            spread = abs(answers[0]["position"] - answers[1]["position"])
        return {"model": model, "provider": provider_from_model(model),
                "display_name": get_judge_short_name(model),
                "position": round(pos, 2), "declined": False,
                "confidence": round(conf, 1), "spread": round(spread, 2),
                "comment": comment}

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, max(1, len(models)))) as ex:
        for f in concurrent.futures.as_completed([ex.submit(ask, m) for m in models]):
            try:
                r = f.result()
            except Exception:
                r = None
            if r:
                results.append(r)
    # Two models from one lab can share a short name; keep the board readable.
    tally = {}
    for r in results:
        tally[r["display_name"]] = tally.get(r["display_name"], 0) + 1
    for r in results:
        if tally[r["display_name"]] > 1:
            tail = r["model"].split("/")[-1].split(":")[0]
            r["display_name"] = f"{r['display_name']} ({tail[-12:]})"
    results.sort(key=lambda r: r["display_name"])
    return results


def build_poll_roster(debaters, panel):
    """Everyone whose opinion counts toward consensus.

    This is deliberately wider than the judging panel. A judge is excluded from
    scoring text it wrote, and a reasoning model is excluded from speaking
    because it narrates its scratchpad, but neither restriction has anything to
    do with a model stating its own view on the question. Both belong here.
    """
    # A model that just argued a side may be anchored by having argued it, so a
    # lab's seat goes to a model that did not debate wherever one exists.
    # A lab's single seat should go to its strongest available model, since that
    # is what best represents where that lab's system lands. POLL_EXTRA_MODELS is
    # the curated frontier list, so it is consulted first.
    debaters = list(debaters)
    preferred = list(POLL_EXTRA_MODELS) + list(panel)
    per_provider = {}
    roster = []
    for m in preferred + debaters:
        if not m or m in roster:
            continue
        prov = provider_from_model(m)
        if per_provider.get(prov, 0) >= MAX_POLL_PER_PROVIDER:
            continue
        per_provider[prov] = per_provider.get(prov, 0) + 1
        roster.append(m)
    return roster


def poll_movement(before, after):
    """How many individual models moved, and which way.

    More robust than the change in mean: it does not care what scale each model
    used, only which direction it went.
    """
    b = {r["model"]: r for r in before}
    toward_a = toward_b = unchanged = 0
    crossed = []
    for r in after:
        prev = b.get(r["model"])
        if not prev or prev["declined"] or r["declined"]:
            continue
        delta = r["position"] - prev["position"]
        if delta > 0.25:
            toward_a += 1
        elif delta < -0.25:
            toward_b += 1
        else:
            unchanged += 1
        if prev["position"] * r["position"] < 0:
            crossed.append(r["display_name"])
    return {"toward_a": toward_a, "toward_b": toward_b,
            "unchanged": unchanged, "crossed": crossed}


def poll_summary(results):
    """Counts and mean lean, aggregated one lab at a time.

    Each lab contributes a single position, the average of whatever models of
    its own answered. With one seat per lab this is the same as counting
    models; if the cap is ever raised it stops a lab with two seats carrying
    twice the weight of a lab with one.
    """
    declined = [r for r in results if r["declined"]]
    by_lab = {}
    for r in results:
        if r["declined"] or r["position"] is None:
            continue
        by_lab.setdefault(r["provider"], []).append(r["position"])
    stated = [{"position": sum(v) / len(v)} for v in by_lab.values()]
    lean_a = sum(1 for r in stated if r["position"] > LEAN_THRESHOLD)
    lean_b = sum(1 for r in stated if r["position"] < -LEAN_THRESHOLD)
    undecided = len(stated) - lean_a - lean_b
    positions = [r["position"] for r in stated]
    mean = round(sum(positions) / len(positions), 2) if positions else 0.0
    if len(positions) > 1:
        var = sum((p - mean) ** 2 for p in positions) / (len(positions) - 1)
        spread = round(var ** 0.5, 2)
    else:
        spread = 0.0
    return {"lean_a": lean_a, "lean_b": lean_b, "undecided": undecided,
            "declined": len(declined), "mean": mean, "stated": len(stated),
            "asked": len(results), "labs_answering": len(by_lab), "spread": spread,
            "low": round(min(positions), 1) if positions else 0.0,
            "high": round(max(positions), 1) if positions else 0.0,
            "providers": len({r["provider"] for r in results})}


def describe_poll(summary, roles):
    parts = []
    if summary["lean_a"]:
        parts.append(f"{summary['lean_a']} said {roles['side_a_label']}")
    if summary["lean_b"]:
        parts.append(f"{summary['lean_b']} said {roles['side_b_label']}")
    if summary["undecided"]:
        parts.append(f"{summary['undecided']} sat on the fence")
    if summary["declined"]:
        parts.append(f"{summary['declined']} would not pick a side")
    if not parts:
        return "nobody would answer"
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def build_opening_poll_narration(summary, roles, topic):
    if summary["lean_a"] > summary["lean_b"]:
        mood = f"So the room starts out leaning {roles['side_a_label']}"
    elif summary["lean_b"] > summary["lean_a"]:
        mood = f"So the room starts out leaning {roles['side_b_label']}"
    else:
        mood = "So the room starts out split down the middle"
    return (
        f"Before anyone argues anything, we asked {summary['asked']} different AIs, one from "
        f"each of {summary['providers']} companies, what they already think. {topic} "
        f"{sentence_case(describe_poll(summary, roles))}. "
        f"{mood}. Now our two debaters get the rest of this video to change their minds."
    )


def build_closing_poll_narration(before, after, roles, movement):
    """Lead on how many changed their mind. No scales, no averages."""
    moved = movement["toward_a"] + movement["toward_b"]
    total = moved + movement["unchanged"]

    if moved == 0:
        headline = ("not one of them budged. Two rounds of arguing, and every single AI "
                    "thinks exactly what it thought before")
    elif movement["toward_a"] and movement["toward_b"]:
        headline = (f"{movement['toward_a']} shifted toward {roles['side_a_label']} and "
                    f"{movement['toward_b']} shifted the other way, so the debate pushed "
                    f"them further apart rather than together")
    else:
        toward = roles["side_a_label"] if movement["toward_a"] else roles["side_b_label"]
        headline = (f"{moved} out of {total} shifted, and every one of them shifted "
                    f"toward {toward}")

    crossed = ""
    if movement["crossed"]:
        shown = movement["crossed"][:3]
        names = shown[0] if len(shown) == 1 else ", ".join(shown[:-1]) + " and " + shown[-1]
        crossed = (f" {names} did not just soften up, "
                   f"{'they' if len(shown) > 1 else 'it'} switched sides completely.")

    return (
        f"Now the bit that matters. We went back to the same AIs, this time with the whole "
        f"debate in front of them, and asked the same question again. "
        f"{sentence_case(headline)}.{crossed} "
        f"The final count: {describe_poll(after, roles)}. "
        f"Worth saying, this is what these AIs think, not proof of who is right. And they "
        f"are all built in fairly similar ways, so them agreeing is not the same as a room "
        f"full of independent people agreeing."
    )


# ----------------------------------------------------------------------------
# Visuals# ----------------------------------------------------------------------------
# Visuals
# ----------------------------------------------------------------------------

def create_background(pos, glow, fn):
    source = os.path.join(os.path.dirname(os.path.abspath(__file__)), "background.png")
    if os.path.exists(source):
        try:
            im = Image.open(source).convert("RGB").resize((VIDEO_W, VIDEO_H), Image.LANCZOS)
            im.save(fn)
            return
        except Exception:
            pass
    img = Image.new("RGBA", (VIDEO_W, VIDEO_H), (8, 10, 20, 255))
    draw = ImageDraw.Draw(img)
    for y in range(VIDEO_H):
        r = int(8 + 12 * y / VIDEO_H)
        g = int(10 + 18 * y / VIDEO_H)
        b = int(20 + 35 * y / VIDEO_H)
        draw.line([0, y, VIDEO_W, y], fill=(r, g, b, 255))
    cx = VIDEO_W * 0.22 if pos == "left" else VIDEO_W * 0.78 if pos == "right" else VIDEO_W * 0.5
    cy = VIDEO_H * 0.72
    for rad in range(200, 20, -20):
        alpha = int(12 * (1 - rad / 200))
        draw.ellipse([cx - rad, cy - rad, cx + rad, cy + rad],
                     fill=(*hex_to_rgba(glow, alpha)[:3], alpha))
    vignette = Image.new("RGBA", (VIDEO_W, VIDEO_H), (0, 0, 0, 0))
    vd = ImageDraw.Draw(vignette)
    for i in range(120):
        a = int(90 * (i / 120) ** 2)
        vd.rectangle([i, i, VIDEO_W - i, VIDEO_H - i], outline=(0, 0, 0, a), width=1)
    img = Image.alpha_composite(img, vignette)
    img.filter(ImageFilter.GaussianBlur(0.6)).save(fn)


def create_ui_overlay(name, topic, pos, glow, fn):
    """Name card with a dedicated strip underneath the text for the audio bar.

    Returns the exact rectangle the waveform must be drawn into, so it can never
    land on top of the name.
    """
    img = Image.new("RGBA", (VIDEO_W, VIDEO_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    name_font = load_font(40, bold=True)
    topic_font = load_font(24, bold=True)

    label = name if len(name) <= 44 else name[:42].rstrip() + ".."
    tb = draw.textbbox((0, 0), label, font=name_font)
    text_w = tb[2] - tb[0]
    text_h = tb[3] - tb[1]

    pad_x, pad_top, pad_bottom = 26, 20, 18
    gap = 14                 # clear space between the name and the audio bar
    wave_h = 26
    dot_slot = 40            # room for the "live" dot ahead of the name

    inner_w = dot_slot + text_w
    card_w = max(inner_w + pad_x * 2, 380)
    card_h = pad_top + text_h + gap + wave_h + pad_bottom
    card_bottom = VIDEO_H - 56
    card_top = card_bottom - card_h

    if pos == "left":
        card_left = 90
    elif pos == "right":
        card_left = VIDEO_W - 90 - card_w
    else:
        card_left = (VIDEO_W - card_w) // 2
    card_right = card_left + card_w

    draw.rounded_rectangle([card_left, card_top, card_right, card_bottom],
                           radius=16, fill=(0, 0, 0, 195),
                           outline=hex_to_rgba(glow, 235), width=2)

    # Live dot, inside the card so it can never run off the edge of the frame.
    dot_r = 9
    dot_cx = card_left + pad_x + dot_r + 2
    dot_cy = card_top + pad_top + text_h // 2
    draw.ellipse([dot_cx - dot_r - 6, dot_cy - dot_r - 6, dot_cx + dot_r + 6, dot_cy + dot_r + 6],
                 fill=hex_to_rgba(glow, 70))
    draw.ellipse([dot_cx - dot_r, dot_cy - dot_r, dot_cx + dot_r, dot_cy + dot_r],
                 fill=hex_to_rgba(glow, 255))

    text_x = card_left + pad_x + dot_slot
    text_y = card_top + pad_top - tb[1]
    draw.text((text_x, text_y), label, font=name_font, fill=(255, 255, 255, 255))

    # Faint rail the waveform sits on, entirely below the text row.
    wave_x = card_left + pad_x
    wave_y = card_top + pad_top + text_h + gap
    wave_w = card_w - pad_x * 2
    rail_y = wave_y + wave_h // 2
    draw.line([wave_x, rail_y, wave_x + wave_w, rail_y], fill=hex_to_rgba(glow, 55), width=2)

    topic_text = topic if len(topic) <= 90 else topic[:88] + ".."
    draw.text((VIDEO_W // 2, 66), topic_text, font=topic_font, fill=(255, 255, 255, 185), anchor="mm")

    img.save(fn)
    return {"wave_x": int(wave_x), "wave_y": int(wave_y),
            "wave_w": int(wave_w), "wave_h": int(wave_h)}


def get_audio_duration(p):
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=noprint_wrappers=1:nokey=1", p],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15)
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def format_ass_time(s):
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    sec = int(s % 60)
    cs = int((s - int(s)) * 100)
    return f"{h}:{m:02d}:{sec:02d}.{cs:02d}"


def ass_escape(t):
    return t.replace("\\", "\\\\").replace("{", r"\{").replace("}", r"\}")


def speech_end_seconds(path):
    """Where the voice actually stops, ignoring the silence the file ends on.

    The synthesised audio ends with a tail of silence that nothing is spoken
    over. Comparing word timings against the full file duration therefore hides
    real drift: a segment whose subtitles run a second late can still finish
    inside the file, so it looks correct and is left alone. The longer the
    segment, the more drift survives that check, which is why the moderator's
    intro, the longest single read in the video, drifted the most.
    """
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats", "-i", path,
             "-af", "silencedetect=noise=-45dB:d=0.30", "-f", "null", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
    except Exception:
        return None
    total = get_audio_duration(path)
    if not total:
        return None
    # The last stretch of silence is the tail if it reaches the end of the
    # file, whether ffmpeg leaves it open or closes it off at EOF. A silence
    # that closes early is just a pause between sentences.
    last_start, last_end = None, None
    for line in r.stderr.splitlines():
        if "silence_start:" in line:
            try:
                last_start = float(line.split("silence_start:")[1].strip().split()[0])
                last_end = None
            except (ValueError, IndexError):
                pass
        elif "silence_end:" in line:
            try:
                last_end = float(line.split("silence_end:")[1].strip().split()[0])
            except (ValueError, IndexError):
                last_end = total
    if last_start is None:
        return total
    if last_end is not None and last_end < total - 0.15:
        return total
    # Ignore an implausible answer rather than dragging every line forward.
    if last_start < total * 0.5:
        return total
    return last_start


def generate_subtitles(words, fn, scorecard=False, audio_file=None, full_text=None):
    header = ("[Script Info]\nScriptType: v4.00+\nPlayResX: 1920\nPlayResY: 1080\nWrapStyle: 0\n\n"
              "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
              "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, "
              "Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, "
              "Encoding\n"
              "Style: DebateSub,DejaVu Sans,42,&H00FFFFFF,&H00FFFFFF,&H00000000,&HAA000000,1,0,0,0,"
              "100,100,0,0,1,3,1,2,120,120,80,1\n"
              "Style: ScoreSub,DejaVu Sans,36,&H00FFFFFF,&H00FFFFFF,&H00000000,&HAA000000,1,0,0,0,"
              "100,100,0,0,1,2,1,2,80,80,40,1\n\n"
              "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    events = []
    # Scorecards used to carry the whole narration as one static block, which
    # covered the bottom third of the card for its entire duration. They get
    # the same timed lines as everything else, sat below the card's content.
    style = "ScoreSub" if scorecard else "DebateSub"
    pos_y = 1030 if scorecard else 790
    if not words:
        if scorecard and audio_file and full_text:
            dur = get_audio_duration(audio_file) or 6.0
            events.append(f"Dialogue: 0,0:00:00.00,{format_ass_time(dur)},{style},,0,0,0,,"
                          f"{ass_escape(full_text)}")
        open(fn, "w", encoding="utf-8").write(header + "\n".join(events) + "\n")
        return
    if audio_file:
        try:
            actual = get_audio_duration(audio_file)
            if actual > 1 and words:
                # Where the voice stops, not where the file stops. Stretching
                # timings to fill the whole file was the old behaviour and it
                # made every subtitle progressively late; measuring against the
                # file's full length instead let real drift through untouched.
                spoken = speech_end_seconds(audio_file) or actual
                est = words[-1].get("end", spoken)
                if est > spoken + 0.15 and est > 0:
                    scale = spoken / est
                    # A wild correction means the measurement is wrong, not that
                    # the timings are. Leave them alone rather than wreck them.
                    if scale >= 0.80:
                        for w in words:
                            w["start"] *= scale
                            w["end"] *= scale
        except Exception:
            pass

    # Put the line up a fraction before the word is said. Reading slightly
    # ahead feels natural; reading behind feels broken.
    for w in words:
        w["start"] = max(0.0, w["start"] - SUBTITLE_LEAD)
        w["end"] = max(w["start"] + 0.2, w["end"] - SUBTITLE_LEAD * 0.5)

    lines = []

    def emit(chunk, end):
        per_line = 8 if scorecard else 10
        txt = "\\N".join([" ".join([ass_escape(c["text"]) for c in chunk[i:i + per_line]])
                          for i in range(0, len(chunk), per_line)][:4])
        lines.append([chunk[0]["start"], end, txt])

    chunk = []
    last_end = 0
    for w in words:
        if not chunk:
            chunk = [w]
            last_end = w["end"]
        elif w["start"] - last_end > 0.6 or len(chunk) >= 7:
            emit(chunk, last_end)
            chunk = [w]
            last_end = w["end"]
        else:
            chunk.append(w)
            last_end = w["end"]
    if chunk:
        emit(chunk, last_end)

    # A line that arrives exactly as its first word is spoken reads as late,
    # because nobody can read it until after they have heard it. Each line is
    # pulled back into the pause in front of it instead, without ever running
    # into the line before.
    prev_end = 0.0
    for line in lines:
        line[0] = max(prev_end + 0.05, line[0] - SUBTITLE_GAP_LEAD)
        line[1] = max(line[1], line[0] + 0.4)
        prev_end = line[1]

    for start, end, txt in lines:
        events.append(f"Dialogue: 0,{format_ass_time(start)},{format_ass_time(end)},"
                      f"{style},,0,0,0,,"
                      f"{{\\an2\\pos(960,{pos_y})\\q2\\fad(120,120)}}{txt}")
    open(fn, "w", encoding="utf-8").write(header + "\n".join(events) + "\n")


async def _edge_synthesize(text, spec, fn):
    """Speak via edge-tts, using its native prosody parameters.

    edge-tts XML escapes whatever text it is given and drops it inside its own
    <prosody> element, so hand written SSML is read out loud as words rather
    than applied. Rate, pitch and volume must be passed as arguments instead.
    """
    com = edge_tts.Communicate(
        text, spec["edge_voice"],
        rate=spec.get("rate", "+0%"),
        pitch=spec.get("pitch", "+0Hz"),
        volume=spec.get("volume", "+0%"),
    )
    audio = b""
    words = []
    async for chunk in com.stream():
        if chunk["type"] == "audio":
            audio += chunk["data"]
        elif chunk["type"] == "WordBoundary":
            s = chunk["offset"] / 10_000_000
            d = chunk["duration"] / 10_000_000
            words.append({"text": chunk["text"], "start": s, "duration": d, "end": s + d})
    open(fn, "wb").write(audio)
    return words


def _eleven_synthesize(text, spec, fn):
    """Speak via ElevenLabs, with character timings folded up into words."""
    voice_id = spec.get("eleven_voice")
    if not voice_id:
        raise RuntimeError("no ElevenLabs voice id configured for this speaker")
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/with-timestamps"
    payload = {
        "text": text,
        "model_id": ELEVEN_MODEL,
        "voice_settings": {
            "stability": spec.get("stability", 0.45),
            "similarity_boost": 0.75,
            "style": spec.get("style", 0.35),
            "use_speaker_boost": True,
        },
    }
    r = requests.post(url, headers={"xi-api-key": ELEVENLABS_API_KEY,
                                    "Content-Type": "application/json"},
                      json=payload, timeout=180)
    if r.status_code != 200:
        raise RuntimeError(f"ElevenLabs returned {r.status_code}: {r.text[:200]}")
    data = r.json()
    open(fn, "wb").write(base64.b64decode(data["audio_base64"]))

    align = data.get("alignment") or {}
    chars = align.get("characters") or []
    starts = align.get("character_start_times_seconds") or []
    ends = align.get("character_end_times_seconds") or []
    words = []
    buf, w_start, w_end = "", None, None
    for ch, cs, ce in zip(chars, starts, ends):
        if ch.isspace():
            if buf:
                words.append({"text": buf, "start": w_start,
                              "duration": w_end - w_start, "end": w_end})
                buf, w_start = "", None
            continue
        if w_start is None:
            w_start = cs
        buf += ch
        w_end = ce
    if buf and w_start is not None:
        words.append({"text": buf, "start": w_start,
                      "duration": w_end - w_start, "end": w_end})
    return words


def _even_word_timings(text, fn):
    """Last resort timings, spread across the real audio duration."""
    toks = text.split()
    dur = get_audio_duration(fn) or max(1.0, len(toks) / DEFAULT_WORDS_PER_SEC)
    step = dur / max(1, len(toks))
    return [{"text": t, "start": i * step, "duration": step * 0.85,
             "end": i * step + step * 0.85} for i, t in enumerate(toks)]


async def generate_audio_async(text, spec, fn):
    ct = clean_for_speech(text)
    if TTS_PROVIDER == "elevenlabs" and ELEVENLABS_API_KEY:
        try:
            words = _eleven_synthesize(ct, spec, fn)
            if words:
                return words
            return _even_word_timings(ct, fn)
        except Exception as e:
            print(f"    ElevenLabs failed ({type(e).__name__}: {str(e)[:120]}); "
                  f"falling back to edge-tts for this segment.")
    words = await _edge_synthesize(ct, spec, fn)
    if not words:
        words = _even_word_timings(ct, fn)
    return words


def voice_for_slot(slot, judge_voice_index=None):
    """Voice plus delivery settings for a speaker slot.

    Slight differences in rate and pitch give each speaker a distinct delivery
    and stop the debate sounding like one voice reading both sides.
    """
    if slot == "A":
        return {"edge_voice": SIDE_A_VOICE, "eleven_voice": ELEVEN_VOICE_A,
                "rate": "-3%", "pitch": "-2Hz", "stability": 0.42, "style": 0.40}
    if slot == "B":
        return {"edge_voice": SIDE_B_VOICE, "eleven_voice": ELEVEN_VOICE_B,
                "rate": "+3%", "pitch": "+4Hz", "stability": 0.40, "style": 0.45}
    if slot == "JUDGE":
        idx = (judge_voice_index or 0) % len(JUDGE_VOICES)
        return {"edge_voice": JUDGE_VOICES[idx],
                "eleven_voice": ELEVEN_JUDGE_VOICES[idx % len(ELEVEN_JUDGE_VOICES)],
                # Nudge each judge slightly differently so the panel sounds
                # like several people rather than one.
                "rate": f"{-4 + (idx % 5) * 2:+d}%",
                "pitch": f"{-3 + (idx % 4) * 2:+d}Hz",
                "stability": 0.50, "style": 0.30}
    return {"edge_voice": MODERATOR_VOICE, "eleven_voice": ELEVEN_VOICE_MOD,
            "rate": "-1%", "pitch": "+0Hz", "stability": 0.55, "style": 0.25}


def generate_audio(text, slot, fn, judge_voice_index=None):
    spec = voice_for_slot(slot, judge_voice_index)
    try:
        return asyncio.run(generate_audio_async(text, spec, fn))
    except Exception:
        fallback = {"edge_voice": MODERATOR_VOICE, "eleven_voice": ELEVEN_VOICE_MOD}
        return asyncio.run(generate_audio_async(text, fallback, fn))


def render_video_segment(bg_path, ui_path, audio_path, subs_path, output_path,
                         position, glow, wave_box, visual_plan):
    duration = get_audio_duration(audio_path) or 10.0
    cmd = ["ffmpeg", "-y", "-loop", "1", "-i", bg_path, "-loop", "1", "-i", ui_path, "-i", audio_path]
    fp = []
    fp.append(f"[0:v]scale={VIDEO_W}:{VIDEO_H}:flags=lanczos[bg]")
    fp.append(f"[1:v]scale={VIDEO_W}:{VIDEO_H}:flags=lanczos[ui]")
    if position == "left":
        fp.append("[bg]scale=iw*1.3:ih*1.3,crop=1920:1080:(iw-1920)/2-200:(ih-1080)/2[bg_zoom]")
    elif position == "right":
        fp.append("[bg]scale=iw*1.3:ih*1.3,crop=1920:1080:(iw-1920)/2+200:(ih-1080)/2[bg_zoom]")
    else:
        fp.append("[bg]scale=iw*1.25:ih*1.25,crop=1920:1080:(iw-1920)/2:(ih-1080)/2[bg_zoom]")

    glow_hex = glow.lstrip("#")
    ww, wh = wave_box["wave_w"], wave_box["wave_h"]
    wx, wy = wave_box["wave_x"], wave_box["wave_y"]
    # showwaves paints on black; key the black out so only the bar itself shows.
    fp.append(f"[2:a]aformat=channel_layouts=mono,compand=gain=-4,"
              f"showwaves=s={ww}x{wh}:mode=cline:colors=0x{glow_hex}:rate={FPS}:draw=full:scale=sqrt,"
              f"format=rgba,colorkey=0x000000:0.32:0.08[wave]")
    fp.append("[bg_zoom][ui]overlay=0:0:shortest=1[bg_ui]")
    fp.append(f"[bg_ui][wave]overlay={wx}:{wy}:shortest=1[stage]")

    last = "[stage]"
    visual_inputs = []
    for vis in visual_plan:
        if not isinstance(vis, dict):
            continue
        st = float(vis.get("start", 0.0))
        et = float(vis.get("end", st + 4.0))
        still = None
        try:
            if vis.get("kind") == "emoji":
                still = create_emoji_asset(vis.get("emoji", ""))
            else:
                if CUE_STYLE == "animation":
                    clip = make_cue_clip(vis.get("scene", ""), vis.get("motion", ""))
                    if clip:
                        visual_inputs.append((clip, st, et, "video"))
                        continue
                still = make_illustration(vis.get("scene", ""))
        except Exception:
            still = None
        # No artwork means no cue. Nothing is ever drawn as a placeholder.
        if still:
            kind = "emoji" if vis.get("kind") == "emoji" else "image"
            visual_inputs.append((still, st, et, kind))

    for idx, (path, st, et, kind) in enumerate(visual_inputs):
        span = max(0.8, et - st)
        fade_out = max(0.0, span - 0.5)
        vx = (VIDEO_W - CUE_W) // 2
        vy = 210
        if kind == "video":
            # Key the paper white out of every frame so the animation sits on
            # the stage rather than inside a white card.
            fp.append(
                f"[{3 + idx}:v]scale={CUE_W}:-2:force_original_aspect_ratio=decrease,"
                f"format=rgba,colorkey=0xFFFFFF:0.16:0.05,"
                f"fade=t=in:st=0:d=0.4:alpha=1,"
                f"fade=t=out:st={fade_out:.2f}:d=0.5:alpha=1,"
                f"setpts=PTS-STARTPTS+{st:.2f}/TB[v{idx}]")
            fp.append(f"{last}[v{idx}]overlay={vx}:{vy}:eof_action=pass"
                      f":enable='between(t,{st:.2f},{et:.2f})'[tmp{idx}]")
        elif kind == "emoji":
            # The stream must be shifted to the moment the cue is due. Without
            # the offset it plays at the start of the segment and finishes on a
            # transparent fade-out frame, which overlay then holds, so every
            # cue after the first one is invisible.
            fp.append(f"[{3 + idx}:v]scale={EMOJI_W}:{EMOJI_H},format=rgba,"
                      f"fade=t=in:st=0:d=0.3:alpha=1,"
                      f"fade=t=out:st={fade_out:.2f}:d=0.4:alpha=1,"
                      f"setpts=PTS-STARTPTS+{st:.2f}/TB[v{idx}]")
            ex = (VIDEO_W - EMOJI_W) // 2
            ey = (VIDEO_H - EMOJI_H) // 2 - 50
            fp.append(f"{last}[v{idx}]overlay={ex}:{ey}"
                      f":enable='between(t,{st:.2f},{et:.2f})'[tmp{idx}]")
        else:
            fp.append(
                f"[{3 + idx}:v]scale={CUE_W}:{CUE_H}:force_original_aspect_ratio=decrease,"
                f"format=rgba,"
                f"fade=t=in:st=0:d=0.45:alpha=1,"
                f"fade=t=out:st={fade_out:.2f}:d=0.5:alpha=1,"
                f"setpts=PTS-STARTPTS+{st:.2f}/TB[v{idx}]")
            drift = f"{vy}+18*sin((t-{st:.2f})*0.7)"
            fp.append(f"{last}[v{idx}]overlay={vx}:'{drift}'"
                      f":enable='between(t,{st:.2f},{et:.2f})'[tmp{idx}]")
        last = f"[tmp{idx}]"

    safe_subs = subs_path.replace(":", "\\:")
    fp.append(f"{last}format=yuv420p,subtitles={safe_subs}[out]")

    for gp, st, et, kind in visual_inputs:
        if kind == "video":
            cmd.extend(["-i", gp])
        else:
            cmd.extend(["-loop", "1", "-t", f"{max(0.8, et - st):.2f}", "-i", gp])
    cmd.extend(["-filter_complex", ";".join(fp), "-map", "[out]", "-map", "2:a",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-r", str(FPS),
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
                "-shortest", "-t", str(duration + 0.5), output_path])
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if r.returncode != 0:
        print(r.stderr[-8000:])
        raise RuntimeError("Render failed")
    return duration


def generate_scoreboard(rn, res, avg_a, avg_b, cum_a, cum_b, path, roles,
                        va=None, vb=None, vt=None, vu=None):
    W, H = VIDEO_W, VIDEO_H
    base = Image.new("RGB", (W, H), (12, 16, 32))
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 180))
    img = Image.alpha_composite(base.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(img)
    ft = load_font(48, bold=True)
    fs = load_font(28, bold=True)
    fh = load_font(22, bold=True)
    fr = load_font(24)
    draw.text((W // 2, 50), f"ROUND {rn} - HOW THE JURY MARKED IT",
              font=ft, fill=(255, 215, 0), anchor="mt")
    draw.text((W // 2, 115), f"{roles['side_a_label']}  vs  {roles['side_b_label']}",
              font=fs, fill=(255, 255, 255), anchor="mt")
    hy = 190
    cx1, cx2, cx3, cx4 = 120, 750, 1050, 1350
    sa = roles["side_a_label"][:18]
    sb = roles["side_b_label"][:18]
    draw.rectangle([60, hy - 10, W - 60, hy + 45], fill=(25, 35, 70), outline=(255, 215, 0), width=2)
    draw.text((cx1, hy), "Juror", font=fh, fill=(255, 255, 255))
    draw.text((cx2, hy), sa, font=fh, fill=(0, 255, 204))
    draw.text((cx3, hy), sb, font=fh, fill=(255, 120, 255))
    draw.text((cx4, hy), "Winner", font=fh, fill=(255, 215, 0))
    # The panel can be large, so rows shrink to fit rather than run off screen.
    # The bottom of the frame is left clear for the spoken subtitle line.
    y = hy + 65
    available = 790 - y
    row_h = max(30, min(58, available // max(1, len(res))))
    fr = load_font(max(15, min(24, row_h - 22)))
    for idx, r in enumerate(res):
        draw.rectangle([60, y - 6, W - 60, y + row_h - 14],
                       fill=(20, 28, 50) if idx % 2 == 0 else (15, 22, 40))
        jt = f"{r['display_name']} ({r['provider']})"
        if len(jt) > 32:
            jt = jt[:30] + ".."
        draw.text((cx1, y), jt, font=fr, fill=(255, 255, 255))
        draw.text((cx2, y), f"{r['A_total']:.1f}", font=fr, fill=(0, 255, 204))
        draw.text((cx3, y), f"{r['B_total']:.1f}", font=fr, fill=(255, 120, 255))
        if r["winner"] == "A":
            wl, col = roles["side_a_label"], (0, 255, 204)
        elif r["winner"] == "B":
            wl, col = roles["side_b_label"], (255, 120, 255)
        elif r["winner"] == "TIE":
            wl, col = "TIE", (220, 220, 220)
        else:
            wl, col = "NO CALL", (150, 150, 150)
        draw.text((cx4, y), wl, font=fr, fill=col)
        y += row_h
    draw.line([(60, y + 5), (W - 60, y + 5)], fill=(255, 255, 255), width=2)
    y += 25
    if va is not None:
        split = f"Jury: {va} - {vb}" + (f" ({vt} draw)" if vt else "")
        if vu:
            split += f"   {vu} thrown out"
        draw.text((W // 2, y), split, font=fs, fill=(255, 255, 255), anchor="mt")
        y += 45
    draw.text((W // 2, y), f"Round Avg: {avg_a:.1f} vs {avg_b:.1f}",
              font=fs, fill=(200, 200, 200), anchor="mt")
    draw.text((W // 2, y + 45),
              f"Average after {rn} round{'s' if rn > 1 else ''}: "
              f"{cum_a / rn:.1f} vs {cum_b / rn:.1f}",
              font=fs, fill=(255, 215, 0), anchor="mt")
    img.save(path)


def generate_poll_board(results, summary, roles, path, title, before=None):
    """Where each model stands, drawn as a lean from one side to the other."""
    W, H = VIDEO_W, VIDEO_H
    img = Image.alpha_composite(
        Image.new("RGB", (W, H), (12, 16, 32)).convert("RGBA"),
        Image.new("RGBA", (W, H), (0, 0, 0, 180))).convert("RGB")
    draw = ImageDraw.Draw(img)
    ft = load_font(46, bold=True)
    fs = load_font(26, bold=True)
    fr = load_font(23)

    draw.text((W // 2, 44), title, font=ft, fill=(255, 215, 0), anchor="mt")
    # Side A sits on the left everywhere else in the video, so it sits on the
    # left here too. The signed value in the right hand column is unchanged.
    draw.text((W // 2, 104), f"{roles['side_a_label']}  \u2190   lean   \u2192  "
                             f"{roles['side_b_label']}",
              font=fs, fill=(255, 255, 255), anchor="mt")

    left, right = 430, W - 430
    mid = (left + right) // 2
    top = 168
    rows = results
    row_h = max(28, min(52, (790 - top) // max(1, len(rows))))
    fr = load_font(max(14, min(23, row_h - 25)))

    # Scale gridline
    draw.line([(mid, top - 14), (mid, top + row_h * len(rows) + 6)],
              fill=(255, 255, 255, 60), width=1)

    for i, r in enumerate(rows):
        y = top + i * row_h
        if i % 2 == 0:
            draw.rectangle([60, y - 5, W - 60, y + row_h - 10], fill=(20, 28, 50))
        name = f"{r['display_name']} ({r['provider']})"
        draw.text((80, y), name[:34], font=fr, fill=(255, 255, 255))

        if r["declined"] or r["position"] is None:
            draw.text((mid, y), "declined to answer", font=fr,
                      fill=(150, 150, 150), anchor="mt")
            continue
        pos = r["position"]
        x = int(mid - (pos / POLL_SCALE) * (right - mid))
        colour = (0, 255, 204) if pos > LEAN_THRESHOLD else \
                 (255, 120, 255) if pos < -LEAN_THRESHOLD else (220, 220, 220)
        my = y + row_h // 3
        draw.line([(mid, my), (x, my)], fill=colour, width=max(3, row_h // 10))
        rr = max(5, row_h // 6)
        draw.ellipse([x - rr, my - rr, x + rr, my + rr], fill=colour)
        # Strength only, coloured to the side it leans. A signed number here
        # read as a contradiction once the axis was flipped, and the exact
        # signed value is in scores.json either way.
        draw.text((right + 26, y), f"{abs(pos):.1f}", font=fr, fill=colour)

    y = top + row_h * len(rows) + 24
    draw.line([(60, y), (W - 60, y)], fill=(255, 255, 255), width=2)
    y += 18
    line = (f"{summary['lean_a']} say {roles['side_a_label']}   |   "
            f"{summary['lean_b']} say {roles['side_b_label']}   |   "
            f"{summary['undecided']} on the fence   |   "
            f"{summary['declined']} would not say")
    draw.text((W // 2, y), line, font=fs, fill=(255, 255, 255), anchor="mt")
    y += 44
    if before is None:
        draw.text((W // 2, y), f"Average lean: {summary['mean']:+.2f}",
                  font=fs, fill=(255, 215, 0), anchor="mt")
    else:
        swing = summary["mean"] - before["mean"]
        draw.text((W // 2, y),
                  f"Average lean: {before['mean']:+.2f}  \u2192  {summary['mean']:+.2f}"
                  f"   (moved {swing:+.2f})",
                  font=fs, fill=(255, 215, 0), anchor="mt")
    img.save(path)


def _fit_font(draw, text, max_w, start, floor=30, bold=True):
    """Largest font at or below start that keeps text inside max_w."""
    size = start
    while size > floor:
        f = load_font(size, bold=bold)
        if draw.textlength(text, font=f) <= max_w:
            return f
        size -= 4
    return load_font(floor, bold=bold)


def _draw_split_bar(draw, x, y, w, h, counts, colours, font):
    """One horizontal bar split into segments, each labelled with its count.

    Used for the before and after rows of the verdict card. Seeing the block
    of colour grow is what makes where the room landed obvious at a glance,
    without having to read three numbers and work out which one matters.
    """
    total = sum(counts)
    if total <= 0:
        draw.rounded_rectangle([x, y, x + w, y + h], radius=8, fill=(40, 48, 70))
        return
    edges = [x]
    run = 0
    for n in counts:
        run += n
        edges.append(x + int(round(w * run / total)))
    for i, (n, colour) in enumerate(zip(counts, colours)):
        x0, x1 = edges[i], edges[i + 1]
        if x1 <= x0:
            continue
        draw.rectangle([x0, y, x1, y + h], fill=colour)
        if x1 - x0 >= 34:
            draw.text(((x0 + x1) // 2, y + h // 2), str(n), font=font,
                      fill=(10, 14, 28), anchor="mm")
    draw.rounded_rectangle([x, y, x + w, y + h], radius=6,
                           outline=(255, 255, 255), width=2)


def generate_verdict_board(path, topic, roles, before, after, movement, votes,
                           mean_a, mean_b):
    """The closing card. One winner, named once, in the largest type on screen.

    Everything else here is a footnote to that. Giving the three results equal
    boxes, which is what this used to do, read as three competing winners and
    left people asking which number was the answer.
    """
    W, H = VIDEO_W, VIDEO_H
    A_COL, B_COL, N_COL = (0, 255, 204), (255, 120, 255), (150, 158, 180)
    img = Image.alpha_composite(
        Image.new("RGB", (W, H), (12, 16, 32)).convert("RGBA"),
        Image.new("RGBA", (W, H), (0, 0, 0, 185))).convert("RGB")
    draw = ImageDraw.Draw(img)
    fs = load_font(24)

    draw.text((W // 2, 26), "THE VERDICT", font=load_font(44, bold=True),
              fill=(255, 215, 0), anchor="mt")
    draw.text((W // 2, 84), topic[:92], font=load_font(26), fill=(235, 235, 235),
              anchor="mt")

    p = verdict_parts(roles, before, after, movement, votes, mean_a, mean_b)
    room = p["winner"]
    stated = p["stated"]
    if room == roles["side_a_label"]:
        room_colour = A_COL
    elif room == roles["side_b_label"]:
        room_colour = B_COL
    else:
        room_colour = (235, 235, 235)

    # ---- The headline: one winner, across the whole card.
    hx0, hx1, hy0, hh = 110, W - 110, 130, 470
    draw.rounded_rectangle([hx0, hy0, hx1, hy0 + hh], radius=22,
                           fill=(20, 28, 50), outline=room_colour, width=4)
    draw.text(((hx0 + hx1) // 2, hy0 + 20), "WINNER" if room else "RESULT",
              font=load_font(30, bold=True), fill=(255, 215, 0), anchor="mt")

    headline = room if room else "NO WINNER"
    hf = _fit_font(draw, headline, hx1 - hx0 - 120, 132, floor=44)
    draw.text(((hx0 + hx1) // 2, hy0 + 62), headline, font=hf,
              fill=room_colour, anchor="mt")

    if room:
        detail = (f"where the AIs landed: {p['winner_count']} of the {stated} "
                  f"finished on this side")
    else:
        detail = f"the {stated} AIs finished evenly split, with neither side ahead"
    draw.text(((hx0 + hx1) // 2, hy0 + 226), detail, font=load_font(34),
              fill=(255, 255, 255), anchor="mt")

    # Before and after, drawn as the same bar twice so the shift is visible
    # without anyone having to compare two numbers in their head.
    bar_x = hx0 + 230
    bar_w = hx1 - 60 - bar_x
    fnum = load_font(24, bold=True)
    draw.text((bar_x, hy0 + 288), roles["side_a_label"][:22], font=fs, fill=A_COL)
    draw.text((bar_x + bar_w, hy0 + 288), roles["side_b_label"][:22], font=fs,
              fill=B_COL, anchor="rt")
    for i, (tag, poll) in enumerate((("BEFORE", before), ("AFTER", after))):
        y = hy0 + 322 + i * 62
        draw.text((bar_x - 24, y + 22), tag, font=load_font(24, bold=True),
                  fill=(190, 198, 220), anchor="rm")
        _draw_split_bar(draw, bar_x, y, bar_w, 44,
                        [poll["lean_a"], poll["undecided"], poll["lean_b"]],
                        [A_COL, N_COL, B_COL], fnum)

    # ---- Everything below is a plain line of text, in one colour.
    # These used to be two coloured boxes with a side's name in each, which
    # read as two more verdicts competing with the one above. They are
    # sentences now, in the same grey as the rest of the small print.
    if p["moved"] == 0:
        shifted = "Nobody shifted anybody: not one AI changed its mind."
    elif p["persuader"]:
        shifted = (f"Shifted the most minds: {p['persuader']}, with "
                   f"{p['persuader_count']} of the {stated} moving their way.")
    else:
        shifted = ("Minds moved both ways in equal numbers, so neither side "
                   "shifted more than the other.")

    if not p["scored"]:
        marked = ("The arguing itself went unmarked: no juror returned a usable score, "
                  "and none was invented.")
    elif p["arguer"]:
        won, lost = max(votes["A"], votes["B"]), min(votes["A"], votes["B"])
        extra = ""
        if votes["TIE"]:
            extra += f", {votes['TIE']} calling it a draw"
        if votes["UNSTABLE"]:
            extra += f", {votes['UNSTABLE']} thrown out"
        marked = (f"Marked the better arguer: {p['arguer']}, the jury going {won} to "
                  f"{lost}{extra}.")
    else:
        marked = f"On the arguing itself the jury could not split them, {votes['A']} all."

    if not room:
        closer = "Neither of those would have settled it either."
    elif p["sweep"]:
        closer = "Both of those went the winner's way as well, so it is a clean sweep."
    else:
        closer = "Neither of those decides the winner."

    y = 640
    for line in (shifted, marked, closer):
        lf = _fit_font(draw, line, hx1 - hx0, 28, floor=19, bold=False)
        draw.text((W // 2, y), line, font=lf, fill=(200, 205, 215), anchor="mt")
        y += 40

    y += 14
    draw.line([(hx0, y), (hx1, y)], fill=(255, 255, 255), width=2)
    y += 18
    for line in [
        "Where they land is what the AIs think after hearing both sides. It is not proof "
        "of who is right.",
        "The jury never knew which side was which. Every round was marked twice, both ways "
        "round, and any juror who flip flopped was thrown out.",
    ]:
        draw.text((W // 2, y), line, font=load_font(22), fill=(175, 175, 175),
                  anchor="mt")
        y += 30
    img.save(path)


def prepare_verdict_segment(narration, path_args, sid="verdict"):
    """The closing card, spoken over the verdict graphic."""
    spec = {"kind": "scorecard", "sid": sid,
            "image": f"verdict_{sid}.png", "audio": f"verdict_audio_{sid}.mp3",
            "subs": f"verdict_subs_{sid}.ass", "video": f"verdict_video_{sid}.mp4"}
    generate_verdict_board(spec["image"], *path_args)
    words = generate_audio(narration, "MOD", spec["audio"])
    generate_subtitles(words, spec["subs"], scorecard=True,
                       audio_file=spec["audio"], full_text=narration)
    spec["duration"] = get_audio_duration(spec["audio"])
    return spec


def prepare_poll_segment(results, summary, roles, narration, sid, title, before=None):
    """Poll graphic plus its spoken read. Rendered later with the rest."""
    spec = {"kind": "scorecard", "sid": sid,
            "image": f"poll_{sid}.png", "audio": f"poll_audio_{sid}.mp3",
            "subs": f"poll_subs_{sid}.ass", "video": f"poll_video_{sid}.mp4"}
    generate_poll_board(results, summary, roles, spec["image"], title, before)
    words = generate_audio(narration, "MOD", spec["audio"])
    generate_subtitles(words, spec["subs"], scorecard=True,
                       audio_file=spec["audio"], full_text=narration)
    spec["duration"] = get_audio_duration(spec["audio"])
    return spec


def render_scorecard_video(ip, ap, sp, op):
    dur = get_audio_duration(ap) or 6.0
    safe = sp.replace(":", "\\:")
    cmd = ["ffmpeg", "-y", "-loop", "1", "-i", ip, "-i", ap, "-filter_complex",
           f"[0:v]scale={VIDEO_W}:{VIDEO_H}:flags=lanczos,format=yuv420p,subtitles={safe}[out]",
           "-map", "[out]", "-map", "1:a",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-r", str(FPS),
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
           "-shortest", "-t", str(dur + 0.6), op]
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if r.returncode != 0:
        print(r.stderr[-7000:])
        raise RuntimeError("Scorecard render failed")
    return dur


# ---------------------------------------------------------------------------
# Illustrated cues
#
# Instead of emoji, each turn gets one or two hand drawn style illustrations of
# something actually being said. A model picks the moments and describes the
# scene, an image model draws it, and it is composited over the stage with a
# slow drift and a soft fade. If anything in that chain is unavailable the cue
# is dropped: there is no placeholder and nothing is ever drawn as a box.
# ---------------------------------------------------------------------------

# How on-screen cues are drawn:
#   emoji        free, Twemoji artwork, no API of any kind (default)
#   illustration generated stills, needs IMAGE_PROVIDER and costs a little
#   animation    short generated clips, needs a video provider and costs a lot
#   none         no cues at all
CUE_STYLE = os.environ.get("CUE_STYLE", "emoji").strip().lower()

IMAGE_PROVIDER = os.environ.get("IMAGE_PROVIDER", "none").strip().lower()
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
REPLICATE_API_TOKEN = os.environ.get("REPLICATE_API_TOKEN")
REPLICATE_IMAGE_MODEL = os.environ.get(
    "REPLICATE_IMAGE_MODEL", "black-forest-labs/flux-schnell")
# Stills are cheap, so two a turn is fine; animation is billed per second, so
# one a turn is the sane default unless it is set explicitly.
CUES_PER_TURN = int(os.environ.get(
    "CUES_PER_TURN",
    "1" if os.environ.get("ANIMATE_CUES", "").strip() not in ("", "0", "false", "no")
    else "2"))

# Animate cues into short clips instead of drifting a still. The still is
# generated first and used as the opening frame, so the clip inherits the same
# drawn style. Check Replicate for the current image to video models; the model
# and its image input key are both configurable because they change often.
ANIMATE_CUES = os.environ.get("ANIMATE_CUES", "").strip() not in ("", "0", "false", "no")

# OpenRouter generates video as well as text, on the same key and the same
# credit balance as the debate itself, so that is the default. Replicate stays
# available as an alternative.
VIDEO_PROVIDER = os.environ.get("VIDEO_PROVIDER", "openrouter").strip().lower()
OPENROUTER_VIDEO_URL = "https://openrouter.ai/api/v1/videos"

# Video is billed per second of output and is by far the most expensive part of
# a build, so the default order prefers the cheap models. An exact id set in
# OPENROUTER_VIDEO_MODEL always wins; otherwise the first of these that the
# account can actually see is used.
OPENROUTER_VIDEO_MODEL = os.environ.get("OPENROUTER_VIDEO_MODEL", "").strip()
VIDEO_MODEL_PREFERENCE = ["wan", "seedance", "veo-3.1-fast", "veo", "sora"]

REPLICATE_VIDEO_MODEL = os.environ.get("REPLICATE_VIDEO_MODEL",
                                       "wan-video/wan-2.1-i2v-480p")
REPLICATE_VIDEO_IMAGE_KEY = os.environ.get("REPLICATE_VIDEO_IMAGE_KEY", "image")
CUE_CLIP_SECONDS = float(os.environ.get("CUE_CLIP_SECONDS", "4"))
_VIDEO_MODEL_RESOLVED = None
ILLUSTRATION_DIR = "illustration_cache"
os.makedirs(ILLUSTRATION_DIR, exist_ok=True)
IMAGE_GEN_OK = True
VIDEO_GEN_OK = True

# Held constant so every illustration in every video looks like the same hand.
ILLUSTRATION_STYLE = (
    "loose hand drawn ink line art with soft watercolour washes, muted natural "
    "palette, generous white space, plain white background, storybook illustration, "
    "no text, no words, no lettering, no border, no frame, one simple subject"
)
CUE_W = 520
CUE_H = 520


EMOJI_CACHE_DIR = "emoji_cache"
os.makedirs(EMOJI_CACHE_DIR, exist_ok=True)
EMOJI_CDN_OK = True
EMOJI_MISSING = set()
EMOJI_W = 180
EMOJI_H = 180
# Cues sat on screen for under two seconds, which is not long enough to read.
EMOJI_HOLD_SECONDS = 3.2
# Subtitles appear this far ahead of the word being spoken.
SUBTITLE_LEAD = 0.12
# A line is also pulled back into the pause in front of it by up to this much,
# so it is already on screen when the sentence starts rather than arriving with
# it. It never runs into the line before.
SUBTITLE_GAP_LEAD = 0.6

# Built around the words people actually say in an argument, not just topic
# nouns, because a list of topic nouns leaves most of a spoken turn with
# nothing on screen. Matched on stems, so children, child and childs all land
# on the same cue.
WORD_EMOJI_MAP = {
    # money and work
    "money": "\U0001F4B0", "cost": "\U0001F4B0", "price": "\U0001F4B0", "cheap": "\U0001F4B0",
    "expensive": "\U0001F4B8", "afford": "\U0001F4B8", "wage": "\U0001F4B5", "salar": "\U0001F4B5",
    "profit": "\U0001F4C8", "econom": "\U0001F4B9", "tax": "\U0001F4B8", "budget": "\U0001F4B8",
    "debt": "\U0001F4C9", "bank": "\U0001F3E6", "job": "\U0001F3ED", "work": "\U0001F477",
    "factor": "\U0001F3ED", "busines": "\U0001F4BC", "compan": "\U0001F3E2", "market": "\U0001F6D2",
    "trade": "\U0001F4E6", "billion": "\U0001F4B0", "million": "\U0001F4B0", "dollar": "\U0001F4B5",
    "pound": "\U0001F4B7", "buy": "\U0001F6D2", "sell": "\U0001F3F7\ufe0f", "pay": "\U0001F4B3",
    # people
    "child": "\U0001F9D2", "children": "\U0001F9D2", "kid": "\U0001F9D2", "teenager": "\U0001F9D1",
    "famil": "\U0001F46A", "parent": "\U0001F46A", "mother": "\U0001F469", "father": "\U0001F468",
    "friend": "\U0001F91D", "peopl": "\U0001F465", "person": "\U0001F9CD", "everyone": "\U0001F465",
    "nobod": "\U0001F937", "somebod": "\U0001F9CD", "someone": "\U0001F9CD", "anyone": "\U0001F465",
    "communit": "\U0001F3D8\ufe0f", "societ": "\U0001F465", "neighbour": "\U0001F3E1",
    "school": "\U0001F3EB", "educat": "\U0001F393", "student": "\U0001F393", "teacher": "\U0001F9D1",
    "home": "\U0001F3E0", "house": "\U0001F3E0", "countr": "\U0001F5FA\ufe0f", "citie": "\U0001F3D9\ufe0f",
    "city": "\U0001F3D9\ufe0f", "world": "\U0001F30E", "nation": "\U0001F5FA\ufe0f",
    # health and body
    "health": "\U0001FA7A", "medicin": "\U0001F489", "hospital": "\U0001F3E5", "doctor": "\U0001FA7A",
    "vaccin": "\U0001F489", "disease": "\U0001F9A0", "virus": "\U0001F9A0", "sick": "\U0001F912",
    "mental": "\U0001F9E0", "sleep": "\U0001F634", "awake": "\U0001F440", "tired": "\U0001F62B",
    "addict": "\U0001F4F1", "drug": "\U0001F48A", "brain": "\U0001F9E0", "bod": "\U0001F9CD",
    # evidence and argument
    "scien": "\U0001F52C", "research": "\U0001F52C", "stud": "\U0001F4C8", "data": "\U0001F4CA",
    "evidence": "\U0001F50D", "proof": "\U0001F50D", "prove": "\U0001F50D", "experiment": "\u2697\ufe0f",
    "number": "\U0001F522", "statistic": "\U0001F4CA", "percent": "\U0001F4C8", "surve": "\U0001F4CB",
    "report": "\U0001F4C4", "record": "\U0001F4DA", "histor": "\U0001F4DC", "book": "\U0001F4D6",
    "fact": "\U0001F4CC", "truth": "\U0001F4A1", "true": "\u2705", "false": "\u274C",
    "wrong": "\u274C", "correct": "\u2705", "lie": "\U0001F925", "honest": "\U0001F91D",
    "doubt": "\U0001F914", "certain": "\U0001F4AF", "argu": "\U0001F5E3\ufe0f", "claim": "\U0001F5E3\ufe0f",
    "point": "\U0001F449", "question": "\u2753", "answer": "\U0001F4A1", "reason": "\U0001F9E9",
    "example": "\U0001F4CC", "case": "\U0001F4C1", "stor": "\U0001F4D6", "review": "\U0001F50D",
    "audit": "\U0001F4CB", "test": "\U0001F9EA", "measur": "\U0001F4CF", "compar": "\u2696\ufe0f",
    # thinking and feeling
    "think": "\U0001F4AD", "thought": "\U0001F4AD", "belie": "\U0001F4AD", "idea": "\U0001F4A1",
    "know": "\U0001F9E0", "understand": "\U0001F9E0", "remember": "\U0001F9E0", "forget": "\U0001F4AD",
    "agree": "\U0001F44D", "disagree": "\U0001F44E", "admit": "\U0001F64B", "deny": "\U0001F645",
    "worr": "\U0001F61F", "afraid": "\U0001F628", "fear": "\U0001F628", "angry": "\U0001F621",
    "happ": "\U0001F642", "sad": "\U0001F622", "love": "\u2764\ufe0f", "hate": "\U0001F620",
    "care": "\U0001F49B", "hope": "\U0001F31F", "pain": "\U0001F623", "suffer": "\U0001F622",
    # law, politics, power
    "law": "\u2696\ufe0f", "legal": "\u2696\ufe0f", "court": "\u2696\ufe0f", "judge": "\u2696\ufe0f",
    "right": "\u270A", "freedom": "\U0001F54A\ufe0f", "ban": "\U0001F6AB", "allow": "\u2705",
    "govern": "\U0001F3DB\ufe0f", "police": "\U0001F46E", "vote": "\U0001F5F3\ufe0f", "election": "\U0001F5F3\ufe0f",
    "war": "\u2694\ufe0f", "peace": "\u262E\ufe0f", "crime": "\U0001F6A8", "prison": "\u26D3\ufe0f",
    "regulat": "\U0001F4CB", "polic": "\U0001F4DC", "rule": "\U0001F4CF", "power": "\u26A1",
    "control": "\U0001F39B\ufe0f", "force": "\U0001F4AA", "protect": "\U0001F6E1\ufe0f", "enforce": "\U0001F46E",
    # world and things
    "climat": "\U0001F30D", "planet": "\U0001F30D", "earth": "\U0001F30E", "energ": "\u26A1",
    "oil": "\U0001F6E2\ufe0f", "solar": "\u2600\ufe0f", "pollut": "\U0001F3ED", "carbon": "\U0001F4A8",
    "forest": "\U0001F332", "ocean": "\U0001F30A", "weather": "\U0001F326\ufe0f", "fire": "\U0001F525",
    "water": "\U0001F4A7", "food": "\U0001F35E", "farm": "\U0001F33E", "animal": "\U0001F98C",
    "car": "\U0001F697", "road": "\U0001F6E3\ufe0f", "build": "\U0001F3D7\ufe0f", "machine": "\u2699\ufe0f",
    # technology
    "technolog": "\U0001F916", "computer": "\U0001F4BB", "internet": "\U0001F310", "phone": "\U0001F4F1",
    "algorithm": "\U0001F9EE", "robot": "\U0001F916", "screen": "\U0001F4F2", "online": "\U0001F310",
    "privac": "\U0001F510", "feed": "\U0001F4F2", "post": "\U0001F4EC", "media": "\U0001F4F0",
    # belief
    "god": "\u2728", "faith": "\U0001F64F", "pray": "\U0001F64F", "religio": "\u26EA",
    "church": "\u26EA", "soul": "\U0001F54A\ufe0f", "moral": "\u2696\ufe0f", "mirac": "\u2728",
    "universe": "\U0001F30C", "star": "\u2B50", "creation": "\U0001F31F", "dna": "\U0001F9EC",
    "evolut": "\U0001F9EC", "design": "\U0001F4D0",
    # stakes and change
    "death": "\U0001F5A4", "die": "\U0001F5A4", "died": "\U0001F5A4", "dying": "\U0001F5A4",
    "life": "\U0001F31F", "live": "\U0001F31F", "born": "\U0001F476", "harm": "\u26A0\ufe0f",
    "danger": "\u26A0\ufe0f", "risk": "\u26A0\ufe0f", "safe": "\U0001F6E1\ufe0f", "damage": "\U0001F4A5",
    "problem": "\u26A0\ufe0f", "solution": "\U0001F4A1", "solve": "\U0001F4A1", "fix": "\U0001F527",
    "break": "\U0001F4A5", "change": "\U0001F504", "grow": "\U0001F331", "rise": "\U0001F4C8",
    "fall": "\U0001F4C9", "drop": "\U0001F4C9", "win": "\U0001F3C6", "lose": "\U0001F4C9",
    "fight": "\U0001F94A", "help": "\U0001F91D", "save": "\U0001F6DF", "stop": "\U0001F6D1",
    "fail": "\u274C", "succe": "\u2705", "future": "\U0001F52E", "past": "\u23EA",
    # time
    "year": "\U0001F4C5", "month": "\U0001F4C6", "week": "\U0001F4C6", "day": "\u2600\ufe0f",
    "morning": "\U0001F305", "night": "\U0001F319", "hour": "\u23F0", "minute": "\u23F1\ufe0f",
    "decade": "\U0001F4C5", "centur": "\U0001F4DC", "today": "\U0001F4C5", "wait": "\u23F3",
    "time": "\u23F3", "clock": "\u23F0",
    # senses
    "look": "\U0001F440", "watch": "\U0001F440", "see": "\U0001F440", "listen": "\U0001F442",
    "hear": "\U0001F442", "read": "\U0001F4D6", "write": "\u270D\ufe0f", "speak": "\U0001F5E3\ufe0f",
    "voice": "\U0001F5E3\ufe0f", "show": "\U0001F449",
}

# Longest first, so "social media" beats "media" and "evolut" beats "evolve".
_EMOJI_STEMS = sorted(WORD_EMOJI_MAP.keys(), key=len, reverse=True)


# A stem only counts if what follows it is a real word ending. Without this,
# "star" would fire on "start" and "die" on "diet".
EMOJI_SUFFIXES = {
    "", "s", "es", "ed", "ing", "ly", "al", "y", "ies", "er", "ers", "or", "ors",
    "ion", "ions", "ist", "ists", "ic", "ical", "ment", "ments", "ance", "ence",
    "ned", "ning", "ged", "ging", "ce", "ces", "e", "le", "les", "ss", "ful", "fs",
}


def emoji_for_word(word):
    """Match a spoken word to a cue by stem, so plurals and tenses all count."""
    w = re.sub(r"[^a-z]", "", word.lower())
    if len(w) < 3:
        return None
    for stem in _EMOJI_STEMS:
        key = stem.replace("_", "").replace(" ", "")
        if len(key) >= 3 and w.startswith(key) and w[len(key):] in EMOJI_SUFFIXES:
            return WORD_EMOJI_MAP[stem]
    return None


def emoji_to_codepoint(ec):
    codes = []
    for ch in ec:
        cp = ord(ch)
        if cp == 0xfe0f:
            continue
        codes.append(f"{cp:x}")
    return "-".join(codes)


EMOJI_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
    "/usr/share/fonts/opentype/noto/NotoColorEmoji.ttf",
    "/usr/share/fonts/truetype/noto/NotoColorEmoji-Regular.ttf",
    "/System/Library/Fonts/Apple Color Emoji.ttc",
]
# Noto Color Emoji is a bitmap font and only renders at this size.
EMOJI_FONT_SIZE = 109
_EMOJI_FONT = None
_EMOJI_FONT_TRIED = False


def emoji_font():
    """The local colour emoji font, if this machine has one."""
    global _EMOJI_FONT, _EMOJI_FONT_TRIED
    if _EMOJI_FONT_TRIED:
        return _EMOJI_FONT
    _EMOJI_FONT_TRIED = True
    paths = list(EMOJI_FONT_CANDIDATES)
    paths += sorted(glob.glob("/usr/share/fonts/**/*olorEmoji*", recursive=True))
    for p in paths:
        if not os.path.exists(p):
            continue
        try:
            _EMOJI_FONT = ImageFont.truetype(p, EMOJI_FONT_SIZE)
            print(f"Emoji font: {p}")
            return _EMOJI_FONT
        except Exception:
            continue
    print("No colour emoji font installed; falling back to the emoji CDN.")
    return None


def _render_emoji_locally(ec, path):
    """Draw the emoji from the installed font. No network involved."""
    font = emoji_font()
    if font is None:
        return False
    size = EMOJI_FONT_SIZE * 2
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    try:
        d.text((size // 2, size // 2), ec, font=font, anchor="mm", embedded_color=True)
    except Exception:
        return False
    if im.getchannel("A").getextrema()[1] == 0:
        return False          # nothing drawn, so this glyph is missing
    im.crop(im.getbbox()).save(path)
    return True


def create_emoji_asset(ec):
    """Artwork for one emoji, or None.

    Drawn from the installed colour emoji font first, because a CDN that is
    slow, blocked or missing a codepoint is why cues went missing entirely.
    There is still no drawn placeholder: a cue with no artwork is dropped.
    """
    global EMOJI_CDN_OK
    code = emoji_to_codepoint(ec)
    if not code or code in EMOJI_MISSING:
        return None
    cached = os.path.join(EMOJI_CACHE_DIR, f"{code}.png")
    if os.path.exists(cached):
        try:
            if Image.open(cached).size[0] > 10:
                return cached
        except Exception:
            pass

    try:
        if _render_emoji_locally(ec, cached):
            return cached
    except Exception:
        pass

    if not EMOJI_CDN_OK:
        EMOJI_MISSING.add(code)
        return None
    for version in ("14.0.2", "15.1.0"):
        url = (f"https://cdn.jsdelivr.net/gh/jdecked/twemoji@{version}"
               f"/assets/72x72/{code}.png")
        try:
            resp = requests.get(url, timeout=8)
        except requests.exceptions.RequestException:
            EMOJI_CDN_OK = False
            print("    Emoji CDN unreachable and no local font; cues disabled.")
            return None
        if resp.status_code == 200 and len(resp.content) > 500:
            try:
                Image.open(BytesIO(resp.content)).convert("RGBA").save(cached)
                return cached
            except Exception:
                break
    EMOJI_MISSING.add(code)
    return None


def create_emoji_plan(words):
    """Emoji cues anchored to the words that trigger them."""
    if not words:
        return []
    plan = []
    last_shown = {}
    for w in words:
        ec = emoji_for_word(w["text"])
        if not ec:
            continue
        s = float(w["start"])
        e = float(w["end"]) + EMOJI_HOLD_SECONDS
        if plan and s - plan[-1]["end"] < 0.25:
            continue
        # The same picture can come back, but not straight away.
        if s - last_shown.get(ec, -99) < 12.0:
            continue
        last_shown[ec] = e
        plan.append({"kind": "emoji", "emoji": ec, "start": max(0.0, s), "end": e})
        if len(plan) >= 20:
            break
    return plan


def plan_illustration_cues(text, words, model):
    """Ask for a couple of drawable moments from what was actually said."""
    if CUE_STYLE not in ("illustration", "animation") or not words:
        return []
    if IMAGE_PROVIDER == "none":
        return []
    prompt = (
        "Here is a passage from a spoken debate:\n\n"
        f"{text[:1500]}\n\n"
        f"Pick the {CUES_PER_TURN} most vivid concrete moments in it that could be drawn "
        "as a simple illustration. Concrete means a physical thing, place, person or "
        "action, never an abstract idea.\n"
        "For each one give the exact phrase from the passage it belongs to, copied word "
        "for word, a short plain description of the opening picture, and the small "
        "movement that happens over the next few seconds. Keep the movement simple and "
        "physical: one subject doing one thing, camera still.\n"
        'Return ONLY JSON: {"cues": [{"phrase": "...", '
        '"scene": "a person standing under a branch heavy with apples", '
        '"motion": "the person reaches up and picks one apple from the branch"}]}'
    )
    resp = query_openrouter(prompt, model, timeout=40, max_tokens=400, temperature=0.4,
                            system="You return only valid JSON. No commentary.",
                            min_chars=2)
    d = extract_json_object(resp)
    if not d or not isinstance(d.get("cues"), list):
        return []

    spoken = [w["text"].lower().strip(".,;:!?") for w in words]
    plan = []
    for cue in d["cues"][:CUES_PER_TURN]:
        if not isinstance(cue, dict):
            continue
        phrase = str(cue.get("phrase", "")).lower()
        scene = str(cue.get("scene", "")).strip()
        motion = str(cue.get("motion", "")).strip()
        if not scene:
            continue
        keys = [w.strip(".,;:!?") for w in phrase.split() if len(w) > 3]
        idx = None
        for i, w in enumerate(spoken):
            if keys and w == keys[0]:
                idx = i
                break
        if idx is None:
            continue
        s = float(words[idx]["start"])
        span = CUE_CLIP_SECONDS if ANIMATE_CUES else 4.5
        e = min(s + span, float(words[-1]["end"]))
        if any(not (e < p["start"] or s > p["end"]) for p in plan):
            continue
        plan.append({"kind": "illustration", "scene": scene, "motion": motion,
                     "start": max(0.0, s - 0.3), "end": e})
    return plan


def _openai_image(prompt, path):
    r = requests.post(
        "https://api.openai.com/v1/images/generations",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}",
                 "Content-Type": "application/json"},
        json={"model": os.environ.get("OPENAI_IMAGE_MODEL", "gpt-image-1"),
              "prompt": prompt, "size": "1024x1024", "n": 1},
        timeout=180)
    if r.status_code != 200:
        raise RuntimeError(f"{r.status_code}: {r.text[:160]}")
    item = r.json()["data"][0]
    if item.get("b64_json"):
        open(path, "wb").write(base64.b64decode(item["b64_json"]))
    else:
        open(path, "wb").write(requests.get(item["url"], timeout=120).content)


def _replicate_image(prompt, path):
    r = requests.post(
        f"https://api.replicate.com/v1/models/{REPLICATE_IMAGE_MODEL}/predictions",
        headers={"Authorization": f"Bearer {REPLICATE_API_TOKEN}",
                 "Content-Type": "application/json", "Prefer": "wait"},
        json={"input": {"prompt": prompt, "aspect_ratio": "1:1",
                        "output_format": "png", "num_outputs": 1}},
        timeout=180)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"{r.status_code}: {r.text[:160]}")
    out = r.json().get("output")
    url = out[0] if isinstance(out, list) else out
    if not url:
        raise RuntimeError("no image returned")
    open(path, "wb").write(requests.get(url, timeout=120).content)


def drop_white_background(src_path, out_path):
    """Turn the paper white transparent, keeping the ink and the washes."""
    im = Image.open(src_path).convert("RGBA")
    im.thumbnail((CUE_W * 2, CUE_H * 2), Image.LANCZOS)
    px = im.load()
    w, h = im.size
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            m = min(r, g, b)
            if m > 242:
                px[x, y] = (r, g, b, 0)
            elif m > 208:
                px[x, y] = (r, g, b, int(a * (242 - m) / 34))
    im.save(out_path)


def resolve_video_model():
    """Pick a video model the account can actually see, cheapest first.

    Video model ids change often, so rather than hardcode one, ask the models
    API which ones output video and match against a preference order.
    """
    global _VIDEO_MODEL_RESOLVED
    if OPENROUTER_VIDEO_MODEL:
        return OPENROUTER_VIDEO_MODEL
    if _VIDEO_MODEL_RESOLVED is not None:
        return _VIDEO_MODEL_RESOLVED
    ids = []
    try:
        r = requests.get(OPENROUTER_MODELS_URL, headers=openrouter_headers(),
                         params={"output_modalities": "video"}, timeout=30)
        if r.status_code == 200:
            ids = [it.get("id", "") for it in r.json().get("data", []) if it.get("id")]
    except Exception:
        ids = []
    chosen = ""
    for want in VIDEO_MODEL_PREFERENCE:
        for mid in ids:
            if want in mid.lower():
                chosen = mid
                break
        if chosen:
            break
    if not chosen and ids:
        chosen = ids[0]
    _VIDEO_MODEL_RESOLVED = chosen
    if chosen:
        print(f"    Video model: {chosen}")
    else:
        print("    No video generation model available on this account.")
    return chosen


def _openrouter_video(image_path, prompt, out_path):
    """Animate a still via OpenRouter's asynchronous video job API."""
    model = resolve_video_model()
    if not model:
        raise RuntimeError("no video model available")
    with open(image_path, "rb") as fh:
        data_uri = "data:image/png;base64," + base64.b64encode(fh.read()).decode()

    body = {
        "model": model,
        "prompt": prompt,
        "duration": int(round(CUE_CLIP_SECONDS)),
        "aspect_ratio": "1:1",
        "generate_audio": False,
        # The drawn still becomes the opening frame, so the motion continues
        # the illustration rather than inventing a new look.
        "frame_images": [{
            "type": "image_url",
            "image_url": {"url": data_uri},
            "frame_type": "first_frame",
        }],
    }
    r = requests.post(OPENROUTER_VIDEO_URL, headers=openrouter_headers(),
                      json=body, timeout=120)
    if r.status_code not in (200, 201, 202):
        raise RuntimeError(f"{r.status_code}: {r.text[:200]}")
    job = r.json()
    poll_url = job.get("polling_url") or f"{OPENROUTER_VIDEO_URL}/{job.get('id')}"
    status = job.get("status", "pending")

    # Generation takes minutes, so this is a job rather than a request.
    for _ in range(40):
        if status == "completed":
            break
        if status in ("failed", "cancelled", "expired"):
            raise RuntimeError(f"{status}: {str(job.get('error'))[:150]}")
        time.sleep(15)
        pr = requests.get(poll_url, headers=openrouter_headers(), timeout=60)
        if pr.status_code != 200:
            continue
        job = pr.json()
        status = job.get("status", status)
    if status != "completed":
        raise RuntimeError(f"timed out in state {status}")

    urls = job.get("unsigned_urls") or []
    if not urls:
        raise RuntimeError("completed with no video url")
    vid = requests.get(urls[0], headers=openrouter_headers(), timeout=300)
    if vid.status_code != 200 or len(vid.content) < 2000:
        raise RuntimeError(f"download failed ({vid.status_code})")
    open(out_path, "wb").write(vid.content)


def _replicate_video(image_path, prompt, out_path):
    """Animate a still into a short clip with a Replicate image to video model."""
    with open(image_path, "rb") as fh:
        data_uri = "data:image/png;base64," + base64.b64encode(fh.read()).decode()
    payload = {"input": {REPLICATE_VIDEO_IMAGE_KEY: data_uri, "prompt": prompt}}
    r = requests.post(
        f"https://api.replicate.com/v1/models/{REPLICATE_VIDEO_MODEL}/predictions",
        headers={"Authorization": f"Bearer {REPLICATE_API_TOKEN}",
                 "Content-Type": "application/json", "Prefer": "wait"},
        json=payload, timeout=600)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"{r.status_code}: {r.text[:160]}")
    body = r.json()
    out = body.get("output")
    # Some models keep working past the wait window; poll until it settles.
    if not out and body.get("urls", {}).get("get"):
        for _ in range(40):
            time.sleep(6)
            p = requests.get(body["urls"]["get"],
                             headers={"Authorization": f"Bearer {REPLICATE_API_TOKEN}"},
                             timeout=60).json()
            if p.get("status") == "succeeded":
                out = p.get("output")
                break
            if p.get("status") in ("failed", "canceled"):
                raise RuntimeError(f"prediction {p.get('status')}")
    url = out[0] if isinstance(out, list) else out
    if not url:
        raise RuntimeError("no clip returned")
    open(out_path, "wb").write(requests.get(url, timeout=300).content)


def make_cue_clip(scene, motion):
    """A short animated clip for a cue.

    The still is drawn first and handed to the video model as its opening
    frame, so the motion inherits the same drawn style rather than arriving in
    some unrelated look. Returns a path to an mp4, or None.
    """
    if not ANIMATE_CUES or not VIDEO_GEN_OK or VIDEO_PROVIDER == "none":
        return None
    if VIDEO_PROVIDER == "replicate" and not REPLICATE_API_TOKEN:
        return None
    if VIDEO_PROVIDER == "openrouter" and not OPENROUTER_API_KEY:
        return None
    still_raw = make_illustration(scene, keep_raw=True)
    if not still_raw:
        return None
    prompt = (f"{motion or scene}. The drawing comes to life with one small natural "
              f"movement. Camera completely still, plain white background, "
              f"{ILLUSTRATION_STYLE}")
    tag = (OPENROUTER_VIDEO_MODEL or "auto") if VIDEO_PROVIDER == "openrouter" \
        else REPLICATE_VIDEO_MODEL
    key = hashlib.md5(f"{VIDEO_PROVIDER}|{tag}|{prompt}".encode()).hexdigest()[:16]
    cached = os.path.join(ILLUSTRATION_DIR, f"{key}.mp4")
    if not os.path.exists(cached):
        try:
            if VIDEO_PROVIDER == "openrouter":
                _openrouter_video(still_raw, prompt, cached)
            else:
                _replicate_video(still_raw, prompt, cached)
        except Exception as e:
            print(f"    Animation failed ({type(e).__name__}: {str(e)[:120]}); "
                  f"using the still instead.")
            return None
    if not os.path.exists(cached) or os.path.getsize(cached) < 2000:
        return None
    return cached


def make_illustration(scene, keep_raw=False):
    """Draw one cue. Returns a path with alpha, or None if unavailable."""
    global IMAGE_GEN_OK
    if IMAGE_PROVIDER == "none" or not IMAGE_GEN_OK:
        return None
    prompt = f"{scene}. {ILLUSTRATION_STYLE}"
    key = hashlib.md5(f"{IMAGE_PROVIDER}|{prompt}".encode()).hexdigest()[:16]
    cached = os.path.join(ILLUSTRATION_DIR, f"{key}.png")

    raw = os.path.join(ILLUSTRATION_DIR, f"{key}_raw.png")
    if keep_raw and os.path.exists(raw):
        return raw
    if not os.path.exists(cached) or (keep_raw and not os.path.exists(raw)):
        try:
            if IMAGE_PROVIDER == "openai" and OPENAI_API_KEY:
                _openai_image(prompt, raw)
            elif IMAGE_PROVIDER == "replicate" and REPLICATE_API_TOKEN:
                _replicate_image(prompt, raw)
            else:
                IMAGE_GEN_OK = False
                print("    No image provider configured; illustrated cues disabled.")
                return None
        except Exception as e:
            print(f"    Illustration failed ({type(e).__name__}: {str(e)[:100]}); cue dropped.")
            return None
        try:
            drop_white_background(raw, cached)
        except Exception:
            return None
    if keep_raw:
        return raw if os.path.exists(raw) else None
    try:
        if Image.open(cached).size[0] < 32:
            return None
    except Exception:
        return None
    return cached


def prepare_segment(text, slot, display_name, topic, sid, judge_voice_index=None,
                    cue_model=None):
    """Do everything for a segment except the ffmpeg render.

    Audio synthesis gives the exact duration pacing needs, so the whole debate
    can be written and voiced before a single frame is encoded. Rendering is by
    far the most expensive step, and nothing should be rendered until the build
    is known to be sound.
    """
    pos, glow = SLOT_STYLE.get(slot, SLOT_STYLE["MOD"])
    spec = {
        "kind": "segment", "sid": sid, "pos": pos, "glow": glow, "name": display_name,
        "audio": f"audio_{sid}.mp3", "subs": f"subs_{sid}.ass",
        "bg": f"bg_{sid}.png", "ui": f"ui_{sid}.png", "video": f"segment_{sid}.mp4",
    }
    words = generate_audio(text, slot, spec["audio"], judge_voice_index)
    try:
        if CUE_STYLE == "emoji":
            spec["cues"] = create_emoji_plan(words)
        elif CUE_STYLE in ("illustration", "animation"):
            model = cue_model or (AVAILABLE_MODELS[0] if AVAILABLE_MODELS else None)
            spec["cues"] = plan_illustration_cues(text, words, model) if model else []
        else:
            spec["cues"] = []
    except Exception:
        spec["cues"] = []
    generate_subtitles(words, spec["subs"], scorecard=False,
                       audio_file=spec["audio"], full_text=text)
    create_background(pos, glow, spec["bg"])
    spec["wave_box"] = create_ui_overlay(display_name, topic, pos, glow, spec["ui"])
    spec["duration"] = get_audio_duration(spec["audio"])
    return spec


def prepare_scorecard(rn, res, ra, rb, cum_a, cum_b, va, vb, vt, vu, roles):
    """Scoreboard image, spoken summary and subtitles. No render yet."""
    spec = {
        "kind": "scorecard", "sid": f"r{rn}",
        "image": f"scoreboard_r{rn}.png", "audio": f"score_audio_r{rn}.mp3",
        "subs": f"score_subs_r{rn}.ass", "video": f"score_video_r{rn}.mp4",
    }
    generate_scoreboard(rn, res, ra, rb, cum_a, cum_b, spec["image"], roles, va, vb, vt, vu)
    marked = len(res)
    text = (f"Round {rn} is marked. {sentence_case(number_word(marked))} "
            f"{'juror' if marked == 1 else 'jurors'} turned in a score. Of the ones who "
            f"could pick a winner, {spoken_split(va, vb, vt, vu, roles)}. "
            f"Out of a hundred for the arguing, {roles['side_a_label']} got {ra:.0f} "
            f"and {roles['side_b_label']} got {rb:.0f}.")
    words = generate_audio(text, "MOD", spec["audio"])
    generate_subtitles(words, spec["subs"], scorecard=True,
                       audio_file=spec["audio"], full_text=text)
    spec["duration"] = get_audio_duration(spec["audio"])
    return spec


def render_prepared(spec):
    """Encode one prepared segment. This is the expensive half of the build."""
    if spec["kind"] == "scorecard":
        render_scorecard_video(spec["image"], spec["audio"], spec["subs"], spec["video"])
    else:
        render_video_segment(spec["bg"], spec["ui"], spec["audio"], spec["subs"],
                             spec["video"], spec["pos"], spec["glow"],
                             spec["wave_box"], spec["cues"])
    return spec["video"]


def build_intro(topic, roles):
    """Fixed branded opening, one variable line naming tonight's question.

    No juror count here. The panel is picked before the debate, but a juror
    that gets pulled in to write a turn is recused from marking that round,
    and one that returns nothing usable is left off the scorecard, so the
    number spoken here could not be guaranteed by the time the marks come in.
    Each scorecard says how many actually marked that round instead.
    """
    return (
        f"{INTRO_OPENING} "
        f"Tonight's question is this. {topic} "
        f"Arguing {roles['side_a_label']}, on my left. "
        f"Arguing {roles['side_b_label']}, on my right. "
        f"{number_word(ROUNDS)} rounds, equal time, and a jury of AIs marking every one "
        f"of them. "
        f"{INTRO_RULES}"
    )


def verdict_parts(roles, before, after, movement, votes, mean_a, mean_b):
    """The one place that decides who won, so nothing can disagree with it.

    The winner is the side the AIs end up on after hearing both cases. That is
    the question the video asks, so that is what winning means here. Who shifted
    the most opinion and who was marked the better arguer are reported too, but
    neither of them decides it.
    """
    a, b = roles["side_a_label"], roles["side_b_label"]

    if after["lean_a"] > after["lean_b"]:
        winner, w_now, w_was = a, after["lean_a"], (before or {}).get("lean_a")
    elif after["lean_b"] > after["lean_a"]:
        winner, w_now, w_was = b, after["lean_b"], (before or {}).get("lean_b")
    else:
        winner, w_now, w_was = None, max(after["lean_a"], after["lean_b"]), None

    moved = movement["toward_a"] + movement["toward_b"]
    if moved == 0:
        persuader, p_count = None, 0
    elif movement["toward_a"] > movement["toward_b"]:
        persuader, p_count = a, movement["toward_a"]
    elif movement["toward_b"] > movement["toward_a"]:
        persuader, p_count = b, movement["toward_b"]
    else:
        persuader, p_count = None, movement["toward_a"]

    scored = mean_a is not None and mean_b is not None
    if not scored:
        arguer = None
    elif votes["A"] > votes["B"]:
        arguer = a
    elif votes["B"] > votes["A"]:
        arguer = b
    else:
        arguer = None

    return {
        "winner": winner,
        "winner_count": w_now,
        "winner_before": w_was,
        "stated": after.get("stated", 0),
        "persuader": persuader,
        "persuader_count": p_count,
        "moved": moved,
        "arguer": arguer,
        "scored": scored,
        "sweep": bool(winner and persuader == winner and arguer == winner),
    }


def build_outro(mean_a, mean_b, votes_a, votes_b, votes_t, votes_u, roles,
                swing=None, movement=None, after=None, before=None):
    """Closing read. Opens on the winner, closes on the winner.

    The two other results are said in between and said as side notes, because
    neither of them settles the question. Ending on them left the video with no
    clear winner at the point people remember most, which is the last thing
    said, so the last thing said is now the winner.
    """
    counted = votes_a + votes_b + votes_t
    total = counted + votes_u
    votes = {"A": votes_a, "B": votes_b, "TIE": votes_t, "UNSTABLE": votes_u}
    p = verdict_parts(roles, before, after or {"lean_a": 0, "lean_b": 0, "stated": 0},
                      movement or {"toward_a": 0, "toward_b": 0, "unchanged": 0},
                      votes, mean_a, mean_b)
    winner, persuader, arguer = p["winner"], p["persuader"], p["arguer"]

    lines = []
    if after and after.get("stated"):
        if winner:
            lines.append(f"So here is the answer. After hearing both sides, the AIs land "
                         f"on {winner}. {p['winner_count']} of the {p['stated']} of them "
                         f"finished there.")
        else:
            lines.append("So here is the answer, and it is that there isn't one. The AIs "
                         "finished split down the middle, with neither side ahead.")
        # Where that same side started, so the headline shows its own working.
        # Saying both counts side by side sounded like a scoreline and left
        # people working out which number belonged to which side.
        if winner and p["winner_before"] is not None:
            was = p["winner_before"]
            if p["winner_count"] > was:
                lines.append(f"That is up from {was} before a word was said.")
            elif p["winner_count"] < was:
                lines.append(f"That is down from {was} before the debate, so they lost "
                             f"ground and still finished ahead.")
            else:
                lines.append("That is exactly where they started, so the debate moved "
                             "nobody onto that side or off it.")

    lines.append("Two side notes, and neither of them decides it.")

    if persuader and movement:
        if winner and persuader != winner:
            lines.append(f"First, {persuader} shifted the most minds, pulling "
                         f"{p['persuader_count']} of them across, but not enough to take "
                         f"the room.")
        else:
            lines.append(f"First, {persuader} shifted the most minds, bringing "
                         f"{p['persuader_count']} of them round.")
    elif p["moved"] == 0:
        lines.append("First, nobody shifted anybody. Not one of them changed its mind.")
    else:
        lines.append(f"First, neither side shifted more minds than the other. "
                     f"{sentence_case(number_word(p['moved']))} of them moved, and they "
                     f"moved both ways.")

    if not p["scored"]:
        lines.append("Second, on the arguing itself we have no marks at all. Not one juror "
                     "gave us a usable answer, and we would rather leave it blank than "
                     "invent a number.")
    elif arguer:
        # Report the count of jurors, not the two averages, which are often a
        # point apart and read as a tie when rounded.
        won = votes_a if arguer == roles["side_a_label"] else votes_b
        lost = votes_b if arguer == roles["side_a_label"] else votes_a
        caveat = ("" if arguer != winner else
                  " That is about how it was argued, not about who is right.")
        lines.append(f"And second, the jury marked {arguer} the better arguer, {won} to "
                     f"{lost}.{caveat}")
    else:
        lines.append("And second, on the arguing itself, the jury could not split them.")

    if p["scored"] and votes_u and counted <= total / 2:
        lines.append(f"Only {counted} of our {total} markings held up when we ran them "
                     f"the other way round, so take that second one with a pinch of salt.")

    # The last thing said is who won, because that is the thing people take
    # away from the end of a video.
    if not winner:
        lines.append("So nobody wins this one. The AIs finished split, and neither side "
                     "could take the room.")
    elif p["sweep"]:
        lines.append(f"So the winner tonight is {winner}, and it is a clean sweep. It took "
                     f"the room, it shifted the most minds, and the jury marked it the "
                     f"better arguer.")
    elif arguer and arguer != winner:
        lines.append(f"So the winner tonight is {winner}. {arguer} was marked the better "
                     f"arguer, but arguing well and being believed are two different "
                     f"things, and {winner} is where the AIs ended up.")
    elif persuader and persuader != winner:
        lines.append(f"So the winner tonight is {winner}. {persuader} pulled more of them "
                     f"across, but not enough, and {winner} is still where the AIs ended "
                     f"up.")
    else:
        lines.append(f"So the winner tonight is {winner}. That is where the AIs ended up "
                     f"once they had heard both sides.")

    lines.append(OUTRO_SIGNOFF)
    return " ".join(lines)


def build_method_note(topic, roles, debaters, judges, poll_roster,
                      sum_before, sum_after, movement, votes, mean_a=None, mean_b=None):
    """A description-ready statement of what this run actually did."""
    labs = sorted({provider_from_model(m) for m in poll_roster})
    swing = sum_after["mean"] - sum_before["mean"]
    moved = movement["toward_a"] + movement["toward_b"]

    # The headline, taken from the same helper the video uses so the
    # description cannot name a different winner.
    p = verdict_parts(roles, sum_before, sum_after, movement, votes, mean_a, mean_b)
    if p["winner"]:
        landed = (f"WINNER: {p['winner']} - this is where the AIs landed once they had "
                  f"heard both sides. {p['winner_count']} of the {p['stated']} that "
                  f"answered finished on that side, against {p['winner_before']} before "
                  f"the debate.")
        if p["sweep"]:
            landed += (" A clean sweep: it also shifted the most minds and was marked "
                       "the better arguer.")
    else:
        landed = ("WINNER: nobody. The AIs finished evenly split, with neither side "
                  "ahead.")
    lines = [
        f"QUESTION: {topic}",
        "",
        f"Arguing {roles['side_a_label']} and {roles['side_b_label']}: "
        f"{get_judge_short_name(debaters[0])} and {get_judge_short_name(debaters[1])}, "
        f"swapping sides each round.",
        f"Scoring the arguing: {len(judges)} models "
        f"({', '.join(get_judge_short_name(j) for j in judges)}).",
        f"Polled for their own position: {len(poll_roster)} models, one from each of "
        f"{len(labs)} labs ({', '.join(labs)}).",
        "",
        "RESULT",
        # The headline first, in the same words the video uses, so the
        # description does not read as three competing results either.
        landed,
        "Everything below is detail. The two results that follow, who shifted the most "
        "opinion and who the jury marked as the better arguer, do not decide the winner.",
        "",
        f"Before the debate the panel sat at {sum_before['mean']:+.2f} on a scale of -5 to "
        f"+5 ({sum_before['lean_a']} leaning {roles['side_a_label']}, "
        f"{sum_before['lean_b']} leaning {roles['side_b_label']}, "
        f"{sum_before['undecided']} undecided, {sum_before['declined']} declined).",
        f"After reading the debate it sat at {sum_after['mean']:+.2f}, a shift of "
        f"{swing:+.2f}.",
        f"{moved} model(s) changed position: {movement['toward_a']} toward "
        f"{roles['side_a_label']}, {movement['toward_b']} toward "
        f"{roles['side_b_label']}, {movement['unchanged']} unmoved."
        + (f" Crossed sides: {', '.join(movement['crossed'])}." if movement["crossed"] else ""),
        (f"On argument quality the panel split {votes['A']}-{votes['B']}"
         + (f" with {votes['TIE']} even" if votes["TIE"] else "")
         + (f"; {votes['UNSTABLE']} judgement(s) discarded as order driven."
            if votes["UNSTABLE"] else ".")
         + (f" Average {mean_a:.1f} vs {mean_b:.1f} out of 100."
            if mean_a is not None else "")
         ) if mean_a is not None else
        "On argument quality there is no result: no judge returned a usable verdict, and "
        "no score was invented to fill the gap.",
        "",
        "METHOD",
    ]
    lines += [f"- {c}" for c in METHOD_CLAIMS]
    lines += ["", "WHAT THIS DOES NOT SHOW"]
    lines += [f"- {c}" for c in METHOD_LIMITS]
    return "\n".join(lines)


def stitch_segments(segs, out):
    lf = "concat_list.txt"
    lines = [f"file '{os.path.abspath(s).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'"
             for s in segs]
    open(lf, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lf, "-c", "copy", out]
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if r.returncode != 0:
        print(r.stderr[-7000:])
        raise RuntimeError("Concat failed")


class Pacing:
    """Keeps the finished video inside the 10 to 15 minute window.

    Each debate turn is sized from the time actually left in the budget, using a
    speaking rate measured from the audio rendered so far.
    """

    def __init__(self):
        self.consumed = 0.0
        self.spoken_words = 0
        self.spoken_seconds = 0.0

    def add(self, seconds, words=None):
        self.consumed += float(seconds or 0.0)
        if words:
            self.spoken_words += words
            self.spoken_seconds += float(seconds or 0.0)

    @property
    def wps(self):
        if self.spoken_seconds > 20 and self.spoken_words > 60:
            return max(1.8, min(3.4, self.spoken_words / self.spoken_seconds))
        return DEFAULT_WORDS_PER_SEC

    def turn_words(self, turns_left, reserved_seconds):
        turns_left = max(1, turns_left)
        available = TARGET_TOTAL_SECONDS - self.consumed - reserved_seconds
        per_turn = available / turns_left
        return int(max(MIN_TURN_WORDS, min(MAX_TURN_WORDS, per_turn * self.wps)))


def strongest_for_lab(lab, available, exclude=()):
    """The best model this account can see from one lab, by the tier list."""
    lab_models = [m for m in available
                  if m.split("/", 1)[0].lower() == lab and m not in exclude]
    if not lab_models:
        return None
    for fragment in STRONGEST_BY_LAB.get(lab, []):
        for m in lab_models:
            if fragment in m.split("/", 1)[1].lower():
                return m
    return sorted(lab_models)[0]


def resolve_roster(available):
    """Pick the strongest model per lab, then split into debaters and panel.

    Explicit DEBATER_MODELS or PANEL_MODELS always win. Otherwise the two
    debate labs supply the arguers and every other lab supplies one judge, so
    the panel stays one seat per lab without any lab being counted twice.
    """
    usable = [m for m in available if not is_reasoning_model(m)]
    chosen = {}
    for lab in STRONGEST_BY_LAB:
        best = strongest_for_lab(lab, usable)
        if best:
            chosen[lab] = best

    debaters = [m.strip() for m in os.environ.get("DEBATER_MODELS", "").split(",") if m.strip()]
    if not debaters:
        debaters = [chosen[lab] for lab in DEBATER_LABS if lab in chosen]
        # If a preferred debate lab is missing, borrow the next best lab.
        for lab, m in chosen.items():
            if len(debaters) >= 2:
                break
            if m not in debaters:
                debaters.append(m)
    debaters = debaters[:2]

    panel = [m.strip() for m in os.environ.get("PANEL_MODELS", "").split(",") if m.strip()]
    if not panel:
        debater_labs = {m.split("/", 1)[0].lower() for m in debaters}
        panel = [m for lab, m in chosen.items() if lab not in debater_labs]
    return debaters, panel[:MAX_JUDGES]


def pin_panel(models, debaters):
    """Use the configured panel as given, minus anything arguing the debate."""
    global JUDGE_VOICE_MAP
    debater_providers = {provider_from_model(m) for m in debaters}
    panel, seen = [], set()
    for m in models:
        if m in debaters or is_reasoning_model(m):
            continue
        if provider_from_model(m) in debater_providers:
            print(f"  {get_judge_short_name(m)} shares a provider with a debater; "
                  f"left off the panel.")
            continue
        if m in seen:
            continue
        seen.add(m)
        panel.append(m)
        if len(panel) >= MAX_JUDGES:
            break
    JUDGE_VOICE_MAP = {mid: idx % len(JUDGE_VOICES) for idx, mid in enumerate(panel)}
    return panel


def print_cost_estimate(panel_size):
    """Rough spend for one build, printed before anything is generated."""
    if not USE_PAID_MODELS:
        print("Using free models; no OpenRouter spend.")
        return
    turns = ROUNDS * TURNS_PER_SIDE_PER_ROUND * 2
    judge_calls = panel_size * 2 * ROUNDS          # two order passes per judge
    poll_calls = panel_size * 2 * 2                # pre and post, both scale directions
    tokens_in = (turns * 700 + judge_calls * 1150 + poll_calls * 1200
                 + ROUNDS * 2 * 850 + 2000)
    tokens_out = turns * 220 + judge_calls * 130 + poll_calls * 120 + ROUNDS * 2 * 160
    cost = (tokens_in / 1e6) * PRICE_PER_M["in"] + (tokens_out / 1e6) * PRICE_PER_M["out"]
    chars = int(tokens_out * 4.2)
    print(f"Estimated spend: about {tokens_in/1000:.0f}k input and {tokens_out/1000:.0f}k "
          f"output tokens, roughly ${cost:.2f} at {PRICE_PER_M['in']:.0f} and "
          f"{PRICE_PER_M['out']:.0f} dollars per million. Adjust PRICE_PER_M for your roster.")
    if TTS_PROVIDER == "elevenlabs":
        print(f"Plus roughly {chars/1000:.0f}k ElevenLabs characters for the narration.")
    if ANIMATE_CUES and VIDEO_PROVIDER != "none":
        clips = turns * CUES_PER_TURN
        secs = clips * CUE_CLIP_SECONDS
        print(f"Plus up to {clips} animated cues, about {secs:.0f} seconds of generated "
              f"video. Video is billed per second and is normally the largest line on "
              f"the bill: at 5 cents a second that is roughly ${secs * 0.05:.2f}, at 40 "
              f"cents a second roughly ${secs * 0.40:.2f}. Lower CUES_PER_TURN or "
              f"CUE_CLIP_SECONDS to cut it.")


def verify_voices():
    """Check every configured voice exists, and that none code-switch.

    A Multilingual voice decides the language phrase by phrase, which is how
    English lines ended up part spoken in French. Any that slip in are replaced,
    as is any name the service does not recognise.
    """
    global SIDE_A_VOICE, SIDE_B_VOICE, MODERATOR_VOICE, JUDGE_VOICES
    try:
        available = {v["ShortName"] for v in asyncio.run(edge_tts.list_voices())}
    except Exception as e:
        print(f"Could not list voices ({type(e).__name__}); using them as configured.")
        available = set()

    def ok(name):
        if "multilingual" in name.lower():
            return False
        return not available or name in available

    spare = [v for v in VOICE_FALLBACKS if ok(v)] or VOICE_FALLBACKS
    swapped = []

    def fix(name, taken):
        if ok(name):
            return name
        for cand in spare:
            if cand not in taken:
                swapped.append(f"{name} -> {cand}")
                return cand
        return spare[0]

    taken = set()
    SIDE_A_VOICE = fix(SIDE_A_VOICE, taken); taken.add(SIDE_A_VOICE)
    SIDE_B_VOICE = fix(SIDE_B_VOICE, taken); taken.add(SIDE_B_VOICE)
    MODERATOR_VOICE = fix(MODERATOR_VOICE, taken); taken.add(MODERATOR_VOICE)
    fixed_judges = []
    for v in JUDGE_VOICES:
        nv = fix(v, taken)
        taken.add(nv)
        fixed_judges.append(nv)
    JUDGE_VOICES = fixed_judges

    if swapped:
        print("Voice substitutions: " + ", ".join(swapped))
    else:
        print("Voices: all present and English only.")


def preflight_environment():
    """Prove the toolchain works before any content is generated.

    Every check here fails in seconds. Without them, a missing codec or a
    blocked TTS endpoint only shows up once the build has already spent real
    time on model calls.
    """
    for tool in ("ffmpeg", "ffprobe"):
        try:
            r = subprocess.run([tool, "-version"], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, timeout=20)
            if r.returncode != 0:
                raise OSError
        except Exception:
            raise DebateGenerationError(
                f"{tool} is not available. Install ffmpeg before running the pipeline.")

    if not os.path.exists("background.png"):
        print("No background.png; the generated gradient backdrop will be used.")

    font = load_font(40, bold=True)
    if not isinstance(font, ImageFont.FreeTypeFont):
        print("DejaVu fonts not found; text will fall back to a bitmap font.")

    verify_voices()

    # Text to speech: the network dependency most likely to be blocked.
    probe_audio = "preflight_probe.mp3"
    try:
        words = generate_audio("Preflight check.", "MOD", probe_audio)
    except Exception as e:
        raise DebateGenerationError(
            f"Text to speech failed: {type(e).__name__}: {e}. edge-tts cannot reach "
            f"Microsoft's endpoint, so no audio can be produced.")
    dur = get_audio_duration(probe_audio)
    if dur <= 0 or not words:
        raise DebateGenerationError("Text to speech produced no usable audio in preflight.")

    # A real render, in miniature, to catch filtergraph and codec problems.
    try:
        box = create_ui_overlay("PREFLIGHT", "preflight", "left", "#00FFCC", "preflight_ui.png")
        create_background("left", "#00FFCC", "preflight_bg.png")
        generate_subtitles(words, "preflight_subs.ass", audio_file=probe_audio)
        render_video_segment("preflight_bg.png", "preflight_ui.png", probe_audio,
                             "preflight_subs.ass", "preflight_seg.mp4",
                             "left", "#00FFCC", box, [])
    except Exception as e:
        raise DebateGenerationError(
            f"Rendering a one segment smoke test failed: {type(e).__name__}: {e}")
    finally:
        for f in ("preflight_probe.mp3", "preflight_ui.png", "preflight_bg.png",
                  "preflight_subs.ass", "preflight_seg.mp4"):
            try:
                os.remove(f)
            except OSError:
                pass
    print("Preflight: ffmpeg, fonts, text to speech and rendering all OK.")


def preflight_models(models, judges_needed):
    """Confirm enough models actually answer, before generating anything.

    Checks a debater model and, in parallel, that at least `judges_needed`
    distinct judges respond, so a panel that cannot be assembled fails now
    rather than after the first round has been written and voiced.
    """
    responded = []

    def probe(m):
        return m, query_openrouter(
            "Reply with exactly the word: ready",
            m, timeout=30, max_tokens=20, temperature=0,
            system="You reply with one word.", min_chars=2)

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(models))) as ex:
        for f in concurrent.futures.as_completed([ex.submit(probe, m) for m in models[:8]]):
            try:
                m, resp = f.result()
            except Exception:
                continue
            if resp:
                responded.append(m)

    if not responded:
        raise DebateGenerationError(
            "No model answered a trivial preflight prompt. Check OPENROUTER_API_KEY and that "
            "the free models in FALLBACK_MODELS are still available. Stopping before anything "
            "is generated or rendered.")
    if len(responded) < judges_needed:
        raise DebateGenerationError(
            f"Only {len(responded)} model(s) answered preflight, but a real panel needs at "
            f"least {judges_needed}. Responding: "
            f"{', '.join(get_judge_short_name(m) for m in responded)}. Stopping now rather "
            f"than reaching the first scorecard and failing there.")
    print(f"Preflight: {len(responded)} models responding "
          f"({', '.join(get_judge_short_name(m) for m in responded)}).")
    return responded


def run_debate_pipeline():
    global USED_ARGUMENTS, USED_JUDGE_EXPLANATIONS
    USED_ARGUMENTS = set()
    USED_JUDGE_EXPLANATIONS = set()
    cleanup_cache()
    if not OPENROUTER_API_KEY:
        raise DebateGenerationError("OPENROUTER_API_KEY missing")
    preflight_environment()
    if not os.path.exists("topic.txt"):
        open("topic.txt", "w", encoding="utf-8").write("Does God exist?")
    topic = open("topic.txt", "r", encoding="utf-8").read().strip() or "Does God exist?"
    print(f"\nTOPIC FROM topic.txt: {topic}\n")

    global AVAILABLE_MODELS
    catalogue = discover_models() or FALLBACK_MODELS.copy()
    resolved_debaters, resolved_panel = [], []
    if USE_PAID_MODELS:
        resolved_debaters, resolved_panel = resolve_roster(catalogue)
        avail = resolved_debaters + resolved_panel
        print(f"Roster resolved from {len(catalogue)} available models:")
        for m in resolved_debaters:
            print(f"  debater  {get_judge_short_name(m):16} {m}")
        for m in resolved_panel:
            print(f"  panel    {get_judge_short_name(m):16} {m}")
    else:
        avail = catalogue
    AVAILABLE_MODELS = [m for m in avail if not is_reasoning_model(m)]
    print_cost_estimate(len(resolved_panel) if USE_PAID_MODELS else MAX_JUDGES)
    preflight_models(AVAILABLE_MODELS or avail, MIN_PANEL_SIZE)
    if len(resolved_debaters) >= 2:
        ap_model, sk_model = resolved_debaters[0], resolved_debaters[1]
    else:
        ap_model, sk_model = choose_primary_models(AVAILABLE_MODELS or avail)
    roles = get_debate_roles(topic, ap_model)
    print(f"Sides: {roles['side_a_label']} (Brian, left) vs {roles['side_b_label']} (Ava, right)")
    print(f"  A stance: {roles['side_a_stance']}")
    print(f"  B stance: {roles['side_b_stance']}")
    if resolved_panel:
        judges = pin_panel(resolved_panel, (ap_model, sk_model))
    else:
        judges = choose_judges(AVAILABLE_MODELS or avail, (ap_model, sk_model))
    if len(judges) < MIN_PANEL_SIZE:
        raise DebateGenerationError(
            f"Only {len(judges)} usable judge model(s) were found. A scorecard needs a real "
            f"panel, and padding it with invented scores is exactly what this build refuses "
            f"to do."
        )
    global JUDGE_MODELS
    JUDGE_MODELS = set(judges)
    print(f"Judges: {', '.join(get_judge_short_name(j) for j in judges)}")
    print(f"Debaters: {roles['side_a_label']} = {ap_model}, "
          f"{roles['side_b_label']} = {sk_model}")

    pacing = Pacing()
    specs = []
    sid = 0

    def add_seg(text, slot, display_name, jvi=None, count_speech=True):
        """Write and voice a segment. Rendering happens later, in one pass."""
        nonlocal sid
        spec = prepare_segment(text, slot, display_name, topic, sid, jvi)
        specs.append(spec)
        sid += 1
        pacing.add(spec["duration"], count_words(text) if count_speech else None)
        return spec["duration"]

    total_turns = ROUNDS * TURNS_PER_SIDE_PER_ROUND * 2
    turns_done = 0

    add_seg(build_intro(topic, roles), "MOD", "MODERATOR", count_speech=False)

    # Where the panel stands before hearing a word. This is the consensus
    # measurement; the round scorecards measure who argued better.
    poll_extra = []
    if USE_PAID_MODELS:
        seen_labs = {provider_from_model(m) for m in list(judges) + [ap_model, sk_model]}
        for lab in STRONGEST_BY_LAB:
            best = strongest_for_lab(lab, catalogue)
            if best and provider_from_model(best) not in seen_labs:
                poll_extra.append(best)
                seen_labs.add(provider_from_model(best))
    poll_roster = build_poll_roster((ap_model, sk_model), list(judges) + poll_extra)
    print(f"\nOpening poll: {len(poll_roster)} models from "
          f"{len({provider_from_model(m) for m in poll_roster})} labs, asked cold.")
    poll_before = poll_panel(poll_roster, topic, roles)
    if not poll_before:
        print("  No model answered the opening poll; the consensus segments are skipped "
              "for this build.")
    sum_before = poll_summary(poll_before)
    for r in poll_before:
        stance = "declined" if r["declined"] else f"{r['position']:+.1f}"
        print(f"    {r['display_name']}: {stance}")
    print(f"  Opening: {describe_poll(sum_before, roles)}; mean {sum_before['mean']:+.2f}")
    specs.append(prepare_poll_segment(
        poll_before, sum_before, roles,
        build_opening_poll_narration(sum_before, roles, topic),
        "opening", "WHAT THE AIs THINK - BEFORE THE DEBATE"))
    pacing.add(specs[-1]["duration"])

    cum_a = cum_b = 0.0
    all_results = []
    last_a_text = ""
    last_b_text = ""

    turn_attribution = []
    turn_transcript = []
    votes_a = votes_b = votes_t = votes_u = 0
    scored_rounds = 0
    for rn in range(1, ROUNDS + 1):
        print(f"\n--- ROUND {rn} ---")
        a_turns, b_turns = [], []
        round_writers = set()

        # Swap which model argues which side each round, so a stronger model
        # cannot systematically carry one position.
        if rn % 2 == 0:
            a_model, b_model = sk_model, ap_model
        else:
            a_model, b_model = ap_model, sk_model
        # Alternate who opens, so the last word of a round is not always one
        # side's advantage.
        speaking_order = ("A", "B") if rn % 2 == 1 else ("B", "A")
        print(f"  {roles['side_a_label']} argued by {get_judge_short_name(a_model)}, "
              f"{roles['side_b_label']} argued by {get_judge_short_name(b_model)}; "
              f"{roles['side_a_label' if speaking_order[0] == 'A' else 'side_b_label']} opens")

        for tn in range(1, TURNS_PER_SIDE_PER_ROUND + 1):
            opener_words = None
            for side in speaking_order:
                turns_left = total_turns - turns_done
                rounds_left = ROUNDS - rn + 1
                reserved = (rounds_left * EST_SCORECARD_SEC
                            + rounds_left * 2 * EST_COMMENTARY_SEC
                            + EST_POLL_SEC + EST_OUTRO_SEC)
                target = pacing.turn_words(turns_left, reserved)
                if opener_words is not None:
                    # Answer at the length the other side actually spoke, so
                    # neither side gains from simply saying more.
                    target = max(MIN_ACCEPTABLE_TURN_WORDS,
                                 min(MAX_TURN_WORDS, opener_words))
                opponent_last = last_b_text if side == "A" else last_a_text
                model = a_model if side == "A" else b_model
                text, wrote = generate_turn(topic, roles, side, rn, tn, opponent_last,
                                            target, model)
                label = roles["side_a_label"] if side == "A" else roles["side_b_label"]
                round_writers.add(wrote)
                turn_attribution.append({"round": rn, "turn": tn, "side": label,
                                         "model": wrote, "words": count_words(text)})
                turn_transcript.append({"side": label, "text": text})
                substitute = " (substitute)" if wrote != model else ""
                print(f"  R{rn} T{tn} {label}: {count_words(text)}w / target {target}w "
                      f"- written by {get_judge_short_name(wrote)} [{wrote}]{substitute}")
                if side == "A":
                    a_turns.append(text)
                    last_a_text = text
                else:
                    b_turns.append(text)
                    last_b_text = text
                if opener_words is None:
                    opener_words = count_words(text)
                add_seg(text, side, label)
                turns_done += 1

        a_full = "\n\n".join(a_turns)
        b_full = "\n\n".join(b_turns)

        # Judges reward longer answers, so an unequal share of speaking is a
        # thumb on the scale even when the judging itself is blind.
        wa, wb = count_words(a_full), count_words(b_full)
        gap = abs(wa - wb) / max(1, max(wa, wb))
        flag = "  <-- uneven, favours the longer side" if gap > 0.25 else ""
        print(f"  speaking length: {roles['side_a_label']} {wa}w vs "
              f"{roles['side_b_label']} {wb}w ({gap:.0%} apart){flag}")

        res = evaluate_round(judges, topic, rn, a_full, b_full, roles,
                             recused=round_writers)
        if not res:
            # Unscored: no scorecard, no reactions, nothing added to the totals.
            all_results.append({"round": rn, "scored": False,
                                "words": {"A": wa, "B": wb, "gap": round(gap, 3)},
                                "debater_models": {"A": a_model, "B": b_model},
                                "judges": []})
            print(f"  Round {rn} goes unscored; the debate continues.")
            continue
        scored_rounds += 1
        ra, rb = calculate_round_average(res)
        cum_a += ra
        cum_b += rb
        va, vb, vt, vu = round_votes(res)
        votes_a += va
        votes_b += vb
        votes_t += vt
        votes_u += vu
        all_results.append({"round": rn, "scored": True, "avg_a": ra, "avg_b": rb,
                            "votes": {"A": va, "B": vb, "TIE": vt, "UNSTABLE": vu},
                            "words": {"A": wa, "B": wb, "gap": round(gap, 3)},
                            "debater_models": {"A": a_model, "B": b_model},
                            "judges": res})
        print(f"  Round {rn}: panel split {va}-{vb}"
              f"{f' with {vt} even' if vt else ''}"
              f"{f', {vu} abstaining' if vu else ''}; "
              f"average {roles['side_a_label']} {ra:.1f} vs {roles['side_b_label']} {rb:.1f}")

        score_spec = prepare_scorecard(rn, res, ra, rb, cum_a, cum_b, va, vb, vt, vu, roles)
        specs.append(score_spec)
        pacing.add(score_spec["duration"])

        # One judge from each camp explains the round, but only where that camp exists.
        camps = []
        a_favs = [r for r in res if r["winner"] == "A"]
        b_favs = [r for r in res if r["winner"] == "B"]
        if a_favs:
            camps.append(("A", a_favs))
        if b_favs:
            camps.append(("B", b_favs))

        used_providers = set()
        for side, camp in camps:
            label = roles["side_a_label"] if side == "A" else roles["side_b_label"]
            # Try several jurors from this camp: one giving a poor reaction is
            # no reason to lose the segment when others agreed with it.
            candidates = [r for r in camp if r["provider"] not in used_providers] or camp
            random.shuffle(candidates)
            for judge in candidates[:3]:
                text = generate_panel_commentary(judge["model"], side, topic, rn,
                                                 a_full, b_full, roles)
                if not text:
                    continue
                used_providers.add(judge["provider"])
                jvi = JUDGE_VOICE_MAP.get(judge["model"], 0)
                name = (f"JUDGE — {judge['display_name'].upper()} "
                        f"({judge['provider'].upper()})")
                print(f"  reaction for {label} by {judge['display_name']} "
                      f"[{judge['model']}]")
                add_seg(text, "JUDGE", name, jvi=jvi, count_speech=False)
                break
            else:
                print(f"  no juror on the {label} side gave a usable reaction; "
                      f"segment skipped.")
        print(f"  written and voiced so far: {pacing.consumed / 60:.1f} min")

    # The same question, to the same models, now having read the whole thing.
    print("\nClosing poll: asking the same panel again, with the transcript.")
    transcript = "\n\n".join(
        f"{t['side']}: {t['text']}" for t in turn_transcript)
    poll_after = poll_panel(poll_roster, topic, roles, transcript=transcript)
    sum_after = poll_summary(poll_after) if poll_after else sum_before
    for r in poll_after:
        stance = "declined" if r["declined"] else f"{r['position']:+.1f}"
        print(f"    {r['display_name']}: {stance}")
    swing = sum_after["mean"] - sum_before["mean"]
    print(f"  Closing: {describe_poll(sum_after, roles)}; mean {sum_after['mean']:+.2f} "
          f"(swing {swing:+.2f})")
    movement = poll_movement(poll_before, poll_after)
    print(f"  Movement: {movement['toward_a']} toward {roles['side_a_label']}, "
          f"{movement['toward_b']} toward {roles['side_b_label']}, "
          f"{movement['unchanged']} unmoved"
          + (f"; crossed sides: {', '.join(movement['crossed'])}" if movement["crossed"] else ""))
    specs.append(prepare_poll_segment(
        poll_after, sum_after, roles,
        build_closing_poll_narration(sum_before, sum_after, roles, movement),
        "closing", "WHAT THE AIs THINK - AFTER THE DEBATE", before=sum_before))
    pacing.add(specs[-1]["duration"])

    print(f"\nPanel consensus across all rounds: {votes_a} to {votes_b}"
          f"{f' with {votes_t} even' if votes_t else ''}"
          f"{f', {votes_u} abstained as order driven' if votes_u else ''}.")
    votes = {"A": votes_a, "B": votes_b, "TIE": votes_t, "UNSTABLE": votes_u}
    mean_a = cum_a / scored_rounds if scored_rounds else None
    mean_b = cum_b / scored_rounds if scored_rounds else None
    if scored_rounds < ROUNDS:
        print(f"\n{ROUNDS - scored_rounds} of {ROUNDS} rounds went unscored.")
    outro_text = build_outro(mean_a, mean_b, votes_a, votes_b, votes_t, votes_u, roles,
                             swing=swing, movement=movement, after=sum_after,
                             before=sum_before)
    specs.append(prepare_verdict_segment(
        outro_text,
        (topic, roles, sum_before, sum_after, movement, votes, mean_a, mean_b)))
    pacing.add(specs[-1]["duration"])

    method_note = build_method_note(
        topic, roles, (ap_model, sk_model), judges, poll_roster,
        sum_before, sum_after, movement, votes, mean_a, mean_b)
    try:
        open("video_description.txt", "w", encoding="utf-8").write(method_note + "\n")
        print("\nWrote video_description.txt (paste under the video).")
    except OSError:
        pass

    try:
        json.dump({"topic": topic, "sides": roles,
                   "method_note": method_note,
                   "debater_models": {"A": ap_model, "B": sk_model},
                   "judge_models": judges,
                   "turns": turn_attribution,
                   "rounds": all_results,
                   "cumulative": {"A": round(cum_a, 2), "B": round(cum_b, 2)},
                   "consensus_votes": {"A": votes_a, "B": votes_b, "TIE": votes_t,
                                       "UNSTABLE": votes_u},
                   "poll": {"roster": poll_roster,
                            "movement": movement,
                            "before": {"summary": sum_before, "models": poll_before},
                            "after": {"summary": sum_after, "models": poll_after},
                            "swing": round(sum_after["mean"] - sum_before["mean"], 3)}},
                  open("scores.json", "w", encoding="utf-8"), indent=2)
    except Exception:
        pass

    # Everything above is cheap and can fail. Only now, with the full debate
    # written, judged and voiced, do we spend time encoding video.
    planned = pacing.consumed
    print(f"\nDebate complete: {len(specs)} segments, {planned / 60:.1f} min of audio.")
    if planned < MIN_TOTAL_SECONDS:
        print(f"NOTE: runtime is under the {MIN_TOTAL_SECONDS / 60:.0f} minute floor.")
    elif planned > MAX_TOTAL_SECONDS:
        print(f"NOTE: runtime is over the {MAX_TOTAL_SECONDS / 60:.0f} minute ceiling.")
    print("Rendering now; this is the slow part.")

    segs = []
    for idx, spec in enumerate(specs, 1):
        segs.append(render_prepared(spec))
        print(f"  rendered {idx}/{len(specs)} ({spec['duration']:.0f}s)")

    stitch_segments(segs, OUTPUT_FILE)
    final = get_audio_duration(OUTPUT_FILE) or pacing.consumed
    print(f"\nCOMPLETE: {OUTPUT_FILE}  runtime {int(final // 60)}m {int(final % 60)}s")
    cleanup_cache()


if __name__ == "__main__":
    try:
        run_debate_pipeline()
    except KeyboardInterrupt:
        print("Cancelled")
    except Exception as e:
        print("FAILED")
        print(str(e))
        import traceback
        traceback.print_exc()
        raise
