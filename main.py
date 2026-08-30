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

ROUNDS = 3
TURNS_PER_SIDE_PER_ROUND = 2
MAX_JUDGES = 7

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
EST_INTRO_SEC = 26.0
EST_OUTRO_SEC = 22.0
EST_SCORECARD_SEC = 16.0
EST_COMMENTARY_SEC = 22.0
COMMENTARY_WORDS = 55

EMOJI_W = 180
EMOJI_H = 180
USED_ARGUMENTS = set()
USED_JUDGE_EXPLANATIONS = set()

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
]
JUDGE_VOICE_MAP = {}

# Speaker slot -> (screen position, accent colour)
SLOT_STYLE = {
    "A": ("left", "#00FFCC"),
    "B": ("right", "#FF00FF"),
    "JUDGE": ("center", "#3399FF"),
    "MOD": ("center", "#FFD700"),
}

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
}

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
    if "gpt" in low: return "ChatGPT"
    if "claude-3-5" in low: return "Claude 3.5"
    if "claude" in low: return "Claude"
    if "gemini-2.0" in low: return "Gemini 2.0"
    if "gemini" in low: return "Gemini"
    if "deepseek" in low: return "DeepSeek"
    if "mistral" in low: return "Mistral"
    if "nemotron" in low: return "Nemotron"
    if "llama" in low: return "Llama"
    if "qwen" in low: return "Qwen"
    return provider_from_model(mid)


def cleanup_cache():
    for pat in ["*.mp4", "*.mp3", "*.ass", "*.png", "*_list.txt"]:
        for fn in glob.glob(pat):
            if fn in [OUTPUT_FILE, "background.png", "topic.txt"]:
                continue
            try:
                os.remove(fn)
            except OSError:
                pass


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
    t = re.sub(r"\s*\+\s*", " plus ", t)
    t = re.sub(r"\s*=\s*", " equals ", t)
    t = t.replace("(", " ").replace(")", " ").replace("[", " ").replace("]", " ")
    t = t.replace("–", ", ").replace("—", ". ").replace(" - ", ". ")
    for o, n in {"*": "", "#": "", "_": "", "`": "", "\"": "", ":": " . ", ";": " . ", "&": " and"}.items():
        t = t.replace(o, n)
    t = re.sub(r"\s+", " ", t).strip()
    if t and t[-1] not in ".!?":
        t += "."
    t = re.sub(r"\.{2,}", ".", t)
    return t


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
            if not mid or ":free" not in mid.lower():
                continue
            if any(x in mid.lower() for x in ["embed", "tts", "whisper", "audio"]):
                continue
            if is_reasoning_model(mid):
                continue
            top = ["openai", "anthropic", "google", "meta-llama", "mistralai", "deepseek", "qwen", "nvidia"]
            if not any(p in mid.lower() for p in top):
                continue
            free.append(mid)
        if free:
            return list(dict.fromkeys(free))
        return FALLBACK_MODELS.copy()
    except Exception:
        return FALLBACK_MODELS.copy()


def query_openrouter(prompt, mid, timeout=60, max_tokens=800, temperature=0.82, system=None):
    if not OPENROUTER_API_KEY:
        return None
    if ":free" not in (mid or "").lower():
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
                if c and len(c.strip()) > 60:
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
    free = [m for m in avail if ":free" in m] or FALLBACK_MODELS
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
    top_providers = {"openai", "anthropic", "google", "meta-llama", "mistralai", "deepseek", "qwen", "nvidia"}
    cands = [m for m in avail if m not in excl and ":free" in m
             and m.split("/")[0].lower() in top_providers
             and provider_from_model(m) not in primary_providers]
    if len(cands) < 4:
        cands = [m for m in avail if m not in excl and ":free" in m
                 and provider_from_model(m) not in primary_providers]
    groups = {}
    for m in cands:
        prov = provider_from_model(m)
        if prov not in groups:
            groups[prov] = m
    order = ["OpenAI", "Anthropic", "Google", "Meta", "Mistral", "DeepSeek", "Qwen", "Nvidia"]
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
                                system="You return only valid JSON. No commentary.")
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

