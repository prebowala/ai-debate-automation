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
MAX_JUDGES = 11

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
EST_OUTRO_SEC = 38.0
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
SIDE_A_VOICE = "en-US-BrianMultilingualNeural"
SIDE_B_VOICE = "en-US-AvaMultilingualNeural"
MODERATOR_VOICE = "en-US-AndrewMultilingualNeural"

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
USE_PAID_MODELS = os.environ.get("USE_PAID_MODELS", "").strip() not in ("", "0", "false", "no")

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
# run. Unknown models fall back to the default and are marked approximate.
PRICE_PER_M = {"in": 3.0, "out": 12.0}

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
CHANNEL_NAME = "the AI Debate Arena"
INTRO_OPENING = (
    "Welcome to the AI Debate Arena, where rival artificial intelligences argue opposite "
    "sides of one question, and a separate panel of AI judges decides who actually made "
    "the better case."
)
INTRO_RULES = (
    "The models swap sides every round, and the judges score blind, without being told which "
    "side is which. And before we start, we asked every judge where it already stands, so at "
    "the end we can see exactly who changed their mind."
)
# ---------------------------------------------------------------------------
# What this video actually measures. Every clause here is something the
# pipeline genuinely does; nothing in it is aspirational. build_method_note()
# fills in the real numbers from the run and writes it to video_description.txt
# so the claim published with the video matches the claim the code can support.
# ---------------------------------------------------------------------------
METHOD_CLAIMS = [
    "Two AI models argue assigned sides of the question. They are told which side to "
    "take, so their arguments are not their own views. They swap sides every round, so "
    "neither position is carried by whichever model is stronger.",

    "Argument quality is scored by a separate panel. Judges never see which side is "
    "which, they score every round twice with the two sides swapped, and any judge whose "
    "verdict reverses when the order is swapped is dropped from the count rather than "
    "averaged in.",

    "Separately, one model from each participating lab states its own position on the "
    "question, before the debate and again after reading the full transcript. Each model "
    "is asked twice with the scale reversed. That before and after change is the headline "
    "result.",

    "Nothing is scripted or filled in. If a model returns no usable answer, it is left "
    "out and reported as absent. No score, argument or verdict in this video was written "
    "by a human or generated as placeholder text.",
]

METHOD_LIMITS = [
    "This measures how a specific set of models responded to a specific debate. It is "
    "not a measure of whether the position is true.",

    "These models are not independent voices. They are trained on overlapping material "
    "and tuned in similar ways, so agreement between them is weaker evidence than the "
    "number of models suggests.",

    "A model declining to take a position is reported as declining. It is never counted "
    "as agreement or filled in.",
]

OUTRO_SIGNOFF = (
    "Scored blind, with the sides reversed, and any judge that changed its mind on the running "
    "order thrown out. Drop the question you want settled next in the comments, subscribe, and "
    "we will see you in the ring."
)

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
    low = (mid or "").lower()
    if "o4-mini" in low: return "o4-mini"
    if "o3" in low: return "o3"
    if "gpt" in low and "mini" in low: return "GPT-4o mini"
    if "gpt" in low: return "ChatGPT"
    if "claude" in low and "opus" in low: return "Claude Opus"
    if "claude" in low and "sonnet" in low: return "Claude Sonnet"
    if "claude" in low and "haiku" in low: return "Claude Haiku"
    if "claude-3-5" in low: return "Claude 3.5"
    if "claude" in low: return "Claude"
    if "gemini" in low and "pro" in low: return "Gemini Pro"
    if "gemini" in low and "flash" in low: return "Gemini Flash"
    if "gemini-2.0" in low: return "Gemini 2.0"
    if "gemini" in low: return "Gemini"
    if "deepseek" in low and "r1" in low: return "DeepSeek R1"
    if "deepseek" in low: return "DeepSeek"
    if "mistral" in low: return "Mistral"
    if "nemotron" in low: return "Nemotron"
    if "llama" in low: return "Llama"
    if "qwen" in low: return "Qwen"
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
                6: "Six", 7: "Seven", 8: "Eight", 9: "Nine", 10: "Ten"}


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