def judge_round(model, topic, rn, ap, sk, roles):
    """Return this judge's real scores, or None if it did not return any.

    A judge that fails is left out of the panel. No score is ever invented,
    and a genuine tie is recorded as a tie rather than nudged into a winner.
    """
    prompt = (
        f"You are judging round {rn} of a debate on: {topic}\n\n"
        f"{roles['side_a_label']} argued:\n{ap[:2000]}\n\n"
        f"{roles['side_b_label']} argued:\n{sk[:2000]}\n\n"
        "Score each side from 0 to 100 on how well they used evidence and how directly they "
        "answered the other side. Judge only what was actually said above.\n"
        'Return ONLY JSON: {"A_total": 0, "B_total": 0, "winner": "A", '
        '"reason": "one spoken sentence naming the specific point that decided it"}\n'
        'Use "winner": "TIE" only if the two sides were genuinely inseparable.'
    )
    if ":free" not in model or is_reasoning_model(model):
        return None
    # Only this judge may produce this judge's verdict. Substituting another
    # model here would put a score on the scorecard under the wrong name.
    for _ in range(2):
        resp = query_openrouter(prompt, model, timeout=40, max_tokens=320, temperature=0.5,
                                system="You return only valid JSON. No commentary.")
        d = extract_json_object(resp)
        if not d or d.get("A_total") is None or d.get("B_total") is None:
            continue
        try:
            a = clamp_score(d.get("A_total"))
            b = clamp_score(d.get("B_total"))
        except Exception:
            continue
        if a > b:
            winner = "A"
        elif b > a:
            winner = "B"
        else:
            winner = "TIE"
        return {"model": model, "provider": provider_from_model(model),
                "display_name": get_judge_short_name(model),
                "A_total": round(a, 1), "B_total": round(b, 1),
                "winner": winner,
                "reason": str(d.get("reason", ""))[:200]}
    return None


def evaluate_round(judges, topic, rn, ap, sk, roles, recused=()):
    """Score a round with the judges that actually responded.

    The panel is never padded. A model that fails to return scores simply does
    not appear on the scorecard. Any model that wrote a turn this round is
    recused, so nothing scores its own text.
    """
    recused = set(recused)
    sitting = [m for m in judges if m not in recused]
    for m in judges:
        if m in recused:
            print(f"    {get_judge_short_name(m)} wrote a turn this round and is recused "
                  f"from scoring it.")
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
        raise DebateGenerationError(
            f"No judge returned a usable score for round {rn}. The scorecard would have been "
            f"invented, so the build stops here instead."
        )
    results.sort(key=lambda r: r["display_name"])
    for r in results:
        print(f"    judge {r['display_name']} [{r['model']}]: "
              f"{roles['side_a_label']} {r['A_total']:.1f}, "
              f"{roles['side_b_label']} {r['B_total']:.1f} -> {r['winner']}")
    return results


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
# Visuals
# ----------------------------------------------------------------------------

def emoji_to_codepoint(ec):
    codes = []
    for ch in ec:
        cp = ord(ch)
        if cp == 0xfe0f:
            continue
        codes.append(f"{cp:x}")
    return "-".join(codes)


EMOJI_CACHE_DIR = "emoji_cache"
os.makedirs(EMOJI_CACHE_DIR, exist_ok=True)
# If the emoji CDN is unreachable, stop retrying it on every later segment.
EMOJI_CDN_OK = True
# Codepoints the CDN has no artwork for; never requested twice in one build.
EMOJI_MISSING = set()


def create_emoji_asset(ec, idx):
    """Fetch a Twemoji PNG for `ec`. Returns a path, or None if unavailable.

    There is deliberately no drawn fallback: DejaVu has no emoji glyphs, so
    drawing the character produced a blank .notdef rectangle on screen. A cue we
    cannot render properly is simply dropped instead.
    """
    global EMOJI_CDN_OK
    code = emoji_to_codepoint(ec)
    if not code or code in EMOJI_MISSING:
        return None

    cached = os.path.join(EMOJI_CACHE_DIR, f"{code}.png")
    img_data = None
    if os.path.exists(cached):
        try:
            img_data = Image.open(cached).convert("RGBA")
        except Exception:
            try:
                os.remove(cached)
            except OSError:
                pass

    if img_data is None and EMOJI_CDN_OK:
        for version in ("14.0.2", "15.1.0"):
            url = (f"https://cdn.jsdelivr.net/gh/twitter/twemoji@{version}"
                   f"/assets/72x72/{code}.png")
            try:
                resp = requests.get(url, timeout=8)
            except requests.exceptions.RequestException:
                EMOJI_CDN_OK = False
                print("Emoji CDN unreachable; visual cues disabled for this build.")
                return None
            if resp.status_code == 200 and len(resp.content) > 500:
                try:
                    img_data = Image.open(BytesIO(resp.content)).convert("RGBA")
                    img_data.save(cached)
                    break
                except Exception:
                    img_data = None

    if img_data is None or img_data.size[0] <= 10:
        # No artwork for this codepoint; never ask for it again this build.
        EMOJI_MISSING.add(code)
        return None

    try:
        size = 500
        canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        resized = img_data.resize((380, 380), Image.LANCZOS)
        x = y = (size - 380) // 2
        shadow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(shadow)
        d.ellipse([x + 6, y + 6, x + 386, y + 386], fill=(0, 0, 0, 60))
        shadow = shadow.filter(ImageFilter.GaussianBlur(6))
        canvas = Image.alpha_composite(canvas, shadow)
        canvas.paste(resized, (x, y), resized)
        fn = f"emoji_{idx}.png"
        canvas.save(fn)
        return fn
    except Exception:
        return None


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


async def generate_audio_async(text, voice, fn):
    ct = clean_for_speech(text)
    if "Brian" in voice:
        ssml = f"<speak version='1.0' xml:lang='en-US'><voice name='{voice}'><prosody rate='-2%' pitch='-2%'>{ct}</prosody></voice></speak>"
    elif "Ava" in voice:
        ssml = f"<speak version='1.0' xml:lang='en-US'><voice name='{voice}'><prosody rate='+1%' pitch='+1%'>{ct}</prosody></voice></speak>"
    else:
        ssml = f"<speak version='1.0' xml:lang='en-US'><voice name='{voice}'><prosody rate='+0%'>{ct}</prosody></voice></speak>"
    try:
        com = edge_tts.Communicate(ssml, voice)
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
        if not words:
            raise RuntimeError("no word boundaries")
        return words
    except Exception:
        com = edge_tts.Communicate(ct, voice, rate="+1%")
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
        if not words:
            t = 0
            for tok in ct.split():
                words.append({"text": tok, "start": t, "duration": 0.38, "end": t + 0.38})
                t += 0.42
        return words


def voice_for_slot(slot, judge_voice_index=None):
    if slot == "A":
        return SIDE_A_VOICE
    if slot == "B":
        return SIDE_B_VOICE
    if slot == "JUDGE":
        return JUDGE_VOICES[(judge_voice_index or 0) % len(JUDGE_VOICES)]
    return MODERATOR_VOICE


def generate_audio(text, slot, fn, judge_voice_index=None):
    voice = voice_for_slot(slot, judge_voice_index)
    try:
        return asyncio.run(generate_audio_async(text, voice, fn))
    except Exception:
        return asyncio.run(generate_audio_async(text, MODERATOR_VOICE, fn))


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
    for idx, vis in enumerate(visual_plan):
        try:
            ec = vis.get("emoji", "") if isinstance(vis, dict) else str(vis)
            st = vis.get("start", idx * 2.2) if isinstance(vis, dict) else idx * 2.2
            et = vis.get("end", st + 3.2) if isinstance(vis, dict) else st + 3.2
            gp = create_emoji_asset(ec, idx + 1000 + random.randint(0, 9999))
        except Exception:
            gp = None
        # No artwork means no cue; drawing a placeholder yields a blank box.
        if gp:
            visual_inputs.append((gp, st, et))
    for idx, (gp, st, et) in enumerate(visual_inputs):
        fp.append(f"[{3 + idx}:v]scale={EMOJI_W}:{EMOJI_H}[v{idx}]")
        vx = (VIDEO_W - EMOJI_W) // 2
        vy = (VIDEO_H - EMOJI_H) // 2 - 50
        nl = f"[tmp{idx}]"
        fp.append(f"{last}[v{idx}]overlay={vx}:{vy}:enable='between(t,{st:.2f},{et:.2f})'{nl}")
        last = nl

    safe_subs = subs_path.replace(":", "\\:")
    fp.append(f"{last}format=yuv420p,subtitles={safe_subs}[out]")

    for gp, _, _ in visual_inputs:
        cmd.extend(["-i", gp])
    cmd.extend(["-filter_complex", ";".join(fp), "-map", "[out]", "-map", "2:a",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-r", str(FPS),
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
                "-shortest", "-t", str(duration + 0.5), output_path])
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if r.returncode != 0:
        print(r.stderr[-8000:])
        raise RuntimeError("Render failed")
    for gp, _, _ in visual_inputs:
        try:
            os.remove(gp)
        except OSError:
            pass
    return duration