def clean_for_speech(t):
    if not t:
        return ""
    t = re.sub(r"https?://\S+", " ", t)
    t = re.sub(r"www\.\S+", " ", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)
    t = re.sub(r"```.*?```", " ", t, flags=re.DOTALL)
    t = EMOJI_RE.sub(" ", t)
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
        "X-Title": "AI Debate Arena",
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

STOP_WORDS = {"a", "an", "the", "is", "are", "was", "were", "be", "been", "do", "does",
              "did", "should", "would", "could", "can", "will", "shall", "must", "has",
              "have", "had", "there", "really", "actually", "ever"}


def _titlecase_label(s, limit=26):
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
        if sep in low:
            left, right = low.split(sep, 1)
            la, lb = _titlecase_label(left), _titlecase_label(right)
            if la and lb and la != lb:
                return {
                    "side_a_label": la,
                    "side_a_stance": f"{la.title()} is the right answer to this question",
                    "side_b_label": lb,
                    "side_b_stance": f"{lb.title()} is the right answer to this question",
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


def generate_turn(topic, roles, side, round_num, turn_num, opponent_last, target_words, model):
    label = roles["side_a_label"] if side == "A" else roles["side_b_label"]
    other = roles["side_b_label"] if side == "A" else roles["side_a_label"]
    stance = roles["side_a_stance"] if side == "A" else roles["side_b_stance"]
    used_str = "; ".join(list(USED_ARGUMENTS)[-6:])[:400] if USED_ARGUMENTS else "nothing yet"

    if not opponent_last:
        prompt = (
            f"The debate question is: {topic}\n"
            f"You are arguing the {label} side. You believe that {stance}.\n\n"
            "This is your opening. Say the one thing that most convinces you, and make it concrete: "
            "a specific case, study, number, event or story with enough detail that a listener could "
            "go look it up afterwards. Then say plainly what it means for the question.\n\n"
            f"Speak for roughly {target_words} words. Talk like a person on a podcast, warm, direct, "
            "using contractions. Start with the substance in your very first sentence. Do not greet "
            "anyone, do not name your side, do not describe what you are about to say, and do not "
            "reason about which example to pick. Pick one and say it as if you always meant to."
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
            f"Speak for roughly {target_words} words. Contractions, plain speech, some edge to it. "
            "Your first sentence must already be engaging what they said. Never announce that you "
            "are about to respond, counter, address or discuss anything, and never weigh options "
            "out loud. Choose your evidence silently and state it with conviction."
        )

    attempted = []
    best = ""
    best_model = None
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

        cleaned = trim_to_words(cleaned, target_words + 25)
        for s in re.split(r"(?<=[.!?])\s+", cleaned)[:3]:
            if len(s) > 40:
                USED_ARGUMENTS.add(s[:90])
        return cleaned, m

    if best:
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
        parts.append(f"{t} calling it even")
    if not parts:
        base = "no judge reached a stable verdict"
    elif len(parts) == 1:
        base = parts[0]
    else:
        base = ", ".join(parts[:-1]) + " and " + parts[-1]
    if u:
        base += (f", with {u} thrown out for scoring the running order rather than "
                 f"the argument")
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
        f"About {COMMENTARY_WORDS} words. Talk like a person reacting, not like a report. "
        "No scores, no numbers, no round number, no preamble, no describing what you are doing, "
        "and no thinking out loud about what you might say. Just say it."
    )
    resp = query_openrouter(prompt, model, timeout=40, max_tokens=320, temperature=0.88)
    if not resp:
        return None
    cleaned, deliberation, sentence_count = clean_response(resp)
    if is_deliberating(deliberation, sentence_count):
        return None
    if count_words(cleaned) < 18:
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
        parts.append(f"{summary['lean_a']} leaning {roles['side_a_label']}")
    if summary["lean_b"]:
        parts.append(f"{summary['lean_b']} leaning {roles['side_b_label']}")
    if summary["undecided"]:
        parts.append(f"{summary['undecided']} genuinely on the fence")
    if summary["declined"]:
        parts.append(f"{summary['declined']} refusing to take a side at all")
    if not parts:
        return "nobody would answer"
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def build_opening_poll_narration(summary, roles, topic):
    return (
        f"Before anybody argues anything, we asked {summary['asked']} models from "
        f"{summary['providers']} different labs where they stand. {topic} "
        f"{sentence_case(describe_poll(summary, roles))}. "
        f"On a scale of minus five to plus five they average {summary['mean']:+.1f}, "
        f"and they are spread from {summary['low']:+.1f} to {summary['high']:+.1f}, "
        f"so this is not a room that already agrees. "
        f"That is the position our debaters have to shift."
    )