def generate_scoreboard(rn, res, avg_a, avg_b, cum_a, cum_b, path, roles):
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
        else:
            wl, col = "TIE", (220, 220, 220)
        draw.text((cx4, y), wl, font=fr, fill=col)
        y += 58
    draw.line([(60, y + 5), (W - 60, y + 5)], fill=(255, 255, 255), width=2)
    y += 25
    draw.text((W // 2, y), f"Round Avg: {avg_a:.1f} vs {avg_b:.1f}", font=fs, fill=(255, 255, 255), anchor="mt")
    draw.text((W // 2, y + 45), f"Cumulative: {cum_a:.1f} vs {cum_b:.1f}", font=fs, fill=(255, 215, 0), anchor="mt")
    img.save(path)


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


WORD_EMOJI_MAP = {
    "money": "\U0001F4B0", "cost": "\U0001F4B0", "economy": "\U0001F4B9", "tax": "\U0001F4B8",
    "jobs": "\U0001F3ED", "work": "\U0001F477", "school": "\U0001F3EB", "education": "\U0001F393",
    "children": "\U0001F9D2", "kids": "\U0001F9D2", "family": "\U0001F46A",
    "health": "\U0001FA7A", "medicine": "\U0001F489", "hospital": "\U0001F3E5",
    "science": "\U0001F52C", "research": "\U0001F52C", "data": "\U0001F4CA",
    "evidence": "\U0001F50D", "study": "\U0001F4C8", "history": "\U0001F4DC",
    "law": "⚖️", "court": "⚖️", "rights": "✊", "freedom": "\U0001F54A️",
    "government": "\U0001F3DB️", "vote": "\U0001F5F3️", "war": "⚔️",
    "climate": "\U0001F30D", "planet": "\U0001F30D", "energy": "⚡", "pollution": "\U0001F3ED",
    "technology": "\U0001F916", "computer": "\U0001F4BB", "internet": "\U0001F310",
    "universe": "\U0001F30C", "stars": "⭐", "dna": "\U0001F9EC", "brain": "\U0001F9E0",
    "god": "✨", "faith": "\U0001F64F", "prayer": "\U0001F64F", "truth": "\U0001F4A1",
    "pain": "\U0001F623", "suffering": "\U0001F622", "death": "\U0001F5A4", "life": "\U0001F31F",
    "future": "\U0001F52E", "risk": "⚠️", "safety": "\U0001F6E1️",
    "food": "\U0001F35E", "water": "\U0001F4A7", "city": "\U0001F3D9️", "world": "\U0001F30E",
}


def create_emoji_plan(words):
    if not words:
        return []
    plan, used = [], []
    for w in words:
        cw = re.sub(r"[^a-z]", "", w["text"].lower())
        if cw not in WORD_EMOJI_MAP:
            continue
        s = float(w["start"])
        e = float(w["end"]) + 1.3
        if any(not (e < us or s > ue) for us, ue in used):
            continue
        if used and s - used[-1][1] < 0.9:
            continue
        ec = WORD_EMOJI_MAP[cw]
        if plan and ec == plan[-1]["emoji"]:
            continue
        plan.append({"emoji": ec, "start": max(0, s), "end": e, "word": w["text"]})
        used.append((s, e))
        if len(plan) >= 6:
            break
    return plan


def prepare_segment(text, slot, display_name, topic, sid, judge_voice_index=None):
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
        spec["eplan"] = create_emoji_plan(words)
    except Exception:
        spec["eplan"] = []
    generate_subtitles(words, spec["subs"], scorecard=False,
                       audio_file=spec["audio"], full_text=text)
    create_background(pos, glow, spec["bg"])
    spec["wave_box"] = create_ui_overlay(display_name, topic, pos, glow, spec["ui"])
    spec["duration"] = get_audio_duration(spec["audio"])
    return spec


def prepare_scorecard(rn, res, ra, rb, cum_a, cum_b, roles):
    """Scoreboard image, spoken summary and subtitles. No render yet."""
    spec = {
        "kind": "scorecard", "sid": f"r{rn}",
        "image": f"scoreboard_r{rn}.png", "audio": f"score_audio_r{rn}.mp3",
        "subs": f"score_subs_r{rn}.ass", "video": f"score_video_r{rn}.mp4",
    }
    generate_scoreboard(rn, res, ra, rb, cum_a, cum_b, spec["image"], roles)
    text = (f"Round {rn} is scored. The panel gave {roles['side_a_label']} {ra:.1f}, "
            f"and {roles['side_b_label']} {rb:.1f}. That puts us at "
            f"{cum_a:.1f} to {cum_b:.1f} overall.")
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
                             spec["wave_box"], spec["eplan"])
    return spec["video"]


def build_intro(topic, jc, roles):
    return (f"Welcome to the AI Debate Arena. Tonight's question is this. {topic} "
            f"Arguing {roles['side_a_label']}, on my left. Arguing {roles['side_b_label']}, "
            f"on my right. Three rounds, equal time, and {jc} independent AI judges scoring every "
            f"exchange. Nobody here is holding back. Let's get into it.")


def build_outro(ca, cb, roles):
    if abs(ca - cb) < 0.01:
        res = "a dead heat"
    elif ca > cb:
        res = f"the {roles['side_a_label']} side"
    else:
        res = f"the {roles['side_b_label']} side"
    return (f"That's three rounds. Across the whole debate the judges gave "
            f"{roles['side_a_label']} {ca:.1f}, and {roles['side_b_label']} {cb:.1f}. "
            f"Tonight's decision goes to {res}. Tell us who you think actually won, "
            f"and we'll see you at the next one.")


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
            system="You reply with one word.")

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
    avail = discover_models() or FALLBACK_MODELS.copy()
    AVAILABLE_MODELS = [m for m in avail if not is_reasoning_model(m)]
    preflight_models(AVAILABLE_MODELS or avail, MIN_PANEL_SIZE)
    ap_model, sk_model = choose_primary_models(AVAILABLE_MODELS or avail)
    roles = get_debate_roles(topic, ap_model)
    print(f"Sides: {roles['side_a_label']} (Brian, left) vs {roles['side_b_label']} (Ava, right)")
    print(f"  A stance: {roles['side_a_stance']}")
    print(f"  B stance: {roles['side_b_stance']}")
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

    cum_a = cum_b = 0.0
    all_results = []
    last_a_text = ""
    last_b_text = ""

    turn_attribution = []
    for rn in range(1, ROUNDS + 1):
        print(f"\n--- ROUND {rn} ---")
        a_turns, b_turns = [], []
        round_writers = set()
        for tn in range(1, TURNS_PER_SIDE_PER_ROUND + 1):
            for side in ("A", "B"):
                turns_left = total_turns - turns_done
                rounds_left = ROUNDS - rn + 1
                reserved = (rounds_left * EST_SCORECARD_SEC
                            + rounds_left * 2 * EST_COMMENTARY_SEC
                            + EST_OUTRO_SEC)
                target = pacing.turn_words(turns_left, reserved)
                opponent_last = last_b_text if side == "A" else last_a_text
                model = ap_model if side == "A" else sk_model
                text, wrote = generate_turn(topic, roles, side, rn, tn, opponent_last,
                                            target, model)
                label = roles["side_a_label"] if side == "A" else roles["side_b_label"]
                round_writers.add(wrote)
                turn_attribution.append({"round": rn, "turn": tn, "side": label,
                                         "model": wrote, "words": count_words(text)})
                substitute = " (substitute)" if wrote != model else ""
                print(f"  R{rn} T{tn} {label}: {count_words(text)}w / target {target}w "
                      f"- written by {get_judge_short_name(wrote)} [{wrote}]{substitute}")
                if side == "A":
                    a_turns.append(text)
                    last_a_text = text
                else:
                    b_turns.append(text)
                    last_b_text = text
                add_seg(text, side, label)
                turns_done += 1

        a_full = "\n\n".join(a_turns)
        b_full = "\n\n".join(b_turns)
        res = evaluate_round(judges, topic, rn, a_full, b_full, roles,
                             recused=round_writers)
        ra, rb = calculate_round_average(res)
        cum_a += ra
        cum_b += rb
        all_results.append({"round": rn, "avg_a": ra, "avg_b": rb, "judges": res})
        print(f"  Round {rn} average: {roles['side_a_label']} {ra:.1f} "
              f"vs {roles['side_b_label']} {rb:.1f}")

        score_spec = prepare_scorecard(rn, res, ra, rb, cum_a, cum_b, roles)
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

    add_seg(build_outro(cum_a, cum_b, roles), "MOD", "MODERATOR", count_speech=False)

    try:
        json.dump({"topic": topic, "sides": roles,
                   "debater_models": {"A": ap_model, "B": sk_model},
                   "judge_models": judges,
                   "turns": turn_attribution,
                   "rounds": all_results,
                   "cumulative": {"A": round(cum_a, 2), "B": round(cum_b, 2)}},
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