def build_closing_poll_narration(before, after, roles, movement):
    """Lead on how many models moved. That does not depend on anyone's scale."""
    moved = movement["toward_a"] + movement["toward_b"]

    if moved == 0:
        headline = ("not a single model shifted its position. Two rounds of argument, and "
                    "the panel is exactly where it started")
    elif movement["toward_a"] and movement["toward_b"]:
        headline = (f"{movement['toward_a']} moved toward {roles['side_a_label']} and "
                    f"{movement['toward_b']} moved the other way, so the room pulled apart "
                    f"rather than together")
    else:
        toward = roles["side_a_label"] if movement["toward_a"] else roles["side_b_label"]
        movable = moved + movement["unchanged"]
        headline = (f"{moved} of {movable} models moved, every one of them toward "
                    f"{toward}")

    crossed = ""
    if movement["crossed"]:
        shown = movement["crossed"][:3]
        names = shown[0] if len(shown) == 1 else ", ".join(shown[:-1]) + " and " + shown[-1]
        crossed = (f" {names} did not just soften, "
                   f"{'they' if len(movement['crossed']) > 1 else 'it'} changed sides.")

    return (
        f"Now the part that matters. We put the same question to the same models again, this "
        f"time having read every word of the debate. "
        f"{sentence_case(headline)}.{crossed} "
        f"The average went from {before['mean']:+.1f} to {after['mean']:+.1f}, "
        f"and they finished {describe_poll(after, roles)}. "
        f"That is what these models said, before and after. It is not a measure of who "
        f"is right, and models built from the same sort of training data are not "
        f"independent witnesses. Take it for what it is."
    )


# ----------------------------------------------------------------------------
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
    if scorecard and audio_file and full_text:
        dur = get_audio_duration(audio_file) or 6.0
        events.append(f"Dialogue: 0,0:00:00.00,{format_ass_time(dur)},ScoreSub,,0,0,0,,{ass_escape(full_text)}")
        open(fn, "w", encoding="utf-8").write(header + "\n".join(events) + "\n")
        return
    if not words:
        open(fn, "w", encoding="utf-8").write(header)
        return
    if audio_file:
        try:
            actual = get_audio_duration(audio_file)
            if actual > 1 and words:
                est = words[-1].get("end", actual)
                if abs(est - actual) > 0.5 and est > 0:
                    scale = actual / est
                    for w in words:
                        w["start"] *= scale
                        w["end"] *= scale
        except Exception:
            pass

    def emit(chunk, end):
        s = chunk[0]["start"]
        txt = "\\N".join([" ".join([ass_escape(c["text"]) for c in chunk[i:i + 10]])
                          for i in range(0, len(chunk), 10)][:4])
        events.append(f"Dialogue: 0,{format_ass_time(s)},{format_ass_time(end)},DebateSub,,0,0,0,,"
                      f"{{\\an2\\pos(960,790)\\q2\\fad(120,120)}}{txt}")

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
            fp.append(f"[{3 + idx}:v]scale={EMOJI_W}:{EMOJI_H},format=rgba,"
                      f"fade=t=in:st=0:d=0.3:alpha=1,"
                      f"fade=t=out:st={fade_out:.2f}:d=0.4:alpha=1,"
                      f"setpts=PTS-STARTPTS[v{idx}]")
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
                f"setpts=PTS-STARTPTS[v{idx}]")
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
    draw.text((W // 2, 50), f"ROUND {rn} SCORES", font=ft, fill=(255, 215, 0), anchor="mt")
    draw.text((W // 2, 115), f"{roles['side_a_label']}  vs  {roles['side_b_label']}",
              font=fs, fill=(255, 255, 255), anchor="mt")
    hy = 190
    cx1, cx2, cx3, cx4 = 120, 750, 1050, 1350
    sa = roles["side_a_label"][:14]
    sb = roles["side_b_label"][:14]
    draw.rectangle([60, hy - 10, W - 60, hy + 45], fill=(25, 35, 70), outline=(255, 215, 0), width=2)
    draw.text((cx1, hy), "Judge", font=fh, fill=(255, 255, 255))
    draw.text((cx2, hy), sa, font=fh, fill=(0, 255, 204))
    draw.text((cx3, hy), sb, font=fh, fill=(255, 120, 255))
    draw.text((cx4, hy), "Winner", font=fh, fill=(255, 215, 0))
    y = hy + 65
    for idx, r in enumerate(res):
        draw.rectangle([60, y - 8, W - 60, y + 42],
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
            wl, col = "ABSTAIN", (150, 150, 150)
        draw.text((cx4, y), wl, font=fr, fill=col)
        y += 58
    draw.line([(60, y + 5), (W - 60, y + 5)], fill=(255, 255, 255), width=2)
    y += 25
    if va is not None:
        split = f"Panel: {va} - {vb}" + (f" ({vt} even)" if vt else "")
        if vu:
            split += f"   {vu} abstained"
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
    draw.text((W // 2, 104), f"{roles['side_b_label']}  \u2190   lean   \u2192  "
                             f"{roles['side_a_label']}",
              font=fs, fill=(255, 255, 255), anchor="mt")

    left, right = 430, W - 430
    mid = (left + right) // 2
    top = 168
    row_h = 52
    rows = results[:14]

    # Scale gridline
    draw.line([(mid, top - 14), (mid, top + row_h * len(rows) + 6)],
              fill=(255, 255, 255, 60), width=1)

    for i, r in enumerate(rows):
        y = top + i * row_h
        if i % 2 == 0:
            draw.rectangle([60, y - 6, W - 60, y + row_h - 12], fill=(20, 28, 50))
        name = f"{r['display_name']} ({r['provider']})"
        draw.text((80, y), name[:34], font=fr, fill=(255, 255, 255))

        if r["declined"] or r["position"] is None:
            draw.text((mid, y), "declined to answer", font=fr,
                      fill=(150, 150, 150), anchor="mt")
            continue
        pos = r["position"]
        x = int(mid + (pos / POLL_SCALE) * (right - mid))
        colour = (0, 255, 204) if pos > LEAN_THRESHOLD else \
                 (255, 120, 255) if pos < -LEAN_THRESHOLD else (220, 220, 220)
        draw.line([(mid, y + 14), (x, y + 14)], fill=colour, width=5)
        draw.ellipse([x - 9, y + 5, x + 9, y + 23], fill=colour)
        draw.text((right + 26, y), f"{pos:+.1f}", font=fr, fill=colour)

    y = top + row_h * len(rows) + 24
    draw.line([(60, y), (W - 60, y)], fill=(255, 255, 255), width=2)
    y += 18
    line = (f"{summary['lean_a']} {roles['side_a_label']}   |   "
            f"{summary['lean_b']} {roles['side_b_label']}   |   "
            f"{summary['undecided']} undecided   |   {summary['declined']} declined")
    draw.text((W // 2, y), line, font=fs, fill=(255, 255, 255), anchor="mt")
    y += 44
    if before is None:
        draw.text((W // 2, y), f"Panel average: {summary['mean']:+.2f}",
                  font=fs, fill=(255, 215, 0), anchor="mt")
    else:
        swing = summary["mean"] - before["mean"]
        draw.text((W // 2, y),
                  f"Panel average: {before['mean']:+.2f}  \u2192  {summary['mean']:+.2f}"
                  f"   (swing {swing:+.2f})",
                  font=fs, fill=(255, 215, 0), anchor="mt")
    img.save(path)


def generate_verdict_board(path, topic, roles, before, after, movement, votes,
                           mean_a, mean_b):
    """The closing card: who changed minds, and who argued better.

    These are two different results from two different instruments, so they get
    two panels rather than being blended into one winner.
    """
    W, H = VIDEO_W, VIDEO_H
    img = Image.alpha_composite(
        Image.new("RGB", (W, H), (12, 16, 32)).convert("RGBA"),
        Image.new("RGBA", (W, H), (0, 0, 0, 185))).convert("RGB")
    draw = ImageDraw.Draw(img)
    ft = load_font(54, bold=True)
    fh = load_font(30, bold=True)
    fb = load_font(46, bold=True)
    fs = load_font(24)

    draw.text((W // 2, 52), "THE VERDICT", font=ft, fill=(255, 215, 0), anchor="mt")
    draw.text((W // 2, 122), topic[:88], font=fs, fill=(255, 255, 255), anchor="mt")

    swing = after["mean"] - before["mean"]
    moved = movement["toward_a"] + movement["toward_b"]
    if moved == 0 or abs(swing) < 0.15:
        persuader, p_colour = "NOBODY MOVED", (200, 200, 200)
        p_detail = "not one model changed its position"
    elif movement["toward_a"] > movement["toward_b"]:
        persuader, p_colour = roles["side_a_label"], (0, 255, 204)
        p_detail = f"{movement['toward_a']} of {moved + movement['unchanged']} models moved their way"
    elif movement["toward_b"] > movement["toward_a"]:
        persuader, p_colour = roles["side_b_label"], (255, 120, 255)
        p_detail = f"{movement['toward_b']} of {moved + movement['unchanged']} models moved their way"
    else:
        persuader, p_colour = "SPLIT", (200, 200, 200)
        p_detail = "the panel moved both ways in equal numbers"

    if mean_a is None or mean_b is None:
        arguer, a_colour = "NOT SCORED", (150, 150, 150)
        a_detail = "no judge returned a usable verdict"
        a_sub = "nothing was invented to fill the gap"
    else:
        if votes["A"] > votes["B"]:
            arguer, a_colour = roles["side_a_label"], (0, 255, 204)
        elif votes["B"] > votes["A"]:
            arguer, a_colour = roles["side_b_label"], (255, 120, 255)
        else:
            arguer, a_colour = "TIED", (200, 200, 200)
        a_detail = (f"judges scored it {votes['A']}-{votes['B']}"
                    + (f", {votes['TIE']} even" if votes["TIE"] else "")
                    + (f", {votes['UNSTABLE']} discarded" if votes["UNSTABLE"] else ""))
        a_sub = f"average {mean_a:.1f} vs {mean_b:.1f} out of 100"

    box_y, box_h = 220, 300
    for i, (title, name, colour, detail, sub) in enumerate([
        ("CHANGED MINDS", persuader, p_colour, p_detail,
         f"panel moved {swing:+.2f} on a -5 to +5 scale"),
        ("ARGUED BETTER", arguer, a_colour, a_detail, a_sub),
    ]):
        x0 = 120 + i * (W - 240) // 2
        x1 = x0 + (W - 280) // 2
        cx = (x0 + x1) // 2
        draw.rounded_rectangle([x0, box_y, x1, box_y + box_h], radius=18,
                               fill=(20, 28, 50), outline=(255, 255, 255), width=2)
        draw.text((cx, box_y + 28), title, font=fh, fill=(255, 255, 255), anchor="mt")
        label = name if len(name) <= 18 else name[:16] + ".."
        draw.text((cx, box_y + 96), label, font=fb, fill=colour, anchor="mt")
        draw.text((cx, box_y + 172), detail[:52], font=fs, fill=(220, 220, 220), anchor="mt")
        draw.text((cx, box_y + 210), sub[:52], font=fs, fill=(160, 160, 160), anchor="mt")

    y = box_y + box_h + 60
    draw.line([(120, y), (W - 120, y)], fill=(255, 255, 255), width=2)
    y += 26
    for line in [
        "Judges scored blind, with the sides reversed, and any judge that changed its "
        "mind on the running order was discarded.",
        "Changed minds is measured by asking the models their own position before and "
        "after. It measures persuasion, not truth.",
    ]:
        draw.text((W // 2, y), line, font=fs, fill=(190, 190, 190), anchor="mt")
        y += 34
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

WORD_EMOJI_MAP = {
    "money": "\U0001F4B0", "cost": "\U0001F4B0", "economy": "\U0001F4B9", "tax": "\U0001F4B8",
    "jobs": "\U0001F3ED", "work": "\U0001F477", "school": "\U0001F3EB", "education": "\U0001F393",
    "children": "\U0001F9D2", "kids": "\U0001F9D2", "family": "\U0001F46A",
    "health": "\U0001FA7A", "medicine": "\U0001F489", "hospital": "\U0001F3E5",
    "science": "\U0001F52C", "research": "\U0001F52C", "data": "\U0001F4CA",
    "evidence": "\U0001F50D", "study": "\U0001F4C8", "history": "\U0001F4DC",
    "law": "\u2696\ufe0f", "court": "\u2696\ufe0f", "rights": "\u270A", "freedom": "\U0001F54A\ufe0f",
    "government": "\U0001F3DB\ufe0f", "vote": "\U0001F5F3\ufe0f", "war": "\u2694\ufe0f",
    "climate": "\U0001F30D", "planet": "\U0001F30D", "energy": "\u26A1", "pollution": "\U0001F3ED",
    "technology": "\U0001F916", "computer": "\U0001F4BB", "internet": "\U0001F310",
    "universe": "\U0001F30C", "stars": "\u2B50", "dna": "\U0001F9EC", "brain": "\U0001F9E0",
    "god": "\u2728", "faith": "\U0001F64F", "prayer": "\U0001F64F", "truth": "\U0001F4A1",
    "pain": "\U0001F623", "suffering": "\U0001F622", "death": "\U0001F5A4", "life": "\U0001F31F",
    "future": "\U0001F52E", "risk": "\u26A0\ufe0f", "safety": "\U0001F6E1\ufe0f",
    "food": "\U0001F35E", "water": "\U0001F4A7", "city": "\U0001F3D9\ufe0f", "world": "\U0001F30E",
}


def emoji_to_codepoint(ec):
    codes = []
    for ch in ec:
        cp = ord(ch)
        if cp == 0xfe0f:
            continue
        codes.append(f"{cp:x}")
    return "-".join(codes)


def create_emoji_asset(ec):
    """Twemoji artwork for one emoji, or None.

    There is no drawn fallback: the font has no emoji glyphs, so drawing the
    character produced a blank rectangle on screen. A cue we cannot render
    properly is dropped instead.
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
    if not EMOJI_CDN_OK:
        return None
    for version in ("14.0.2", "15.1.0"):
        url = (f"https://cdn.jsdelivr.net/gh/twitter/twemoji@{version}"
               f"/assets/72x72/{code}.png")
        try:
            resp = requests.get(url, timeout=8)
        except requests.exceptions.RequestException:
            EMOJI_CDN_OK = False
            print("    Emoji CDN unreachable; visual cues disabled for this build.")
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
    for w in words:
        cw = re.sub(r"[^a-z]", "", w["text"].lower())
        if cw not in WORD_EMOJI_MAP:
            continue
        s = float(w["start"])
        e = float(w["end"]) + 1.3
        if any(not (e < p["start"] or s > p["end"]) for p in plan):
            continue
        if plan and s - plan[-1]["end"] < 0.9:
            continue
        ec = WORD_EMOJI_MAP[cw]
        if plan and ec == plan[-1].get("emoji"):
            continue
        plan.append({"kind": "emoji", "emoji": ec,
                     "start": max(0.0, s), "end": e})
        if len(plan) >= 6:
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
    text = (f"Round {rn} is scored. The panel came down {spoken_split(va, vb, vt, vu, roles)}. "
            f"On points, {roles['side_a_label']} {ra:.1f}, and {roles['side_b_label']} {rb:.1f}. "
            f"Averaged so far, {cum_a / rn:.1f} to {cum_b / rn:.1f}.")
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


def build_intro(topic, jc, roles):
    """Fixed branded opening, one variable line naming tonight's question."""
    return (
        f"{INTRO_OPENING} "
        f"Tonight's question is this. {topic} "
        f"Arguing {roles['side_a_label']}, on my left. "
        f"Arguing {roles['side_b_label']}, on my right. "
        f"{number_word(ROUNDS)} rounds, equal time, and {jc} independent AI judges. "
        f"{INTRO_RULES}"
    )


def build_outro(mean_a, mean_b, votes_a, votes_b, votes_t, votes_u, roles,
                swing=None, movement=None):
    """Closing read: who changed minds, then who argued better, then the sign off.

    These are kept apart deliberately. Changing minds is measured by asking the
    models their own position before and after, which is what persuasion
    actually means. Arguing better is the blind panel scoring the transcript.
    They can disagree, and when they do that is worth saying out loud.
    """
    counted = votes_a + votes_b + votes_t
    total = counted + votes_u

    if votes_a > votes_b:
        arguer = roles["side_a_label"]
    elif votes_b > votes_a:
        arguer = roles["side_b_label"]
    else:
        arguer = None

    persuader = None
    if movement:
        if movement["toward_a"] > movement["toward_b"]:
            persuader = roles["side_a_label"]
        elif movement["toward_b"] > movement["toward_a"]:
            persuader = roles["side_b_label"]

    lines = []
    if persuader and movement:
        their_way = (movement["toward_a"] if persuader == roles["side_a_label"]
                     else movement["toward_b"])
        answering = (movement["toward_a"] + movement["toward_b"]
                     + movement["unchanged"])
        lines.append(f"So the most persuasive side tonight was {persuader}. "
                     f"{their_way} of the {answering} models that answered moved "
                     f"their way.")
    elif movement is not None:
        lines.append("So the most persuasive side tonight was neither. Nobody on the "
                     "panel shifted their position at all.")

    if mean_a is None or mean_b is None:
        lines.append("On the arguing itself we have no score at all. Not one judge "
                     "returned a usable verdict, so rather than make numbers up we are "
                     "leaving that column empty.")
        arguer = None
    elif arguer:
        lines.append(f"On the arguing itself, judged blind, {arguer} scored higher, "
                     f"{mean_a:.1f} to {mean_b:.1f} out of a hundred.")
    else:
        lines.append(f"On the arguing itself, judged blind, the panel could not separate "
                     f"them, {mean_a:.1f} to {mean_b:.1f} out of a hundred.")

    if persuader and arguer and persuader != arguer:
        lines.append(f"Which is the interesting part. {arguer} argued it better, and "
                     f"{persuader} is the side that actually changed minds.")

    if votes_u and counted <= total / 2:
        lines.append(f"Only {counted} of {total} judgements held up when we reversed the "
                     f"running order, so treat the scoring lightly.")

    lines.append(OUTRO_SIGNOFF)
    return " ".join(lines)


def build_method_note(topic, roles, debaters, judges, poll_roster,
                      sum_before, sum_after, movement, votes, mean_a=None, mean_b=None):
    """A description-ready statement of what this run actually did."""
    labs = sorted({provider_from_model(m) for m in poll_roster})
    swing = sum_after["mean"] - sum_before["mean"]
    moved = movement["toward_a"] + movement["toward_b"]
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
    if USE_PAID_MODELS:
        # A pinned roster: the same debaters and panel in every video.
        avail = [m for m in DEBATER_MODELS + PANEL_MODELS if not is_reasoning_model(m)]
        print(f"Paid roster: {len(DEBATER_MODELS)} debaters, {len(PANEL_MODELS)} panel models.")
    else:
        avail = discover_models() or FALLBACK_MODELS.copy()
    AVAILABLE_MODELS = [m for m in avail if not is_reasoning_model(m)]
    print_cost_estimate(len(PANEL_MODELS) if USE_PAID_MODELS else MAX_JUDGES)
    preflight_models(AVAILABLE_MODELS or avail, MIN_PANEL_SIZE)
    if USE_PAID_MODELS and len(DEBATER_MODELS) >= 2:
        ap_model, sk_model = DEBATER_MODELS[0], DEBATER_MODELS[1]
    else:
        ap_model, sk_model = choose_primary_models(AVAILABLE_MODELS or avail)
    roles = get_debate_roles(topic, ap_model)
    print(f"Sides: {roles['side_a_label']} (Brian, left) vs {roles['side_b_label']} (Ava, right)")
    print(f"  A stance: {roles['side_a_stance']}")
    print(f"  B stance: {roles['side_b_stance']}")
    if USE_PAID_MODELS and PANEL_MODELS:
        judges = pin_panel(PANEL_MODELS, (ap_model, sk_model))
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

    add_seg(build_intro(topic, len(judges), roles), "MOD", "MODERATOR", count_speech=False)

    # Where the panel stands before hearing a word. This is the consensus
    # measurement; the round scorecards measure who argued better.
    poll_roster = build_poll_roster((ap_model, sk_model), judges)
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
        "opening", "WHERE THE PANEL STANDS - BEFORE"))
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
        a_favs = [r for r in res if r["winner"] == "A"]
        b_favs = [r for r in res if r["winner"] == "B"]
        picks = []
        if a_favs:
            picks.append((random.choice(a_favs), "A"))
        if b_favs:
            taken = {p[0]["provider"] for p in picks}
            pool = [r for r in b_favs if r["provider"] not in taken] or b_favs
            picks.append((random.choice(pool), "B"))
        for judge, side in picks:
            text = generate_panel_commentary(judge["model"], side, topic, rn, a_full, b_full, roles)
            if not text:
                # Nothing this judge actually said; better silent than scripted.
                print(f"  {judge['display_name']} gave no usable reasoning; reaction skipped.")
                continue
            jvi = JUDGE_VOICE_MAP.get(judge["model"], 0)
            name = f"JUDGE — {judge['display_name'].upper()} ({judge['provider'].upper()})"
            print(f"  reaction for {roles['side_a_label'] if side == 'A' else roles['side_b_label']}"
                  f" by {judge['display_name']} [{judge['model']}]")
            add_seg(text, "JUDGE", name, jvi=jvi, count_speech=False)
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
        "closing", "WHERE THE PANEL STANDS - AFTER", before=sum_before))
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
                             swing=swing, movement=movement)
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
