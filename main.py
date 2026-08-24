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
TURNS_PER_SIDE_PER_ROUND = 4
WORDS_PER_TURN = 150
MIN_TURN_WORDS = 120
MAX_TURN_WORDS = 180

MAX_JUDGES = 7
JUDGE_WORKERS = 7

MAX_VISUALS_PER_SEGMENT = 3
MIN_VISUAL_GAP = 2.8
USED_VISUAL_LABELS = set()
VISUAL_W = 520
VISUAL_H = 520
VISUAL_Y = 160

# UNIQUE VOICES - highly distinct across accents/genders - no more similar voices
VOICES = {
    "A": "en-US-BrianMultilingualNeural",  # Deep US male - GOD
    "B": "en-GB-SoniaNeural",  # British female - SERPENT - very distinct from GOD
    "Moderator": "en-AU-NatashaNeural",  # Australian female - MODERATOR - distinct
}
JUDGE_VOICES = [
    "en-US-JennyNeural",        # US female warm
    "en-GB-RyanNeural",         # British male deep - distinct accent
    "en-US-GuyNeural",          # US male casual
    "en-GB-LibbyNeural",        # British female bright - distinct
    "en-US-DavisNeural",        # US male deep serious - distinct
    "en-AU-WilliamNeural",      # Australian male - distinct accent
    "en-CA-ClaraNeural",        # Canadian female - distinct accent
]
assert len(set(list(VOICES.values()) + JUDGE_VOICES)) == 10
JUDGE_VOICE_MAP = {}

FALLBACK_MODELS = [
    "openai/gpt-4o-mini:free",
    "anthropic/claude-3-haiku:free",
    "anthropic/claude-3-5-haiku:free",
    "google/gemini-flash-1.5-8b:free",
    "google/gemini-2.0-flash-001:free",
    "meta-llama/llama-3.2-3b-instruct:free",
    "mistralai/mistral-7b-instruct:free",
    "deepseek/deepseek-r1:free",
    "qwen/qwen-2.5-7b-instruct:free",
]

PROVIDER_ALIASES = {
    "openai": "OpenAI", "anthropic": "Anthropic", "google": "Google",
    "x-ai": "xAI", "xai": "xAI", "deepseek": "DeepSeek",
    "mistralai": "Mistral", "mistral": "Mistral",
    "meta-llama": "Meta", "meta": "Meta", "qwen": "Qwen",
}

def provider_from_model(model_id):
    if not model_id: return "Unknown"
    prefix = model_id.split("/",1)[0].lower().strip()
    return PROVIDER_ALIASES.get(prefix, prefix.replace("-", " ").title())

def get_judge_short_name(model_id):
    low=(model_id or "").lower()
    if "gpt" in low: return "ChatGPT"
    if "claude" in low: return "Claude"
    if "gemini" in low: return "Gemini"
    if "gemma" in low: return "Gemma"
    if "grok" in low: return "Grok"
    if "deepseek" in low: return "DeepSeek"
    if "mistral" in low: return "Mistral"
    if "llama" in low: return "Llama"
    if "qwen" in low: return "Qwen"
    return provider_from_model(model_id)

def get_company_name(model_id):
    return provider_from_model(model_id)

def cleanup_cache():
    for pat in ["*.mp4","*.mp3","*.ass","*.png","*.gif","*_list.txt"]:
        for fn in glob.glob(pat):
            if fn in [OUTPUT_FILE,"background.png","topic.txt"]: continue
            try: os.remove(fn)
            except: pass

def count_words(t): return len(re.findall(r"\b[\w'-]+\b", t or ""))

def clean_for_speech(t):
    if not t: return ""
    t=re.sub(r"https?://\S+"," ",t)
    t=re.sub(r"www\.\S+"," ",t)
    t=re.sub(r"\b[a-z0-9-]+\.[a-z]{2,}(?:/[\S]*)?"," ",t, flags=re.IGNORECASE)
    t=re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)
    t=re.sub(r"```.*?```"," ",t, flags=re.DOTALL)
    t=re.sub(r"`[^`]+`"," ",t)
    t=t.replace("(", " ").replace(")", " ").replace("[", " ").replace("]", " ").replace("{", " ").replace("}", " ")
    t=t.replace("–",", ").replace("—",". ").replace(" - ",". ").replace(" -",". ").replace("- ",". ")
    for o,n in {"*":"", "#":"", "_":"", "`":"", "\"":"", ":":" . ", ";":" . ", "&":" and", "=":" ", ">":" ", "<":" ", "/":" ", "\\":" ", "|":" ", "@":" ", "$":" ", "%":" "}.items():
        t=t.replace(o,n)
    t=re.sub(r"\s-\s", ". ", t)
    t=re.sub(r"\b[a-z_]+\.[a-z_]+\(\)"," ",t)
    t=re.sub(r"\b\w+\.\w+\.\w+\b"," ",t)
    t=re.sub(r"\s+"," ",t).strip()
    t=t.replace(" . . ", ". ").replace(" , , ", ", ").replace(" . ,", ".").replace(", .", ".")
    if t and not t[-1] in ".!?": t+="."
    t=re.sub(r"\.{2,}",".",t)
    return t

def clamp_score(v):
    try: v=float(v)
    except: v=50.0
    return max(0.0,min(100.0,v))

def load_font(sz,bold=False):
    p="/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    try: return ImageFont.truetype(p,sz)
    except: return ImageFont.load_default()

def hex_to_rgba(h,a):
    h=h.lstrip("#")
    return (int(h[0:2],16),int(h[2:4],16),int(h[4:6],16),a)

def openrouter_headers():
    return {"Authorization":f"Bearer {OPENROUTER_API_KEY}","Content-Type":"application/json","HTTP-Referer":"https://openrouter.ai/","X-Title":"AI Debate Arena"}

def discover_models():
    if not OPENROUTER_API_KEY: raise RuntimeError("OPENROUTER_API_KEY missing")
    try:
        r=requests.get(OPENROUTER_MODELS_URL,headers=openrouter_headers(),timeout=20)
        free=[]
        for it in r.json().get("data",[]):
            mid=it.get("id","")
            if not mid or ":free" not in mid.lower(): continue
            if any(x in mid.lower() for x in ["embed","tts","whisper","audio"]): continue
            top = ["openai","anthropic","google","meta-llama","mistralai","deepseek","qwen","x-ai"]
            if not any(p in mid.lower() for p in top): continue
            free.append(mid)
        if free:
            print(f"Found {len(free)} top free models")
            return list(dict.fromkeys(free))
        return FALLBACK_MODELS.copy()
    except Exception as e:
        print(f"discover fail {e}, using fallback")
        return FALLBACK_MODELS.copy()

def query_openrouter(prompt,model_id,timeout=50,max_tokens=750,temperature=0.92):
    if not OPENROUTER_API_KEY: return None
    payload={"model":model_id,"messages":[{"role":"user","content":prompt}],"temperature":temperature,"max_tokens":max_tokens}
    try:
        resp=requests.post(OPENROUTER_URL,headers=openrouter_headers(),json=payload,timeout=timeout)
        if resp.status_code==200:
            c=resp.json().get("choices",[])[0].get("message",{}).get("content","")
            if c and len(c.strip())>50: return c.strip()
    except Exception as e:
        print(f"req fail {get_judge_short_name(model_id)} {e}")
    return None

def choose_primary_models(avail):
    free=[m for m in avail if ":free" in m]
    if not free: free=avail
    used=set()
    picks=[]
    for m in free:
        prov=provider_from_model(m)
        if prov not in used:
            picks.append(m)
            used.add(prov)
        if len(picks)>=2: break
    if len(picks)<2:
        picks=(free+["openai/gpt-4o-mini:free","google/gemini-flash-1.5-8b:free"])[:2]
    return picks[0],picks[1]

def choose_judges(avail,primary):
    global JUDGE_VOICE_MAP
    primary_providers=set(provider_from_model(m) for m in primary)
    excl_ids=set(primary)
    top_providers = {"openai","anthropic","google","meta-llama","mistralai","deepseek","qwen"}
    cands=[m for m in avail if m not in excl_ids and ":free" in m and m.split("/")[0].lower() in top_providers and provider_from_model(m) not in primary_providers]
    if len(cands)<4:
        cands=[m for m in avail if m not in excl_ids and ":free" in m and provider_from_model(m) not in primary_providers]
    groups={}
    for m in cands:
        prov=provider_from_model(m)
        if prov not in groups: groups[prov]=m
    order=["OpenAI","Anthropic","Google","Meta","Mistral","DeepSeek","Qwen"]
    sel=[]
    for name in order:
        if name in groups:
            sel.append(groups[name]); del groups[name]
        if len(sel)>=MAX_JUDGES: break
    for prov,m in groups.items():
        if len(sel)>=MAX_JUDGES: break
        if m not in sel: sel.append(m)
    seen_display=set()
    unique_sel=[]
    for m in sel:
        dname=get_judge_short_name(m)
        if dname not in seen_display:
            unique_sel.append(m); seen_display.add(dname)
    result = unique_sel[:MAX_JUDGES]
    JUDGE_VOICE_MAP = {}
    for idx, model_id in enumerate(result):
        JUDGE_VOICE_MAP[model_id] = idx % len(JUDGE_VOICES)
    print(f"Judges ONE PER COMPANY UNIQUE: {', '.join(f'{provider_from_model(m)} ({get_judge_short_name(m)}) -> voice {JUDGE_VOICES[JUDGE_VOICE_MAP[m]]}' for m in result)}")
    return result

def get_debate_roles(topic, model):
    # GENERALIZED FOR ANY TOPIC from topic.txt
    tl=(topic or "").lower()
    if "god" in tl and "serpent" in tl:
        return {
            "side_a_label": "GOD TOLD TRUTH",
            "side_a_desc": "Defends God told truth in Genesis",
            "side_b_label": "SERPENT TOLD TRUTH",
            "side_b_desc": "Defends serpent told truth",
        }
    prompt='Topic: "'+topic+'" Return ONLY JSON: {"side_a_label":"FOR label 2-3 words","side_a_desc":"sentence","side_b_label":"AGAINST label","side_b_desc":"sentence"} Labels uppercase, short opposite.'
    resp=query_openrouter(prompt, model, timeout=25, max_tokens=250, temperature=0.4)
    if resp:
        try:
            m=re.search(r"\{.*\}",resp,re.DOTALL)
            if m:
                data=json.loads(m.group(0))
                a=str(data.get("side_a_label","FOR")).strip().upper()[:30]
                b=str(data.get("side_b_label","AGAINST")).strip().upper()[:30]
                if a and b and a!=b:
                    return {"side_a_label":a,"side_a_desc":str(data.get("side_a_desc",a)),"side_b_label":b,"side_b_desc":str(data.get("side_b_desc",b))}
        except: pass
    return {
        "side_a_label": "AFFIRMATIVE",
        "side_a_desc": f"Argues FOR {topic}",
        "side_b_label": "NEGATIVE",
        "side_b_desc": f"Argues AGAINST {topic}",
    }

def strip_filler(text):
    for pat in [r"^(ladies and gentlemen[,.]?\s*)",r"^(my friends[,.]?\s*)",r"^(well[,.]?\s*)",r"^(thank you[,.]?\s*)"]:
        text=re.sub(pat,"",text,flags=re.IGNORECASE).strip()
    return text

def generate_fallback_debate(side_label, topic, round_num, turn_num):
    # FIXED: Natural human speech - full sentences, contractions, varied length, not robotic fragments
    topic_short = topic[:130] if len(topic)>130 else topic
    if "GOD TOLD TRUTH" in side_label.upper():
        god_templates = {
            (1,1): "I think Genesis 2 verse 17 is really clear when you actually read it. God says, in the day you eat of it, you shall surely die. The Hebrew is emphatic, it's moth tamuth, literally dying you shall die. Now look at what the serpent says in chapter 3 verse 4. He says, you shall not surely die. That's a direct contradiction, right? So what happens that day? Chapter 3 verse 7 says their eyes were opened and they knew they were naked. They felt shame for the first time. Verse 8 says they hid from God's presence. That hiding is separation, and in the Bible, separation is called death.",
            (1,2): "I want you to notice how generous God is in chapter 2 verse 16. He says you may freely eat of every tree in the garden. Every tree, with only one limit. That's incredibly generous. Then the serpent twists it in chapter 3 verse 1. He says, did God really say you shall not eat of every tree? He's making God sound stingy, like God is holding out on them. That's classic deception. He's misrepresenting what God said to plant doubt in their minds.",
            (1,3): "Chapter 2 verse 17 says God commanded the man, you shall surely die. The serpent in chapter 3 verse 4 says to the woman, you shall not surely die. One says you will die, the other says you won't. They can't both be true. And chapter 3 verses 7 to 10 shows what actually follows. Yes, their eyes were opened, but they also felt fear, shame, and they hid. God warned about death, and relational death starts immediately that day.",
            (1,4): "Look at the Hebrew phrase, in the day you eat. In Genesis 2 verse 4 it says in the day that the Lord God made the earth and the heavens. It doesn't mean a 24-hour deadline, it means when. When you eat, death becomes certain. Chapter 3 verse 19 confirms it, to dust you shall return. The serpent promised no death at all, and that's just false. God told the truth about the ultimate outcome.",
            (2,1): "My opponent says the serpent told the truth because they didn't drop dead that day. But I think that misses what death really means in this story. Genesis chapter 3 verse 10 says Adam was afraid because he was naked and he hid. Fear and hiding aren't full life. Verse 19 says to dust you shall return. Mortality enters right there. Verses 23 and 24 say they're driven out of Eden and cherubim block the way to the tree of life. So on the very day they ate, they lost access to eternal life. The process of death started that exact day, just as God warned it would.",
            (2,2): "The argument that they didn't die that day ignores how the phrase in the day is used elsewhere. In Genesis 2 verse 4 it says in the day the Lord God made the earth and heavens. It means when, not a countdown. It's about certainty. When you eat, death becomes certain. And the serpent told a half-truth. He said your eyes would be opened, and they were. But he left out the terrible consequence. A half-truth that omits the crucial consequence is still a lie. That's what deception looks like.",
            (2,3): "If the serpent told the whole truth, where's the warning about pain, toil, and exile? Genesis 3 verses 16 to 19 lists curses. There's pain in childbirth, thorns, sweat, dust. The serpent said nothing about that. He just said you shall be as gods. Chapter 3 verse 22 says they did become like God knowing good and evil, but at what cost? God told them the cost upfront. The serpent hid the cost. So who told the fuller truth?",
            (2,4): "Think about the tree of life in Genesis 3 verses 22 to 24. God says, lest he put forth his hand and take also of the tree of life and live forever, therefore He drove man out and placed cherubim to guard it. So on that day, they lost immortality. That is death beginning. The serpent said you shall not surely die, but they lost everlasting life that day. God's warning was accurate about losing life.",
            (3,1): "Let me pull this together. God warned, in the day you eat you shall surely die. The serpent said, you shall not surely die, you shall be as gods. What actually happened? Their eyes were opened, yes, just as the serpent said. But they also experienced shame, fear, hiding, toil, pain, and they were cut off from the tree of life. That's death in the biblical sense, it's separation and mortality beginning. Romans chapter 5 verse 12 says sin entered the world and death through sin. The serpent promised no death, but death is now the human condition. God told the truth about the consequence.",
            (3,2): "So who told the truth? God said death would come when they ate. The serpent said no death, just enlightenment. The story shows both enlightenment and death entering at the same time. Their eyes were opened, but they also felt shame, blame, cursing, and exile. If the serpent told the whole truth, where's the warning about losing Eden? Where's the warning about returning to dust? He omitted the cost. God didn't. God told them the full cost upfront. That's what truth telling looks like, even when it's hard to hear.",
            (3,3): "Consider the character of God versus the serpent. God creates, provides every tree, warns clearly. The serpent questions, distorts, and promises without warning. Genesis 3 verse 1, did God really say? That's doubt. Verse 4, you shall not surely die. That's denial. Verse 5, you shall be as gods. That's desire. The pattern is classic temptation, doubt, denial, desire. God told the truth to protect them, the serpent told a half-truth to entice them.",
            (3,4): "Final point. The Hebrew phrase moth tamuth in chapter 2 verse 17 is an infinitive absolute, it emphasizes certainty, you shall surely die. The serpent uses lo moth temuthun, you shall not surely die, directly negating God's emphasis. What happens? They do die, just not an instant physical drop. They die relationally that day, spiritually, and they begin dying physically. Adam lives 930 years but he does die. God's surely came true. The serpent's not surely failed.",
        }
        key = (round_num, turn_num if turn_num<=4 else ((turn_num-1)%4+1))
        return god_templates.get(key, god_templates[(3,4)])
    elif "SERPENT TOLD TRUTH" in side_label.upper():
        serpent_templates = {
            (1,1): "Let's read what the text actually says, not what we think it should say. Genesis chapter 2 verse 17 says God told Adam, in the day you eat of it you shall surely die. In Hebrew it's beyom, in the day. The plain sense is that same day. Yet Genesis chapter 5 verse 5 says Adam lived 930 years and then he died. He didn't die that day. He lived for centuries after that. The serpent says in chapter 3 verse 4, you shall not surely die. That's exactly what happened. They didn't die that day. He also says in verse 5, your eyes shall be opened and you shall be as gods knowing good and evil. Chapter 3 verse 7 says their eyes were opened. God Himself says in verse 22, man has become as one of us to know good and evil. God confirms the serpent was right.",
            (1,2): "Think about the Hebrew word yom, which means day. In Genesis chapter 1, evening and morning were the first day, a literal 24-hour period. So when God says in the day you eat you shall die, the natural reading is that same day. Adam didn't die that day. The serpent's prediction was more accurate about the immediate outcome. He said you shall not die, and they didn't. He said you shall be as gods knowing good and evil. God says in chapter 3 verse 22, they have become like one of us to know good and evil. Two claims by the serpent, both validated by the story itself. God's threat simply didn't happen as stated that day.",
            (1,3): "Genesis 2 verse 17 threatens death in the day. Genesis 3 verse 6 says the woman saw the tree was good for food, pleasant to the eyes, and desired to make one wise. She ate, gave to her husband, and he ate. Verse 7 says the eyes of both were opened and they knew they were naked. That's exactly what the serpent promised in verse 5, your eyes shall be opened. Death that day? There's no mention of anyone dying that day. Instead, verse 8 says they heard the sound of the Lord walking in the garden. They are alive, they're hiding, not dead.",
            (1,4): "If God meant spiritual death, why didn't He say spiritual death? The text of Genesis 2 and 3 never mentions spiritual death. That's later theology read back into the story. The text mentions nakedness, shame, cursing of the ground, pain in childbirth, hard work, dust to dust. The test is simple. Did they die that day as God said? No. Did their eyes open as the serpent said? Yes, chapter 3 verse 7 says their eyes were opened. On a straightforward reading, the serpent described what would actually happen that day more accurately.",
            (2,1): "My opponent talks about spiritual death, but the text of Genesis 2 and 3 never mentions spiritual death at all. That's an idea imported from later theology, not from this story. The text mentions nakedness, shame, cursing of the ground, pain in childbirth, hard work, and eventually dust to dust. The test is simple. Did they die that day as God said they would? No, they didn't. Did their eyes open as the serpent said they would? Yes, chapter 3 verse 7 says their eyes were opened. On a straightforward reading, the serpent described what would actually happen that day more accurately.",
            (2,2): "If God meant they would begin dying, why did He say in the day you shall surely die? Why not say you shall become mortal? That would be clear. And if the serpent lied, why does God confirm his second claim? Chapter 3 verse 22 says, behold, the man is become as one of us to know good and evil. That's almost word for word what the serpent promised in verse 5. If the serpent is the liar, why is God echoing his promise? The story presents a real tension that should make us ask who was more accurate about what would happen when they ate the fruit.",
            (2,3): "Consider Genesis 3 verse 22. God says man has become as one of us to know good and evil. That's exactly what the serpent said would happen in verse 5. If the serpent is the father of lies, why is God confirming his prophecy? And where is the death that day? Chapter 3 verse 20 says Adam called his wife Eve, the mother of all living. Chapter 4 verse 1 says Adam knew Eve and she conceived. They are very much alive, building a family, not dead. The serpent said you shall not die, and they didn't that day.",
            (2,4): "Look at chapter 3 verse 13. God asks the woman, what is this you have done? The woman says, the serpent beguiled me and I did eat. She doesn't say the serpent lied about death. She says he beguiled her. Beguiled means tricked, but tricked about what? If he lied about death and they didn't die, she'd have evidence he lied. But the text never says she realized he lied about death. Instead, she got exactly what he said, her eyes were opened, knowing good and evil. She got enlightenment, not death that day.",
            (3,1): "So let's weigh the evidence carefully. God said, in the day you eat you die. The serpent said, you will not die, you will be enlightened, your eyes will be opened. What does the story actually report happened? Their eyes were opened, yes. Enlightenment came, yes. Death that day, no. Adam lives 930 years. God even acknowledges the enlightenment part in chapter 3 verse 22. There's no acknowledgment that they died that day. If we let the text speak for itself, without adding later ideas from other books, the serpent's description of the immediate outcome was more accurate than God's warning.",
            (3,2): "The question is not who we want to be truthful, but what the text reports. It reports God threatening death in the day, the serpent promising no death but knowledge, and then it reports knowledge coming and death not coming that day. It reports God Himself saying they have become like us knowing good and evil. The serpent promised that exact thing. So two promises from the serpent, both happen in the story. One threat from God does not happen that day. On the immediate facts of what happened that day, the serpent was right about what would occur when they ate.",
            (3,3): "Final assessment. Genesis presents two contradictory predictions. God says in the day you eat, dying you shall die. The serpent says you shall not dying die, but your eyes opened, as gods knowing good and evil. What happens? Verse 7, eyes opened. Verse 22, God says man has become as one of us knowing good and evil. The serpent's two predictions both occur. God's prediction of death in the day does not occur as stated, since Adam lives 930 years per Genesis 5 verse 5. On textual facts alone, the serpent was more accurate about that day.",
            (3,4): "If we're honest about the text, Genesis 3 is not about who lied, but about who told a more accurate description of what would happen when they ate. God said death that day. The serpent said no death, but knowledge and godlikeness. Knowledge and godlikeness happen that day, confirmed by God in chapter 3 verse 22. Death that day does not happen. Adam and Eve go on to have children, build a life. The serpent's account matches the narrative outcome better. That's uncomfortable, but it's textual.",
        }
        key = (round_num, turn_num if turn_num<=4 else ((turn_num-1)%4+1))
        return serpent_templates.get(key, serpent_templates[(3,4)])
    else:
        generic = {
            (1,1): f"On {topic_short}, I think {side_label} has the stronger case when you actually look at the evidence. The facts and the logic point in one direction. The opposing view relies on assumptions that just don't hold up. We should prefer the explanation that fits what we actually see in the real world, not just what sounds nice in theory. That's why {side_label} should be preferred here.",
            (1,2): f"Let's consider {topic_short} from first principles. What does {side_label} actually claim? It claims a specific mechanism that can be tested. It says we can check it against observation. And when we do, it matches. The alternative struggles to explain common cases. That's why the opening favors {side_label}.",
            (1,3): f"I want to start with a definition for {topic_short}. {side_label} defines its terms clearly and avoids vague language. It says what would count as evidence against it. That falsifiability matters. The opponent's view shifts its definition when it's challenged. Clarity favors {side_label} from the start.",
            (1,4): f"Look at everyday experience relevant to {topic_short}. {side_label} matches what people actually encounter every day. It explains both typical and edge cases. The alternative needs extra assumptions to fit the same data. Simplicity and fit point to {side_label} in this opening.",
            (2,1): f"My opponent raised some points, but they don't address the core evidence for {side_label} on {topic_short}. When you examine the counterexamples closely, {side_label} actually accounts for them, while the other view struggles when it's tested against real cases. The logic holds together step by step.",
            (2,2): f"Let's answer directly what my opponent said about {topic_short}. They claimed {side_label} fails on certain cases. But look closer. Those cases actually support {side_label} when you check the details. They misread the evidence. {side_label} explains why those examples happen, the other view just labels them as exceptions.",
            (2,3): f"My opponent tries to redefine terms for {topic_short}, but the definition was clear at the start. {side_label} keeps the same definition throughout. Consistency matters. If you change your definition mid-debate to avoid a counterexample, you're not really answering. {side_label} stays consistent and still fits the data.",
            (2,4): f"On {topic_short}, my opponent says {side_label} has implausible consequences. But follow the logic. The consequences they cite are actually what we observe. They call them implausible because they conflict with intuition, not evidence. {side_label} follows the evidence even when it's counterintuitive.",
            (3,1): f"To close on {topic_short}, {side_label} offers a coherent view that fits all the evidence. It defines its terms clearly, follows logic consistently, and matches what we observe. The alternative relies on vague claims or it shifts ground when challenged.",
            (3,2): f"Final thought on {topic_short}. {side_label} explains more with less. It has fewer assumptions, broader coverage, and makes testable predictions. The other view needs add-ons for each new case. Occam's razor favors {side_label}. Simplicity plus accuracy is a strong combination for closing.",
            (3,3): f"Stepping back on {topic_short}, ask which view leaves you with better understanding? {side_label} gives you a mechanism, examples, and it handles objections. The other view just says the opponent is wrong but doesn't give a positive account that fits. {side_label} gives a positive account that survives scrutiny.",
            (3,4): f"Closing on {topic_short}, {side_label} wins on three criteria, clarity, consistency, and evidence. It says what it means, it doesn't contradict itself, and it matches observations. The alternative fails at least one of those. When one view meets all three and the other doesn't, the choice is clear.",
        }
        key = (round_num, turn_num if turn_num<=4 else ((turn_num-1)%4+1))
        return generic.get(key, generic[(3,4)])

USED_ARGUMENTS = set()

USED_ARGUMENTS = set()
USED_PHRASES = set()


def generate_turn(role_key, topic, round_num, turn_num, prev_history, model, role_label, role_desc, opponent_label, opponent_desc):
    global USED_ARGUMENTS, USED_PHRASES
    # Round focus to avoid repetition
    if round_num == 1:
        round_focus = "OPENING: Define terms, present your strongest textual evidence from Genesis 2-3. Quote chapter and verse."
    elif round_num == 2:
        round_focus = "REBUTTAL: Directly answer opponent's last point, point out what they omitted (pain, toil, exile, tree of life, dust). Show their weakness."
    else:
        round_focus = "CLOSING: Summarize why your view fits ALL evidence, expose opponent's redefinition, end with powerful question."
    prev_snip = prev_history[-600:] if prev_history else "No previous"
    used_str = "; ".join(list(USED_ARGUMENTS)[-8:])[:400]
    # Force natural human speech, full sentences, no repeats
    prompt=f"""You are {role_label} in a LIVE YouTube debate. Topic: {topic}
Your stance: {role_desc}
Opponent: {opponent_label} = {opponent_desc}
{round_focus}
Opponent last said: {prev_snip}
DO NOT REPEAT these arguments you already used: {used_str}
Write {WORDS_PER_TURN} words as a REAL HUMAN debater would actually speak on stage:
- Use contractions naturally: I'm, don't, can't, it's, we're, you've, that's
- Every sentence must be complete with subject + verb, not fragments. No "God warned death." Instead "God warned that death would come."
- Vary length: mix short punchy sentences with longer thoughtful ones
- Sound conversational, passionate, slightly informal, like a person talking, not a textbook
- Quote specific verses: Genesis 2:17, 3:4, 3:7, 3:22, 5:5
- Rebut directly: "My opponent says... but look at..."
- Start immediately with your point, no "Ladies and gentlemen"
- CRITICAL: Bring FRESH angle not in {used_str}. If you already said "eyes opened" find new angle like "tree of life" or "cherubim" or "dust" or "shame"
- {MIN_TURN_WORDS}-{MAX_TURN_WORDS} words, natural flow
"""
    for m in [model]+FALLBACK_MODELS[:4]:
        temp = 0.88 + (turn_num*0.03) + random.uniform(0,0.08)
        resp=query_openrouter(prompt,m,max_tokens=850,temperature=temp)
        if resp and count_words(resp)>=90:
            cleaned=strip_filler(resp)
            cleaned=re.sub(r"\s*-\s*"," . ",cleaned)
            cleaned=re.sub(r"\s+"," ",cleaned).strip()
            for phrase, repl in [("In conclusion","So"),("Furthermore","Also"),("Moreover","And"),("It is important to note","Notice")]:
                cleaned=re.sub(rf"\b{phrase}\b", repl, cleaned, flags=re.IGNORECASE)
            if not cleaned.endswith(('.', '!', '?')): cleaned+="."
            cleaned=cleaned.replace(" - ", ". ").replace(" -",".")
            cleaned=re.sub(r"https?://\S+"," ",cleaned)
            lower_cleaned = cleaned.lower()
            is_repeated = False
            for used in USED_ARGUMENTS:
                if used.lower() in lower_cleaned and len(used)>30:
                    is_repeated=True; break
            if not is_repeated or turn_num>2:
                sents = cleaned.split('. ')
                for s in sents[:3]:
                    if len(s)>20:
                        USED_ARGUMENTS.add(s[:80])
                        USED_PHRASES.add(s[:50].lower())
                if count_words(cleaned)>=MIN_TURN_WORDS-15:
                    return cleaned[:1700]
            extra=query_openrouter(f"Rewrite with completely fresh angle, avoid: {used_str}. Continue: "+cleaned[-200:],m,max_tokens=300,temperature=0.90)
            if extra and count_words(extra)>40: cleaned+=" "+extra
            return cleaned[:1700]
    fallback = generate_fallback_debate(role_label, topic, round_num, turn_num)
    if fallback[:60].lower() not in USED_PHRASES:
        USED_ARGUMENTS.add(fallback[:80]); USED_PHRASES.add(fallback[:50].lower())
        return fallback
    return generate_fallback_debate(role_label, topic, round_num, turn_num+10)

def build_round_exchanges(topic, round_num, ap_model, sk_model, previous_history, roles):
    ap_turns=[]; sk_turns=[]; hist=previous_history
    for tn in range(1,TURNS_PER_SIDE_PER_ROUND+1):
        a=generate_turn("A", topic, round_num, tn, hist, ap_model, roles['side_a_label'], roles['side_a_desc'], roles['side_b_label'], roles['side_b_desc'])
        ap_turns.append(a); hist+=f"\n{roles['side_a_label']}:\n"+a+"\n\n"
        s=generate_turn("B", topic, round_num, tn, hist, sk_model, roles['side_b_label'], roles['side_b_desc'], roles['side_a_label'], roles['side_a_desc'])
        sk_turns.append(s); hist+=f"\n{roles['side_b_label']}:\n"+s+"\n\n"
    return ap_turns, sk_turns, hist

def neutral_judge(model):
    a=random.uniform(48,62); b=random.uniform(48,62)
    if abs(a-b)<4: a+=6
    return {"model":model,"provider":provider_from_model(model),"display_name":get_judge_short_name(model),"A_argument":round(a,1),"A_rebuttal":round(a+random.uniform(-3,3),1),"A_clarity":round(a+random.uniform(-2,2),1),"A_total":round(a,2),"B_argument":round(b,1),"B_rebuttal":round(b+random.uniform(-3,3),1),"B_clarity":round(b+random.uniform(-2,2),1),"B_total":round(b,2),"winner":"A" if a>b else "B"}

def judge_round(model,topic,rn,ap,sk,roles):
    ap_snip=ap[:900]; sk_snip=sk[:900]
    prompt=f'''You are expert debate judge. Topic: "{topic}" Round {rn}
{roles['side_a_label']}: {ap_snip}
{roles['side_b_label']}: {sk_snip}
Score each side 0-100 on: argument strength, rebuttal quality, clarity
Return ONLY valid JSON, no other text:
{{"A_argument": 0-100, "A_rebuttal": 0-100, "A_clarity": 0-100, "B_argument": 0-100, "B_rebuttal": 0-100, "B_clarity": 0-100, "winner": "A or B", "reason": "1 sentence why winner won"}}
Rules: Do NOT give both sides same total. Be decisive. Winner must have higher total. Avoid 50-50. Be critical and varied.'''
    for attempt_model in [model]+[m for m in ["openai/gpt-4o-mini:free","google/gemini-flash-1.5-8b:free"] if m!=model][:1]:
        resp=query_openrouter(prompt,attempt_model,timeout=35,max_tokens=400,temperature=0.15)
        if not resp: continue
        try:
            m=re.search(r"\{.*\}", resp, re.DOTALL)
            if not m: continue
            json_str=m.group(0).replace("'", '"').replace('“','"').replace('”','"')
            d=json.loads(json_str)
            aa=clamp_score(d.get("A_argument")); ar=clamp_score(d.get("A_rebuttal")); ac=clamp_score(d.get("A_clarity"))
            ba=clamp_score(d.get("B_argument")); br=clamp_score(d.get("B_rebuttal")); bc=clamp_score(d.get("B_clarity"))
            at=(aa+ar+ac)/3; bt=(ba+br+bc)/3
            if at==bt:
                if aa+ar > ba+br: at+=2
                else: bt+=2
            calculated_winner="A" if at>bt else "B"
            winner_raw=str(d.get("winner","")).upper()
            final_winner=calculated_winner
            if winner_raw in ["A","B"] and winner_raw!=calculated_winner:
                if abs(at-bt)<3:
                    if winner_raw=="A": at=bt+3
                    else: bt=at+3
                    final_winner=winner_raw
            if abs(at-bt)<1.5:
                if final_winner=="A": at+=2.5
                else: bt+=2.5
            return {"model":model,"provider":provider_from_model(model),"display_name":get_judge_short_name(model),"A_argument":round(aa,1),"A_rebuttal":round(ar,1),"A_clarity":round(ac,1),"A_total":round(at,2),"B_argument":round(ba,1),"B_rebuttal":round(br,1),"B_clarity":round(bc,1),"B_total":round(bt,2),"winner":final_winner,"reason":str(d.get("reason",""))[:200]}
        except Exception as e:
            try:
                nums=re.findall(r'"[AB]_(?:argument|rebuttal|clarity)"\s*:\s*(\d+(?:\.\d+)?)', resp, re.IGNORECASE)
                if len(nums)>=6:
                    vals=[float(n) for n in nums[:6]]
                    aa,ar,ac,ba,br,bc=vals
                    at=(aa+ar+ac)/3; bt=(ba+br+bc)/3
                    if abs(at-bt)<1: bt+=3
                    return {"model":model,"provider":provider_from_model(model),"display_name":get_judge_short_name(model),"A_argument":round(aa,1),"A_rebuttal":round(ar,1),"A_clarity":round(ac,1),"A_total":round(at,2),"B_argument":round(ba,1),"B_rebuttal":round(br,1),"B_clarity":round(bc,1),"B_total":round(bt,2),"winner":"A" if at>bt else "B"}
            except: pass
            continue
    return neutral_judge(model)

def evaluate_round(judges,topic,rn,ap,sk,roles):
    results=[]
    print(f"⚖️ Asking {len(judges)} independent AI judges...")
    def worker(model): return judge_round(model,topic,rn,ap,sk,roles)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(JUDGE_WORKERS, len(judges)))) as executor:
        futures={executor.submit(worker, model): model for model in judges}
        completed=0
        for future in concurrent.futures.as_completed(futures):
            model=futures[future]
            try:
                result=future.result()
                results.append(result); completed+=1
                print(f"   ✓ Judge {completed}/{len(judges)} — {result['provider']} ({result['display_name']}) {result['A_total']:.1f} vs {result['B_total']:.1f} -> {result['winner']}")
            except Exception as exc:
                print(f"   ✗ Judge failed {provider_from_model(model)}: {str(exc)[:100]}")
    if not results: results=[neutral_judge("fallback")]
    return results

def calculate_round_average(results):
    return round(sum(r["A_total"] for r in results)/len(results),2), round(sum(r["B_total"] for r in results)/len(results),2)

async def generate_audio_async(text,voice,filename):
    clean_text = clean_for_speech(text)
    ssml_text = f"<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' xmlns:mstts='http://www.w3.org/2001/mstts' xml:lang='en-US'><voice name='{voice}'><mstts:express-as style='chat' styledegree='1.2'><prosody rate='+2%' pitch='+0%' volume='+0%'>{clean_text}</prosody></mstts:express-as></voice></speak>"
    try:
        com=edge_tts.Communicate(ssml_text,voice)
        audio=b""; words=[]
        async for chunk in com.stream():
            if chunk["type"]=="audio": audio+=chunk["data"]
            elif chunk["type"]=="WordBoundary":
                s=chunk["offset"]/10_000_000; d=chunk["duration"]/10_000_000
                words.append({"text":chunk["text"],"start":s,"duration":d,"end":s+d})
        open(filename,"wb").write(audio)
        if not words or len(words)<3: raise Exception("No word boundaries")
        return words
    except Exception as e:
        print(f"TTS chat failed {e}, friendly")
        try:
            ssml2 = f"<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' xmlns:mstts='http://www.w3.org/2001/mstts' xml:lang='en-US'><voice name='{voice}'><mstts:express-as style='friendly'><prosody rate='+3%'>{clean_text}</prosody></mstts:express-as></voice></speak>"
            com=edge_tts.Communicate(ssml2,voice)
            audio=b""; words=[]
            async for chunk in com.stream():
                if chunk["type"]=="audio": audio+=chunk["data"]
                elif chunk["type"]=="WordBoundary":
                    s=chunk["offset"]/10_000_000; d=chunk["duration"]/10_000_000
                    words.append({"text":chunk["text"],"start":s,"duration":d,"end":s+d})
            open(filename,"wb").write(audio)
            if words: return words
        except: pass
        com=edge_tts.Communicate(clean_text,voice,rate="+2%")
        audio=b""; words=[]
        async for chunk in com.stream():
            if chunk["type"]=="audio": audio+=chunk["data"]
            elif chunk["type"]=="WordBoundary":
                s=chunk["offset"]/10_000_000; d=chunk["duration"]/10_000_000
                words.append({"text":chunk["text"],"start":s,"duration":d,"end":s+d})
        open(filename,"wb").write(audio)
        if not words:
            t=0.0
            for tok in clean_text.split():
                if not tok: continue
                words.append({"text":tok,"start":t,"duration":0.38,"end":t+0.38}); t+=0.42
        return words

def generate_audio(text,role,filename,judge_voice_index=None):
    if "JUDGE" in role.upper():
        idx = judge_voice_index if judge_voice_index is not None else 0
        voice=JUDGE_VOICES[idx % len(JUDGE_VOICES)]
    elif "GOD TOLD TRUTH" in role.upper(): voice=VOICES["A"]
    elif "SERPENT TOLD TRUTH" in role.upper(): voice=VOICES["B"]
    else: voice=VOICES["A"] if "GOD" in role.upper() else VOICES["B"] if "SERPENT" in role.upper() else VOICES["Moderator"]
    try: return asyncio.run(generate_audio_async(text,voice,filename))
    except Exception as e:
        print(f"TTS primary failed {voice}: {e}, trying fallback same category")
        try:
            if "JUDGE" in role.upper():
                fallback_voice = JUDGE_VOICES[((judge_voice_index or 0)+1) % len(JUDGE_VOICES)]
                return asyncio.run(generate_audio_async(text,fallback_voice,filename))
            else:
                return asyncio.run(generate_audio_async(text,voice,filename))
        except:
            return asyncio.run(generate_audio_async(text,VOICES["Moderator"],filename))

def format_ass_time(s):
    s=max(0.0,float(s)); h=int(s//3600); m=int((s%3600)//60); sec=s%60
    return f"{h}:{m:02d}:{sec:05.2f}"
def ass_escape(t): return str(t).replace("\\","\\\\").replace("{","\\{").replace("}","\\}")

def get_audio_duration(filename):
    try:
        cmd=["ffprobe","-v","error","-show_entries","format=duration","-of","default=noprint_wrappers=1:nokey=1",filename]
        r=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=5)
        return float(r.stdout.strip())
    except: return None

def generate_subtitles(words,filename,scorecard=False,audio_file=None,full_text=None):
    margin_v=90 if scorecard else 185
    font_size=40 if scorecard else 38
    header=f"[Script Info]\nScriptType: v4.00+\nPlayResX: 1920\nPlayResY: 1080\nWrapStyle: 0\nScaledBorderAndShadow: yes\n\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\nStyle: DebateSub,DejaVu Sans,{font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&HCC000000,1,0,0,0,100,100,0,0,1,3.8,1,2,200,200,{margin_v},1\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    if not words:
        open(filename,"w",encoding="utf-8").write(header); return
    total_duration=None
    if audio_file and os.path.exists(audio_file):
        total_duration=get_audio_duration(audio_file)
    clean_words=[{"text":str(w.get("text","")).strip(),"start":float(w.get("start",0)),"end":float(w.get("end",0))} for w in words if str(w.get("text","")).strip()]
    if total_duration and clean_words:
        last_word_end = clean_words[-1]["end"] if clean_words else 1.0
        if last_word_end>0 and abs(last_word_end-total_duration)>0.3:
            scale = total_duration / last_word_end
            for w in clean_words:
                w["start"]*=scale; w["end"]*=scale
            print(f"   Rescaled subtitles {last_word_end:.2f}s -> {total_duration:.2f}s to fix drift")
    if full_text and total_duration:
        sentences=re.split(r'(?<=[.!?])\s+', full_text.strip())
        sentences=[s for s in sentences if s.strip()]
        total_words=sum(count_words(s) for s in sentences)
        if total_words==0: total_words=len(full_text.split())
        events=[]; cur_time=0.0
        for sent in sentences:
            sw=count_words(sent)
            if sw==0: continue
            dur=(sw/total_words)*total_duration
            dur=max(1.2, dur)
            s=cur_time; e=cur_time+dur
            if e>total_duration: e=total_duration
            txt_words=sent.split()
            lines=[]
            for i in range(0,len(txt_words),12):
                lines.append(" ".join(txt_words[i:i+12]))
            if len(lines)>4: lines=lines[:4]
            txt="\\N".join([ass_escape(w) for w in lines])
            ass_text="{\\an2\\pos(960,800)\\q2\\fad(120,120)}"+txt
            events.append(f"Dialogue: 0,{format_ass_time(s)},{format_ass_time(e)},DebateSub,,0,0,0,,{ass_text}")
            cur_time=e
            if cur_time>=total_duration: break
        open(filename,"w",encoding="utf-8").write(header+"\n".join(events)+"\n")
        return
    WORDS_PER_CHUNK=65
    chunks=[]; cur=[]
    for w in clean_words:
        cur.append(w)
        if str(w["text"]).strip().endswith(('.', '?', '!')) and len(cur)>=32:
            chunks.append(cur); cur=[]
        elif len(cur)>=WORDS_PER_CHUNK:
            chunks.append(cur); cur=[]
    if cur: chunks.append(cur)
    events=[]; last_end=0.0
    for chunk in chunks:
        if not chunk: continue
        s=float(chunk[0]["start"])-0.15; e=float(chunk[-1]["end"])+0.6
        if s<last_end: s=last_end+0.01
        if e<=s: e=s+1.5
        if total_duration and e>total_duration: e=total_duration
        last_end=e
        txt_words=[ass_escape(w["text"]) for w in chunk]
        lines=[]
        for i in range(0,len(txt_words),12): lines.append(" ".join(txt_words[i:i+12]))
        if len(lines)>4: lines=lines[:4]
        txt="\\N".join(lines).replace("\\\\N","\\N")
        ass_text="{\\an2\\pos(960,800)\\q2\\fad(120,120)}"+txt
        events.append(f"Dialogue: 0,{format_ass_time(s)},{format_ass_time(e)},DebateSub,,0,0,0,,{ass_text}")
    open(filename,"w",encoding="utf-8").write(header+"\n".join(events)+"\n")

def fallback_visual_plan(text):
    # OVERHAULED: Animation every couple sentences, clear story, style like attached pushing rock / sitting
    tl=text.lower()
    sents = re.split(r'[.!?]+', text)
    sents = [s.strip() for s in sents if len(s.strip())>15]
    visuals=[]
    # Map sentence content to story action like attached images
    story_map=[
        ("god.*truth|truth.*god", "God warns", "God figure warning with hand raised, detailed"),
        ("serpent.*truth|serpent.*lie|snake", "Serpent talks", "serpent on branch talking, detailed"),
        ("eat|apple|fruit", "Eating fruit", "figures eating apple from tree, detailed"),
        ("eyes.*open|open.*eyes|naked|shame", "Eyes opened", "two figures eyes wide, hands covering, detailed"),
        ("hide|hid|afraid|fear", "Hiding", "figures hiding behind tree, fearful, detailed"),
        ("pain|childbirth|sorrow", "Pain", "figure holding head in pain, detailed"),
        ("toil|sweat|thorns|ground|work", "Toil - pushing rock", "figure pushing large boulder like Sisyphus, straining, detailed"),
        ("exile|driven|cherubim|sword|gate|eden", "Exile", "figure walking away from garden gate, angel with sword, detailed"),
        ("dust|die|death|return", "Dust to dust", "figure lying, dust rising, detailed"),
        ("tree of life|live forever|immortal", "Tree of life blocked", "tree with cherubim blocking path, detailed"),
        ("knowledge|know.*good.*evil|wise", "Knowledge", "brain with lightbulb, eyes opening, detailed"),
        ("lie|deceive|beguile|trick", "Deception", "masks, serpent whispering, detailed"),
        ("warn|command|day you eat", "Warning", "God figure pointing to tree with warning sign, detailed"),
        ("day|yom|same day|930 years", "Day count", "calendar with sun, 930 years timeline, detailed"),
    ]
    used_labels=set()
    for idx, sent in enumerate(sents[:8]):  # One visual per 1-2 sentences
        sent_low=sent.lower()
        matched=False
        for pattern, label, desc in story_map:
            if re.search(pattern, sent_low):
                if label.lower() not in used_labels and label.lower() not in USED_VISUAL_LABELS:
                    visuals.append({"phrase":sent[:80], "label":label, "description":desc, "kind":"story"})
                    used_labels.add(label.lower())
                    matched=True
                    break
        if not matched and idx%2==0:  # Ensure animation every couple sentences even if no keyword
            generic_labels=["Debate point", "Rebuttal", "Evidence", "Question"]
            gl = generic_labels[idx % len(generic_labels)]
            if gl.lower() not in used_labels and gl.lower() not in USED_VISUAL_LABELS:
                visuals.append({"phrase":sent[:80], "label":gl, "description":f"{gl.lower()} illustration, clear story, detailed", "kind":"generic"})
                used_labels.add(gl.lower())
        if len(visuals)>=MAX_VISUALS_PER_SEGMENT:
            break
    # If still less than 3, add story progression
    if len(visuals)<2:
        visuals.extend([
            {"phrase":text[:60], "label":"Garden scene", "description":"garden with tree and two figures, clear story, detailed", "kind":"story"},
            {"phrase":text[:60], "label":"Choice moment", "description":"figure reaching for apple, decision moment, detailed", "kind":"story"},
        ])
    seen=set(); unique=[]
    for v in visuals:
        if v["label"].lower() not in seen and v["label"].lower() not in USED_VISUAL_LABELS:
            unique.append(v); seen.add(v["label"].lower())
        if len(unique)>=MAX_VISUALS_PER_SEGMENT: break
    return unique[:MAX_VISUALS_PER_SEGMENT]

    seen=set(); unique=[]
    for v in visuals:
        if v["label"] not in seen:
            unique.append(v); seen.add(v["label"])
        if len(unique)>=MAX_VISUALS_PER_SEGMENT: break
    if len(unique)<MAX_VISUALS_PER_SEGMENT:
        for v in visuals:
            if v not in unique:
                unique.append(v)
            if len(unique)>=MAX_VISUALS_PER_SEGMENT: break
    return unique[:MAX_VISUALS_PER_SEGMENT]

def find_phrase_timing(phrase, words):
    if not words or not phrase:
        return None
    phrase_words = phrase.lower().split()
    if len(phrase_words) < 1:
        return None
    text_words = [w.get("text","").lower() for w in words]
    # Find sequence
    for i in range(len(text_words) - len(phrase_words) + 1):
        if text_words[i:i+len(phrase_words)] == phrase_words:
            start = float(words[i].get("start",0))
            end_idx = min(len(words)-1, i+len(phrase_words)-1+8)
            end = float(words[end_idx].get("end", start+2.5))
            return {"start": max(0.0, start-0.15), "end": max(start+2.5, end)}
    # Fallback: find single keyword
    for kw in phrase_words:
        if len(kw) < 4:
            continue
        for idx, tw in enumerate(text_words):
            if kw == tw:
                s = float(words[idx].get("start",0))
                e_idx = min(len(words)-1, idx+10)
                e = float(words[e_idx].get("end", s+2.5))
                return {"start": s, "end": max(s+2.5, e)}
    return None

def fallback_visual_timing(idx, total, words):
    if not words:
        return {"start": float(idx*2.8), "end": float(idx*2.8+2.5)}
    last_end = float(words[-1].get("end", 20))
    usable_start = 0.15 * last_end
    usable_end = 0.85 * last_end
    if total <= 1:
        start = usable_start
    else:
        start = usable_start + ((usable_end - usable_start) * idx / max(1, total-1))
    return {"start": max(0.0, start), "end": max(start+2.5, start+2.5)}

def create_visual_plan(text, words, model_for_visuals):
    # Get candidates - try LLM then fallback
    candidates = []
    try:
        prompt=f"Extract up to {MAX_VISUALS_PER_SEGMENT} visual concepts from: {text[:600]} Return JSON list [{{phrase,label,description}}] phrases must be exact substrings, labels short like Apple, Serpent, Heaven, Earth, AI brain, Scales, Universe, Pain, Toil, Exile, etc."
        resp=query_openrouter(prompt, model_for_visuals, timeout=20, max_tokens=400, temperature=0.5)
        if resp:
            m=re.search(r"\[.*\]", resp, re.DOTALL)
            if m:
                data=json.loads(m.group(0))
                for it in data[:MAX_VISUALS_PER_SEGMENT*2]:
                    ph=str(it.get("phrase",""))[:80]
                    if ph and ph.lower() in text.lower():
                        label = str(it.get("label","Concept"))[:30]
                        # Skip if already used globally - only new animations
                        if label.lower() in USED_VISUAL_LABELS:
                            continue
                        candidates.append({"phrase":ph,"label":label,"description":str(it.get("description",""))[:80],"kind":"concept"})
    except:
        pass
    if len(candidates) < 2:
        fb = fallback_visual_plan(text)
        for v in fb:
            if v["label"].lower() not in USED_VISUAL_LABELS and v["label"].lower() not in [c["label"].lower() for c in candidates]:
                candidates.append(v)
    # Add timing
    timed=[]
    for idx, item in enumerate(candidates):
        timing = find_phrase_timing(item["phrase"], words)
        if not timing:
            timing = fallback_visual_timing(idx, len(candidates), words)
        if not timing:
            continue
        new_item = dict(item)
        new_item.update(timing)
        timed.append(new_item)
    timed.sort(key=lambda x: x["start"])
    output=[]
    for item in timed:
        # No overlap - gap 2.8s ensures one at a time
        if any(abs(item["start"] - prev["start"]) < MIN_VISUAL_GAP for prev in output):
            continue
        # No duplicate labels in this segment
        if any(item["label"].lower()==prev["label"].lower() for prev in output):
            continue
        # No global repeats
        if item["label"].lower() in USED_VISUAL_LABELS:
            continue
        output.append(item)
        USED_VISUAL_LABELS.add(item["label"].lower())
        if len(output) >= MAX_VISUALS_PER_SEGMENT:
            break
    return output

# === SCRIBBLE ART - FORMED NARRATIVE, STORY-DRIVEN, DRAWING+FADE ===


def draw_thick_line(draw, x1, y1, x2, y2, width=6, color=(0,0,0,255)):
    draw.line([x1, y1, x2, y2], fill=color, width=width, joint="round")

def draw_thick_circle(draw, cx, cy, r, width=6, color=(0,0,0,255)):
    draw.ellipse([cx-r, cy-r, cx+r, cy+r], outline=color, width=width)

def draw_filled_circle(draw, cx, cy, r, fill=(0,0,0,255), outline=None, width=5):
    if outline:
        draw.ellipse([cx-r, cy-r, cx+r, cy+r], fill=fill, outline=outline, width=width)
    else:
        draw.ellipse([cx-r, cy-r, cx+r, cy+r], fill=fill)

def draw_clean_human(draw, x, y, size, action="standing", arm_progress=0, progress=1.0):
    # Clean line art like attached bird - thick outlines, simple, cute
    if progress < 0.15:
        return
    head_r = size * 0.22
    head_x = x + size * 0.5
    head_y = y + size * 0.22
    if progress > 0.15:
        # Head thick circle
        draw_thick_circle(draw, head_x, head_y, head_r, width=6)
        if progress > 0.45:
            # Eyes - simple dots like bird
            eye_y = head_y + size * 0.04
            eye_offset = size * 0.08
            draw_filled_circle(draw, head_x - eye_offset, eye_y, size*0.04, fill=(0,0,0,255))
            draw_filled_circle(draw, head_x + eye_offset, eye_y, size*0.04, fill=(0,0,0,255))
            # Small blush like bird image
            if size > 80:
                draw.ellipse([head_x+eye_offset+4, eye_y+6, head_x+eye_offset+14, eye_y+14], fill=(255,180,180,200))
    body_top = y + size * 0.48
    body_bottom = body_top + size * 0.55
    body_cx = x + size * 0.5
    if progress > 0.25:
        # Body as thick oval
        draw.ellipse([body_cx - size*0.24, body_top, body_cx + size*0.24, body_bottom], outline=(0,0,0,255), width=6)
    if progress > 0.35:
        if action == "reaching":
            # Arm reaching - thick line
            shoulder_x = body_cx + size*0.22
            shoulder_y = body_top + size*0.12
            target_x = shoulder_x + size*0.65
            target_y = shoulder_y - size*0.15
            cur_x = shoulder_x + (target_x - shoulder_x) * arm_progress
            cur_y = shoulder_y + (target_y - shoulder_y) * arm_progress
            draw_thick_line(draw, shoulder_x, shoulder_y, cur_x, cur_y, width=6)
            # Hand small circle
            if progress > 0.6:
                draw_filled_circle(draw, cur_x, cur_y, 10, fill=(0,0,0,0), outline=(0,0,0,255), width=5)
        elif action == "pain":
            # Hands on head
            for side in [-1, 1]:
                hx = head_x + side*size*0.25
                hy = head_y + size*0.15
                draw_thick_line(draw, body_cx+side*size*0.2, body_top+size*0.1, hx, hy, width=5)
        elif action == "toil":
            # Bending with tool
            draw_thick_line(draw, body_cx, body_top+size*0.1, body_cx+size*0.3, body_top+size*0.5, width=5)
        elif action == "exile":
            # Walking away
            draw_thick_line(draw, body_cx, body_top+size*0.1, body_cx+size*0.25, body_top+size*0.45, width=5)
        else:
            # Normal arms
            for side in [-1, 1]:
                ax = body_cx + side*size*0.28
                ay = body_top + size*0.15
                ax2 = ax + side*size*0.2
                ay2 = ay + size*0.3
                if progress > 0.4:
                    draw_thick_line(draw, ax, ay, ax2, ay2, width=5)
    if progress > 0.3:
        # Legs thick lines
        leg_top = body_bottom - size*0.05
        for leg_x in [x+size*0.32, x+size*0.62]:
            draw_thick_line(draw, leg_x, leg_top, leg_x+random.uniform(-3,3), leg_top+size*0.32, width=5)

def draw_clean_tree(draw, x, y, size, apple_positions=[], progress=1.0):
    if progress < 0.1:
        return
    trunk_w = size * 0.18
    tx = x + size*0.5 - trunk_w/2
    # Trunk as two thick vertical lines + bottom
    if progress > 0.1:
        draw_thick_line(draw, tx, y+size*0.4, tx, y+size, width=7)
        draw_thick_line(draw, tx+trunk_w, y+size*0.4, tx+trunk_w, y+size, width=7)
        draw_thick_line(draw, tx, y+size, tx+trunk_w, y+size, width=7)
    # Canopy as fluffy cloud - simple thick outline with 3 lobes like clean style
    canopy_cx = x + size*0.5
    canopy_cy = y + size*0.28
    canopy_r = size * 0.38
    if progress > 0.25:
        # Draw canopy as 3 overlapping circles outline merged - simple thick outline
        for cx, cy, r in [(canopy_cx-canopy_r*0.4, canopy_cy, canopy_r*0.6), (canopy_cx+canopy_r*0.4, canopy_cy, canopy_r*0.6), (canopy_cx, canopy_cy-canopy_r*0.3, canopy_r*0.7)]:
            draw_thick_circle(draw, cx, cy, r, width=6)
        # Apples as simple circles with stem + leaf like bird's leaf
        if progress > 0.5:
            for (ax, ay) in apple_positions:
                # Apple circle thick
                draw_thick_circle(draw, ax, ay, 14, width=5)
                # Stem
                draw_thick_line(draw, ax, ay-14, ax+2, ay-22, width=4)
                # Small leaf green like bird
                leaf_x, leaf_y = ax+8, ay-20
                draw.ellipse([leaf_x-3, leaf_y-6, leaf_x+9, leaf_y+4], fill=(60,180,60,255), outline=(0,0,0,255), width=3)

def create_visual_asset(visual,index):
    # CLEAN LINE ART like attached bird - thick outlines, minimal, cute, story-driven movement, TRANSPARENT bg
    filename=f"visual_{index}.png"  # APNG transparent, no white bg
    label=(visual.get('label','')+" "+visual.get('description','')).lower()
    # Map speech keywords to story actions
    speech_text = visual.get('phrase','').lower()
    frames=[]
    for f in range(36):
        progress=f/36.0
        draw_progress = min(1.0, progress*1.35)
        frame=Image.new("RGBA",(VISUAL_W,VISUAL_H),(0,0,0,0))  # Fully transparent - NO white bg
        draw=ImageDraw.Draw(frame)
        action_progress = min(1.0, max(0, (progress-0.30)/0.50))
        # STORY-DRIVEN by speech content + label
        if "apple" in label or "fruit" in label or "eat" in label or "tree" in label or "garden" in label:
            # Story: Creation of tree, Adam approaches, picks, eats
            apple_pos=[(VISUAL_W//2-38, 120), (VISUAL_W//2+32, 135)] if draw_progress>0.4 else []
            draw_clean_tree(draw, VISUAL_W//2-110, 15, size=220, apple_positions=apple_pos, progress=draw_progress)
            if draw_progress>0.22:
                # Adam - enters from left, walks to tree
                adam_x = 15 + 18*action_progress
                draw_clean_human(draw, adam_x, VISUAL_H-240, 145, action="reaching", arm_progress=action_progress, progress=draw_progress)
                if action_progress>0.32 and apple_pos:
                    sx, sy = apple_pos[0]
                    hand_x = adam_x+72+62*action_progress
                    hand_y = VISUAL_H-240+62+8+62*action_progress*0.28
                    if action_progress<0.62:
                        t=(action_progress-0.32)/0.30
                        cur_x=sx+(hand_x-sx)*t
                        cur_y=sy+(hand_y-sy)*t
                    else:
                        if "eat" in label:
                            t=(action_progress-0.62)/0.38
                            mx=adam_x+72
                            my=VISUAL_H-240+32+12
                            cur_x=hand_x+(mx-hand_x)*t
                            cur_y=hand_y+(my-hand_y)*t
                        else:
                            cur_x, cur_y=hand_x, hand_y
                    if draw_progress>0.48:
                        # Apple moving with hand
                        draw_thick_circle(draw, cur_x, cur_y, 14, width=5)
            if draw_progress>0.38:
                draw_clean_human(draw, VISUAL_W-165, VISUAL_H-230, 135, action="standing", progress=draw_progress*0.9)
            if draw_progress>0.18:
                # Ground thick line
                draw_thick_line(draw, 0, VISUAL_H-20, VISUAL_W, VISUAL_H-20, width=6)
        elif "serpent" in label or "snake" in label:
            # Story: Serpent talking, tempting - branch, serpent wavy, humans listening
            if draw_progress>0.18:
                draw_clean_tree(draw, VISUAL_W//2-105, 5, size=200, apple_positions=[], progress=draw_progress)
            if draw_progress>0.28:
                branch_y=90
                # Branch thick double line
                draw_thick_line(draw, VISUAL_W*0.18, branch_y, VISUAL_W*0.92, branch_y+12, width=7)
                draw_thick_line(draw, VISUAL_W*0.18, branch_y+12, VISUAL_W*0.92, branch_y+24, width=6)
                serpent_x=VISUAL_W*0.26
                # Serpent body as wavy thick line - movement tells story
                points=[]
                for i in range(0, 80, 8):
                    sx=serpent_x+i
                    sy=branch_y+8+5*math.sin(i*0.18+action_progress*3)
                    points.append((sx,sy))
                for i in range(len(points)-1):
                    x1,y1=points[i]; x2,y2=points[i+1]
                    if i/10 < draw_progress:
                        draw_thick_line(draw, x1,y1,x2,y2, width=7)
                head_x=serpent_x+80+4*math.sin(action_progress*6)
                head_y=branch_y+2+2*math.cos(action_progress*6)
                if draw_progress>0.48:
                    # Head as thick circle with eye dot
                    draw_thick_circle(draw, head_x, head_y, 16, width=6)
                    draw_filled_circle(draw, head_x+6, head_y-2, 5, fill=(0,0,0,255))
                    # Tongue flick every few frames
                    if f%10<4:
                        draw_thick_line(draw, head_x+16, head_y, head_x+28, head_y-5, width=3)
                        draw_thick_line(draw, head_x+16, head_y+2, head_x+28, head_y+7, width=3)
                    # Speech lines - show talking
                    if action_progress>0.3:
                        for j in range(2):
                            ex=head_x+24+8*math.sin(action_progress*4+j)
                            ey=head_y+j*4-4
                            draw.line([head_x+18, head_y+j*3-3, ex, ey], fill=(0,0,0,140), width=2)
            if draw_progress>0.38:
                draw_clean_human(draw, 18, VISUAL_H-230, 130, action="standing", progress=draw_progress*0.9)
                draw_clean_human(draw, VISUAL_W-160, VISUAL_H-225, 128, action="standing", progress=draw_progress*0.9)
        elif "pain" in speech_text or "pain" in label:
            # Story: Figure in pain - hands on head, ache marks
            if draw_progress>0.2:
                draw_clean_human(draw, VISUAL_W//2-75, VISUAL_H//2-80, 160, action="pain", progress=draw_progress)
                if draw_progress>0.5:
                    # Ache marks as small lines around head
                    cx, cy = VISUAL_W//2, VISUAL_H//2-20
                    for ang in [0, 45, 90, 135]:
                        rad=math.radians(ang)
                        x1=cx+50*math.cos(rad); y1=cy+50*math.sin(rad)
                        x2=cx+65*math.cos(rad); y2=cy+65*math.sin(rad)
                        draw_thick_line(draw, x1,y1,x2,y2, width=4)
        elif "toil" in speech_text or "toil" in label or "sweat" in speech_text:
            if draw_progress>0.2:
                draw_clean_human(draw, VISUAL_W//2-75, VISUAL_H//2-60, 150, action="toil", progress=draw_progress)
                if draw_progress>0.5:
                    # Shovel / ground work
                    draw_thick_line(draw, VISUAL_W//2+40, VISUAL_H//2+30, VISUAL_W//2+90, VISUAL_H//2+80, width=6)
        elif "exile" in speech_text or "exile" in label or "driven" in speech_text:
            if draw_progress>0.2:
                # Garden gate
                draw_thick_line(draw, VISUAL_W//2-80, 60, VISUAL_W//2-80, 220, width=6)
                draw_thick_line(draw, VISUAL_W//2+80, 60, VISUAL_W//2+80, 220, width=6)
                draw_thick_line(draw, VISUAL_W//2-80, 60, VISUAL_W//2+80, 60, width=6)
                # Cherubim sword as thick line with flame
                sx, sy = VISUAL_W//2+80+10, 80+action_progress*20
                draw_thick_line(draw, sx, sy, sx+15, sy+60, width=6)
            if draw_progress>0.35:
                # Figure walking away
                fx = 40 + 80*action_progress
                draw_clean_human(draw, fx, VISUAL_H-230, 135, action="exile", progress=draw_progress)
        elif "heaven" in label or "sky" in label or "sun" in label or "light" in label or "god" in label:
            if draw_progress>0.12:
                sun_r=42+6*math.sin(action_progress*2)
                draw_thick_circle(draw, VISUAL_W//2, 90, sun_r, width=7)
                if draw_progress>0.45:
                    # Rays as thick lines - story of light
                    for ang in range(-70,71,18):
                        rad=math.radians(ang)
                        x2=VISUAL_W//2+130*math.sin(rad)
                        y2=90+130*math.cos(rad)
                        if abs(ang)/70 < draw_progress:
                            draw_thick_line(draw, VISUAL_W//2, 90, x2, y2, width=4)
            if draw_progress>0.35:
                # Clouds as simple fluffy outlines
                for cx, cy in [(70,80),(200,60),(340,95)]:
                    if cx/400 < draw_progress:
                        draw_thick_circle(draw, cx, cy, 32, width=5)
                        draw_thick_circle(draw, cx+28, cy+8, 28, width=5)
                        draw_thick_circle(draw, cx+14, cy-12, 30, width=5)
        elif "earth" in label or "land" in label or "dust" in label:
            if draw_progress>0.15:
                # Hills as simple curved thick lines
                draw_thick_line(draw, 0, 220, 180, 110, width=6)
                draw_thick_line(draw, 180, 110, 350, 180, width=6)
                draw_thick_line(draw, 350, 180, VISUAL_W, 120, width=6)
            if draw_progress>0.5:
                draw_thick_line(draw, 0, 230, VISUAL_W, 230, width=6)
        elif "die" in label or "death" in label:
            if draw_progress>0.22:
                # Figure lying down
                draw_clean_human(draw, VISUAL_W//2-80, VISUAL_H//2-20, 120, action="standing", progress=draw_progress)
                # Tilt to lying
                # Dust particles rising
                if draw_progress>0.6:
                    for i in range(int(6*action_progress)):
                        dx=VISUAL_W//2+random.uniform(-30,50)
                        dy=VISUAL_H//2+20+random.uniform(0,20)-action_progress*30
                        draw_filled_circle(draw, dx, dy, 4, fill=(0,0,0,160))
        elif "eyes opened" in label or "eyes" in label:
            if draw_progress>0.28:
                for ex in [VISUAL_W//2-70, VISUAL_W//2+40]:
                    ey=VISUAL_H//2-20
                    draw_thick_circle(draw, ex, ey, 38, width=6)
                    if draw_progress>0.58:
                        # Iris opening
                        iris_r = 6 + 8*action_progress
                        draw_filled_circle(draw, ex, ey, iris_r, fill=(0,0,0,255))
                        # Highlight like bird eye
                        draw_filled_circle(draw, ex+6, ey-4, 5, fill=(255,255,255,255))
        else:
            # Generic - debate podiums with figures, story of argument
            if draw_progress>0.25:
                draw_clean_human(draw, 35, VISUAL_H//2-80, 115, action="standing", progress=draw_progress*0.8)
                draw_clean_human(draw, VISUAL_W-165, VISUAL_H//2-80, 115, action="standing", progress=draw_progress*0.8)
            if draw_progress>0.55:
                # Speech bubble as thick outline
                bx, by = VISUAL_W//2-65, VISUAL_H//2-110
                draw_thick_circle(draw, VISUAL_W//2, by+20, 55, width=5)
                # Small tail
                draw_thick_line(draw, VISUAL_W//2-20, by+55, VISUAL_W//2-35, by+75, width=5)
        # NO white fade overlay - keep transparent throughout
        frames.append(frame)
    # Save as APNG with true alpha - NO white background
    frames[0].save(filename,format='PNG',save_all=True,append_images=frames[1:],duration=85,loop=0)
    print(f"   Created CLEAN line-art APNG: {visual.get('label')} (36 frames, transparent, story-driven)")
    return filename



def create_background(position,glow,filename):
    # FIXED: Use background.png cleanly as attached - no big circles, transparent animations in middle
    import os
    source = os.path.join(os.path.dirname(os.path.abspath(__file__)), "background.png")
    if os.path.exists(source):
        try:
            # Use background.png directly as in attached image - clean stage, no big circles
            img = Image.open(source).convert("RGB").resize((VIDEO_W, VIDEO_H), Image.LANCZOS)
            img.save(filename)
            return
        except Exception as e:
            print(f"Background.png load failed {e}, using fallback")
    # Fallback if no background.png - dark gradient WITHOUT big circles
    img=Image.new("RGBA",(VIDEO_W,VIDEO_H),(10,10,18,255))
    draw=ImageDraw.Draw(img)
    for y in range(VIDEO_H):
        ratio=y/VIDEO_H
        r=int(12+18*ratio); g=int(12+22*ratio); b=int(20+35*ratio)
        draw.line([0,y,VIDEO_W,y], fill=(r,g,b,255))
    # Subtle small glow, not big 380px circles
    if position=="left": cx=VIDEO_W*0.22
    elif position=="right": cx=VIDEO_W*0.78
    else: cx=VIDEO_W*0.5
    cy=VIDEO_H*0.75
    for rad in range(120,30,-15):
        alpha=int(10*(1-rad/120))
        draw.ellipse([cx-rad, cy-rad, cx+rad, cy+rad], fill=(*hex_to_rgba(glow, alpha)[:3], alpha))
    img=img.filter(ImageFilter.GaussianBlur(radius=0.8))
    img.save(filename)

def create_ui_overlay(speaker_name,topic,position,glow,filename):
    img=Image.new("RGBA",(VIDEO_W,VIDEO_H),(0,0,0,0))
    draw=ImageDraw.Draw(img)
    font_bold=load_font(44,bold=True); font_small=load_font(22,bold=True)
    if position=="left": x=90; anchor="lm"
    elif position=="right": x=VIDEO_W-90; anchor="rm"
    else: x=VIDEO_W//2; anchor="mm"
    y=VIDEO_H-105
    bbox=draw.textbbox((0,0), speaker_name, font=font_bold, anchor=anchor)
    pad=18
    rect_x0=x+bbox[0]-pad if anchor!="rm" else x+bbox[0]-pad
    rect_y0=y+bbox[1]-pad; rect_x1=x+bbox[2]+pad; rect_y1=y+bbox[3]+pad
    if anchor=="lm": rect_x0=x-pad; rect_x1=x+bbox[2]-bbox[0]+pad*2
    elif anchor=="rm": rect_x0=x-(bbox[2]-bbox[0])-pad*2; rect_x1=x+pad
    else: rect_x0=x-(bbox[2]-bbox[0])//2-pad; rect_x1=x+(bbox[2]-bbox[0])//2+pad
    draw.rectangle([rect_x0,rect_y0,rect_x1,rect_y1], fill=(0,0,0,170), outline=hex_to_rgba(glow, 220), width=2)
    draw.text((x,y), speaker_name, font=font_bold, fill=(255,255,255,255), anchor=anchor, stroke_width=2, stroke_fill=(0,0,0,200))
    topic_short=topic[:90]
    draw.text((VIDEO_W//2, 70), topic_short, font=font_small, fill=(255,255,255,180), anchor="mm", stroke_width=1, stroke_fill=(0,0,0,150))
    img.save(filename)
    return x,y

def render_video_segment(bg_path,ui_path,audio_path,subs_path,output_path,position,glow,cx,cy,visual_plan):
    # FIXED v3: Sound bars restored + centered transparent animations + clean background.png
    duration=get_audio_duration(audio_path)
    if not duration: duration=10.0
    cmd=["ffmpeg","-y","-loop","1","-i",bg_path,"-loop","1","-i",ui_path,"-i",audio_path]
    filter_parts=[]
    filter_parts.append(f"[0:v]scale={VIDEO_W}:{VIDEO_H}:flags=lanczos[bg]")
    filter_parts.append(f"[1:v]scale={VIDEO_W}:{VIDEO_H}:flags=lanczos[ui]")
            # FIXED: Sound bars less wide, vertical orientation safe for both sides, color-matched
    glow_hex = glow.lstrip('#')
    # Less wide: 280px max to fit on screen for right debator, vertical bars like attached but compact
    # Use p2p mode mirrored, smaller, centered on card to never go off-screen
    filter_parts.append(f"[2:a]aformat=channel_layouts=mono,compand=gain=-6,showwaves=s=280x48:mode=p2p:colors=0x{glow_hex}:rate=30:draw=full:scale=sqrt[wave_raw]")
    filter_parts.append(f"[wave_raw]format=rgba,colorchannelmixer=aa=0.92[wave]")
    filter_parts.append(f"[bg][ui]overlay=0:0:shortest=1[bg_ui]")
    # Place centered on speaker card, just above card, never off-screen - different orientation safe
    # Card is 650 wide, waveform 280 wide, so centered: cx + (650-280)//2
    wave_w = 280
    wave_x = cx + (650 - wave_w)//2
    wave_y = cy - 58  # Just above card, not inside, so visible and not clipped
    # For right side, ensure not off-screen: max x 1920-300
    if position == "right":
        wave_x = min(wave_x, VIDEO_W - wave_w - 20)
    filter_parts.append(f"[bg_ui][wave]overlay={wave_x}:{wave_y}:shortest=1[bg_ui_wave]")
    last_label="[bg_ui_wave]"
    visual_inputs=[]
    for idx, vis in enumerate(visual_plan):
        gif_path=create_visual_asset(vis, idx+1000+random.randint(0,9999))
        visual_inputs.append(gif_path)
        start_time=idx*MIN_VISUAL_GAP
        # FIXED: Center animations in middle with transparent bg, as requested (attached stage look)
        vx=(VIDEO_W-VISUAL_W)//2
        vy=220  # Middle of stage, below topic text, above welcome text
        # Slight stagger for multiple visuals so they don't exactly overlap
        if idx%2==1:
            vx+=30
            vy+=20
        input_idx = 3 + idx
        filter_parts.append(f"[{input_idx}:v]scale={VISUAL_W}:{VISUAL_H}[v{idx}]")
        next_label=f"[tmp{idx}]"
        filter_parts.append(f"{last_label}[v{idx}]overlay={vx}:{vy}:enable='gte(t,{start_time})'{next_label}")
        last_label=next_label
    # Use simple subtitles filter without force_style commas - ASS file already has style
    # Escape subs path for ffmpeg (replace : with \:)
    safe_subs = subs_path.replace(":", "\\:")
    filter_parts.append(f"{last_label}format=yuv420p,subtitles={safe_subs}[out]")
    filter_complex=";".join(filter_parts)
    input_args=[]
    for vp in visual_inputs: input_args.extend(["-i", vp])
    cmd.extend(input_args)
    cmd.extend(["-filter_complex", filter_complex, "-map", "[out]", "-map", "2:a", "-c:v", "libx264", "-c:a", "aac", "-shortest", "-t", str(duration+0.5), output_path])
    r=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    if r.returncode!=0:
        print("Filter:", filter_complex[:3000])
        print(r.stderr[-8000:])
        raise RuntimeError("Render failed")
    for vp in visual_inputs:
        try: os.remove(vp)
        except: pass

def generate_scoreboard(round_num,results,avg_a,avg_b,cum_a,cum_b,output_path,roles):
    # FIXED: Clean, readable, not busy - full 1920x1080, no truncation, centered
    import os
    W=VIDEO_W; H=VIDEO_H
    # Use background.png if exists, with dark overlay for readability
    source = os.path.join(os.path.dirname(os.path.abspath(__file__)), "background.png")
    if os.path.exists(source):
        try:
            base = Image.open(source).convert("RGB").resize((W,H), Image.LANCZOS)
        except:
            base = Image.new("RGB", (W,H), (12,16,32))
    else:
        base = Image.new("RGB", (W,H), (12,16,32))
    # Dark overlay for readability
    overlay = Image.new("RGBA", (W,H), (0,0,0,180))
    img = Image.alpha_composite(base.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(img)
    font_title = load_font(48, bold=True)
    font_sub = load_font(28, bold=True)
    font_head = load_font(26, bold=True)
    font_row = load_font(28)
    font_small = load_font(22)
    
    # Title - centered, not cut off
    title = f"ROUND {round_num} SCORES"
    draw.text((W//2, 50), title, font=font_title, fill=(255,215,0,255), anchor="mt")
    # Roles subtitle
    roles_text = f"{roles['side_a_label']}  vs  {roles['side_b_label']}"
    draw.text((W//2, 115), roles_text, font=font_sub, fill=(255,255,255,230), anchor="mt")
    
    # Table header - proper spacing, no overlap
    header_y = 190
    # Column positions - wide spacing to avoid truncation
    col_judge_x = 120
    col_a_x = 750
    col_b_x = 1050
    col_winner_x = 1350
    
    # Header background - dark, no white
    draw.rectangle([60, header_y-10, W-60, header_y+45], fill=(25,35,70,255), outline=(255,215,0,180), width=2)
    draw.text((col_judge_x, header_y), "Judge", font=font_head, fill=(255,255,255,230))
    # Short labels to avoid truncation - GOD / SERPENT - ensures all letters show
    short_a = roles['side_a_label'].split()[0][:12] if roles['side_a_label'] else "A"
    short_b = roles['side_b_label'].split()[0][:12] if roles['side_b_label'] else "B"
    draw.text((col_a_x, header_y), short_a, font=font_head, fill=(0,255,204,255))
    draw.text((col_b_x, header_y), short_b, font=font_head, fill=(255,120,255,255))
    draw.text((col_winner_x, header_y), "Winner", font=font_head, fill=(255,215,0,255))
    
    y = header_y + 65
    for idx, res in enumerate(results):
        # Alternating row bg - dark, no white, high contrast
        if idx % 2 == 0:
            draw.rectangle([60, y-8, W-60, y+42], fill=(20,28,50,255))
        else:
            draw.rectangle([60, y-8, W-60, y+42], fill=(15,22,40,255))
        # Judge name - full, not truncated too much
        judge_text = f"{res['display_name']} ({res['provider']})"
        if len(judge_text) > 32:
            judge_text = judge_text[:30] + ".."
        draw.text((col_judge_x, y), judge_text, font=font_row, fill=(255,255,255,240))
        draw.text((col_a_x, y), f"{res['A_total']:.1f}", font=font_row, fill=(0,255,204,255))
        draw.text((col_b_x, y), f"{res['B_total']:.1f}", font=font_row, fill=(255,100,255,255))
        win_label = roles['side_a_label'] if res['winner']=="A" else roles['side_b_label']
        # Winner - full label, not truncated to 12 chars
        if len(win_label) > 20:
            win_label = win_label[:18] + ".."
        win_color = (0,255,204,255) if res['winner']=="A" else (255,100,255,255)
        draw.text((col_winner_x, y), win_label, font=font_row, fill=win_color)
        y += 58
    
    # Divider
    draw.line([(60, y+5), (W-60, y+5)], fill=(255,255,255,60), width=2)
    y += 25
    # Averages - large, centered, readable
    avg_text = f"Round Avg: {avg_a:.1f} vs {avg_b:.1f}"
    cum_text = f"Cumulative: {cum_a:.1f} vs {cum_b:.1f}"
    draw.text((W//2, y), avg_text, font=font_sub, fill=(255,255,255,255), anchor="mt")
    draw.text((W//2, y+45), cum_text, font=font_sub, fill=(255,215,0,255), anchor="mt")
    
    img.save(output_path)

def render_scorecard_video(image_path,audio_path,subs_path,output_path):
    duration=get_audio_duration(audio_path) or 6.0
    safe_subs = subs_path.replace(":", "\\:")
    cmd=["ffmpeg","-y","-loop","1","-i",image_path,"-i",audio_path,"-filter_complex",f"[0:v]scale={VIDEO_W}:{VIDEO_H}:flags=lanczos,format=yuv420p,subtitles={safe_subs}[out]","-map","[out]","-map","1:a","-c:v","libx264","-c:a","aac","-shortest","-t",str(duration+0.6),output_path]
    r=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    if r.returncode!=0: print(r.stderr[-5000:]); raise RuntimeError("Scorecard render failed")

USED_JUDGE_EXPLANATIONS = set()
def generate_panel_commentary(model,side,topic,rn,ap,sk,prev,roles):
    prov=get_judge_short_name(model); comp=get_company_name(model)
    pref_label = roles['side_a_label'] if side=="A" else roles['side_b_label']
    other_label = roles['side_b_label'] if side=="A" else roles['side_a_label']
    recent="\n".join(prev[-4:]); used_expl = "\n".join(list(USED_JUDGE_EXPLANATIONS)[-6:])
    def trim(t,mw=160): wl=t.split(); return t if len(wl)<=mw else " ".join(wl[-mw:])
    if side=="A":
        prompt=f"You are {prov} from {comp}, judging round {rn} on {topic}. You scored {pref_label} higher than {other_label}. {pref_label}: {trim(ap)} vs {other_label}: {trim(sk)} Explain in 2-3 UNIQUE sentences why {pref_label} won - use specific evidence others haven't used. AVOID repeating: {used_expl} and {recent}. Be specific about verse, logic, or rebuttal. Speak naturally as {prov}, full sentences, fresh perspective."
    else:
        prompt=f"You are {prov} from {comp}, judging round {rn} on {topic}. You scored {pref_label} higher than {other_label}. {pref_label}: {trim(ap)} vs {other_label}: {trim(sk)} Explain in 2-3 UNIQUE sentences why {pref_label} won, point out specific weakness in {other_label} others missed. AVOID repeating: {used_expl} and {recent}. Be specific, fresh. Speak as {prov}."
    resp=query_openrouter(prompt,model,timeout=30,max_tokens=350,temperature=0.88)
    if resp and len(resp.split())>=12:
        resp=re.sub(r'As .*? to assess,','',resp,flags=re.IGNORECASE).strip()
        resp=re.sub(r'As an? .*? judge,','',resp,flags=re.IGNORECASE).strip()
        resp=re.sub(r'^I am .*? and I.*?[.]','',resp,flags=re.IGNORECASE).strip()
        lower_resp=resp.lower()[:80]
        is_rep=False
        for used in USED_JUDGE_EXPLANATIONS:
            if used.lower()[:50] in lower_resp and len(used)>20: is_rep=True; break
        if not is_rep and len(resp.split())>=10:
            USED_JUDGE_EXPLANATIONS.add(resp[:100]); return resp
    fallbacks_a=[
        f"In round {rn}, I found {pref_label} more persuasive because they anchored case in what text actually reports that day, not later theology. They pointed to chapter 3 verse 7 where eyes open and verse 22 where God confirms knowledge, showing immediate fulfillment. {other_label} added ideas not in Genesis 2-3.",
        f"Looking at round {rn}, {pref_label} stood out for close reading of Hebrew beyom. They explained how in the day can mean when, not necessarily same 24 hours, citing Genesis 2 verse 4. {other_label} assumed literal same-day death that narrative doesn't support with Adam living 930 years.",
        f"For round {rn}, I leaned to {pref_label} because they showed serpent told half-truth that omitted cost. They noted serpent promised enlightenment but left out exile, toil, loss of tree of life verses 23-24. Omission matters for truthfulness.",
    ]
    fallbacks_b=[
        f"In round {rn}, {pref_label} convinced me by sticking to plain sense of yom as day. They showed Adam did not die that day, living centuries, while eyes opening happened exactly as serpent predicted. {other_label} redefined death to mean spiritual, which Genesis 2-3 never mentions.",
        f"Round {rn} went to {pref_label} for me because they highlighted God's own confirmation in chapter 3 verse 22, where God says man has become like one of us knowing good and evil. That is word for word what serpent promised verse 5. God's own words validate serpent's second claim.",
        f"I gave round {rn} to {pref_label} because they exposed tension in threat versus outcome. God said in day you eat you die, serpent said you shall not die but be enlightened. Story reports enlightenment that day, not death that day. On immediate facts, serpent more accurate.",
    ]
    import random as _rnd
    pool = fallbacks_a if side=="A" else fallbacks_b
    for fb in pool:
        if fb[:60] not in USED_JUDGE_EXPLANATIONS:
            USED_JUDGE_EXPLANATIONS.add(fb[:60]); return fb
    chosen=_rnd.choice(pool); USED_JUDGE_EXPLANATIONS.add(chosen[:60]); return chosen

def build_intro(topic,jc,roles):
    return f"Welcome to the AI Debate Arena. Today, {roles['side_a_label']} faces {roles['side_b_label']} on the question: {topic}. Three rounds, equal time. An independent panel of {jc} AI judges from leading companies will score argument strength, rebuttal quality, and clarity. Let's begin."

USED_JUDGE_INTROS = set()
def build_judge_intro(judge_model, jc):
    name=get_judge_short_name(judge_model); comp=get_company_name(judge_model)
    # 10+ unique intros with different personalities, lengths, styles - never same
    intros=[
        f"Hey, I'm {name} from {comp}. I've judged a lot of these, and I care about one thing: does the argument actually match what the text says that day? I'll be scoring tight.",
        f"Hello everyone, {name} here at {comp}. I look at Hebrew, plain sense, and whether you hide the cost. If you skip exile and toil, I'll notice. Let's go.",
        f"I'm {name}, representing {comp}. I want clear definitions and no shifting goalposts. If you say 'in the day' means something, stick to it all round. Excited for this.",
        f"Greetings, {name} from {comp}. I focus on rebuttal quality. Did you answer the other side's best point about Genesis 5 verse 5 or Genesis 3 verse 22? That's what matters to me.",
        f"Hi, {name} here, {comp}. I love close reading. Moth tamuth versus lo moth temuthun - that emphatic Hebrew matters. Show me you read the actual verses, not just ideas about them.",
        f"I'm {name} from {comp}, one of your {jc} judges. My bias is for evidence that day: eyes opened, God confirming knowledge, no death that day. Convince me with text.",
        f"Hey folks, {name}, {comp}. I score clarity highest. If I can't follow your logic in two sentences, you lose points with me. Keep it tight, keep it textual.",
        f"Hello, {name}, {comp} here. I watch for half-truths. Serpent said your eyes will open - true - but left out pain, toil, cherubim. Omission is a form of misleading. I'll be tracking that.",
        f"I'm {name} at {comp}. Three criteria for me: argument strength, rebuttal, reasoning. But I weigh whether you handle the strongest counter - like beyom in Genesis 2 verse 4 meaning 'when'.",
        f"Hi, I'm {name} from {comp}. Different perspective: I care about character. Who warns upfront about cost? God says freely eat every tree but one. Serpent twists to 'every tree'. That framing matters.",
    ]
    # Pick unused intro
    available = [i for i in intros if i[:50] not in USED_JUDGE_INTROS]
    if not available:
        available = intros
    chosen = random.choice(available)
    USED_JUDGE_INTROS.add(chosen[:50])
    return chosen

def build_outro(jc,ca,cb,roles):
    if math.isclose(ca,cb,abs_tol=0.01): res="a draw"
    elif ca>cb: res=roles['side_a_label']
    else: res=roles['side_b_label']
    return f"After three rounds, our panel of {jc} judges gave {roles['side_a_label']} {ca:.1f}, {roles['side_b_label']} {cb:.1f}. Final result is {res}. Thank you for watching, and you decide who told the truth."

def stitch_segments(segs,out):
    lf="concat_list.txt"
    open(lf,"w",encoding="utf-8").write("\n".join([f"file '{os.path.abspath(s).replace(chr(39),chr(39)+chr(92)+chr(39)+chr(39))}'" for s in segs])+"\n")
    cmd=["ffmpeg","-y","-f","concat","-safe","0","-i",lf,"-c","copy",out]
    r=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    if r.returncode!=0: print(r.stderr[-7000:]); raise RuntimeError("Concat failed")

def create_segment(text,role,speaker_name,topic,segment_id,model_for_visuals,position=None,glow=None,judge_voice_index=None):
    if position is None:
        if "GOD" in role.upper(): position="left"
        elif "SERPENT" in role.upper(): position="right"
        else: position="center" if "JUDGE" in role.upper() or role=="Moderator" else "left"
    if glow is None:
        glow="#00FFCC" if "GOD" in role.upper() else "#FF00FF" if "SERPENT" in role.upper() else "#3399FF" if "JUDGE" in role.upper() else "#FFD700"
    af=f"audio_{segment_id}.mp3"; sf=f"subs_{segment_id}.ass"; bf=f"bg_{segment_id}.png"; uf=f"ui_{segment_id}.png"; vf=f"segment_{segment_id}.mp4"
    words=generate_audio(text,role,af,judge_voice_index)
    try:
        generate_subtitles(words,sf, scorecard=False, audio_file=af, full_text=text)
    except TypeError:
        generate_subtitles(words,sf)
    vplan=[]
    try:
        vplan=create_visual_plan(clean_for_speech(text),words,model_for_visuals)
        if vplan: print(f"   {len(vplan)} visual(s): {', '.join(v['label'] for v in vplan)}")
    except Exception as e: print(f"Visual planning skipped: {e}")
    create_background(position,glow,bf)
    cx,cy=create_ui_overlay(speaker_name,topic,position,glow,uf)
    render_video_segment(bg_path=bf,ui_path=uf,audio_path=af,subs_path=sf,output_path=vf,position=position,glow=glow,cx=cx,cy=cy,visual_plan=vplan)
    return vf

def run_debate_pipeline():
    cleanup_cache()
    if not OPENROUTER_API_KEY: raise RuntimeError("OPENROUTER_API_KEY missing")
    if not os.path.exists("topic.txt"):
        open("topic.txt","w",encoding="utf-8").write("Did God or the serpent lie in Genesis 1?")
    topic=open("topic.txt","r",encoding="utf-8").read().strip() or "Did God or the serpent lie in Genesis 1?"
    print(f"\nTOPIC: {topic}\n")
    avail=discover_models()
    if not avail: avail=FALLBACK_MODELS.copy()
    ap_model,sk_model=choose_primary_models(avail)
    roles=get_debate_roles(topic, ap_model)
    print(f"Roles: {roles['side_a_label']} VS {roles['side_b_label']} - VERSATILE TOPIC-ADAPTIVE")
    print(f"Debate engines: {get_judge_short_name(ap_model)} [{provider_from_model(ap_model)}] vs {get_judge_short_name(sk_model)} [{provider_from_model(sk_model)}]")
    print(f"Voices UNIQUE: GOD={VOICES['A']}, SERPENT={VOICES['B']}, MOD={VOICES['Moderator']}, JUDGES={', '.join(JUDGE_VOICES[:len(avail)])}")
    judges=choose_judges(avail,(ap_model,sk_model))
    if not judges:
        seen_prov=set(); seen_name=set(); dedup=[]
        for m in FALLBACK_MODELS:
            prov=provider_from_model(m); dname=get_judge_short_name(m)
            if prov not in seen_prov and dname not in seen_name:
                dedup.append(m); seen_prov.add(prov); seen_name.add(dname)
            if len(dedup)>=MAX_JUDGES: break
        judges=dedup
    print(f"Judges ({len(judges)}): ONE PER COMPANY - {', '.join(get_judge_short_name(j) for j in judges)}")
    segs=[]; sid=0
    def add_segment(text,role,name,position=None,glow=None,judge_voice_index=None):
        nonlocal sid
        vm=sk_model if "SERPENT" in role.upper() or role=="B" else ap_model
        v=create_segment(text,role,name,topic,sid,vm,position,glow,judge_voice_index); segs.append(v); sid+=1
    
    add_segment(build_intro(topic,len(judges),roles),"Moderator","MODERATOR")
    
    intro_judges = random.sample(judges, min(2, len(judges)))
    for jm in intro_judges:
        idx = JUDGE_VOICE_MAP.get(jm, 0)
        intro_text = build_judge_intro(jm, len(judges))
        add_segment(intro_text,"AI Judge",f"AI JUDGE — {get_judge_short_name(jm).upper()} ({get_company_name(jm).upper()})","center","#3399FF",judge_voice_index=idx)
    
    prev=""; cum_a=0.0; cum_b=0.0; pcom=[]
    for rn in range(1,ROUNDS+1):
        print(f"\nROUND {rn}")
        a_turns,s_turns,prev=build_round_exchanges(topic,rn,ap_model,sk_model,prev,roles)
        for ti in range(TURNS_PER_SIDE_PER_ROUND):
            add_segment(a_turns[ti],roles['side_a_label'],roles['side_a_label'],"left","#00FFCC")
            add_segment(s_turns[ti],roles['side_b_label'],roles['side_b_label'],"right","#FF00FF")
        a_full="\n".join(a_turns); s_full="\n".join(s_turns)
        print(f"   Round total: A={count_words(a_full)} words | B={count_words(s_full)} words")
        res=evaluate_round(judges,topic,rn,a_full,s_full,roles)
        ra,rb=calculate_round_average(res); cum_a+=ra; cum_b+=rb
        print(f"Round {rn}: {ra:.1f} vs {rb:.1f} | Cum: {cum_a:.1f} vs {cum_b:.1f}")
        sb=f"scoreboard_r{rn}.png"
        generate_scoreboard(rn,res,ra,rb,cum_a,cum_b,sb,roles)
        st=f"Round {rn} complete. Judges gave {roles['side_a_label']} {ra:.1f} and {roles['side_b_label']} {rb:.1f}. Cumulative {cum_a:.1f} to {cum_b:.1f}."
        sa=f"score_audio_r{rn}.mp3"; ss=f"score_subs_r{rn}.ass"; sv=f"score_video_r{rn}.mp4"
        sw=generate_audio(st,"Moderator",sa)
        try: generate_subtitles(sw,ss,scorecard=True,audio_file=sa,full_text=st)
        except: generate_subtitles(sw,ss,scorecard=True)
        render_scorecard_video(sb,sa,ss,sv); segs.append(sv)
        if res:
            # FIXED: Ensure commentary side matches actual judge winner - no more favouring A but describing B
            a_res=[r for r in res if r["winner"]=="A"]
            b_res=[r for r in res if r["winner"]=="B"]
            # If judges split, pick one from each; if all one side, only comment for that side
            if a_res and b_res:
                ja=random.choice(a_res)
                # Pick B judge different provider/model
                b_filtered=[r for r in b_res if r["model"]!=ja["model"] and r["provider"]!=ja["provider"]]
                jb=random.choice(b_filtered) if b_filtered else random.choice(b_res)
                # A commentary - judge actually favoured A
                ca=generate_panel_commentary(ja["model"],"A",topic,rn,a_full,s_full,pcom,roles); pcom.append(ca)
                ja_voice_idx = JUDGE_VOICE_MAP.get(ja["model"], 0)
                add_segment(ca,"AI Judge",f"AI JUDGE — {ja['display_name'].upper()} ({ja['provider'].upper()})","center","#3399FF",judge_voice_index=ja_voice_idx)
                # B commentary - judge actually favoured B
                cb=generate_panel_commentary(jb["model"],"B",topic,rn,a_full,s_full,pcom,roles); pcom.append(cb)
                jb_voice_idx = JUDGE_VOICE_MAP.get(jb["model"], 1)
                if jb_voice_idx==ja_voice_idx: jb_voice_idx=(ja_voice_idx+1)%len(JUDGE_VOICES)
                add_segment(cb,"AI Judge",f"AI JUDGE — {jb['display_name'].upper()} ({jb['provider'].upper()})","center","#3399FF",judge_voice_index=jb_voice_idx)
            elif a_res:
                # All judges favoured A - only generate A commentary, not fake B
                ja=random.choice(a_res)
                ca=generate_panel_commentary(ja["model"],"A",topic,rn,a_full,s_full,pcom,roles); pcom.append(ca)
                ja_voice_idx = JUDGE_VOICE_MAP.get(ja["model"], 0)
                add_segment(ca,"AI Judge",f"AI JUDGE — {ja['display_name'].upper()} ({ja['provider'].upper()})","center","#3399FF",judge_voice_index=ja_voice_idx)
                # Second A judge with different perspective
                remaining=[r for r in a_res if r["model"]!=ja["model"]]
                if remaining:
                    ja2=random.choice(remaining)
                    ca2=generate_panel_commentary(ja2["model"],"A",topic,rn,a_full,s_full,pcom,roles); pcom.append(ca2)
                    ja2_voice_idx = JUDGE_VOICE_MAP.get(ja2["model"], 1)
                    if ja2_voice_idx==ja_voice_idx: ja2_voice_idx=(ja_voice_idx+1)%len(JUDGE_VOICES)
                    add_segment(ca2,"AI Judge",f"AI JUDGE — {ja2['display_name'].upper()} ({ja2['provider'].upper()})","center","#3399FF",judge_voice_index=ja2_voice_idx)
            elif b_res:
                # All favoured B
                jb=random.choice(b_res)
                cb=generate_panel_commentary(jb["model"],"B",topic,rn,a_full,s_full,pcom,roles); pcom.append(cb)
                jb_voice_idx = JUDGE_VOICE_MAP.get(jb["model"], 0)
                add_segment(cb,"AI Judge",f"AI JUDGE — {jb['display_name'].upper()} ({jb['provider'].upper()})","center","#3399FF",judge_voice_index=jb_voice_idx)
                remaining=[r for r in b_res if r["model"]!=jb["model"]]
                if remaining:
                    jb2=random.choice(remaining)
                    cb2=generate_panel_commentary(jb2["model"],"B",topic,rn,a_full,s_full,pcom,roles); pcom.append(cb2)
                    jb2_voice_idx = JUDGE_VOICE_MAP.get(jb2["model"], 1)
                    if jb2_voice_idx==jb_voice_idx: jb2_voice_idx=(jb_voice_idx+1)%len(JUDGE_VOICES)
                    add_segment(cb2,"AI Judge",f"AI JUDGE — {jb2['display_name'].upper()} ({jb2['provider'].upper()})","center","#3399FF",judge_voice_index=jb2_voice_idx)
    add_segment(build_outro(len(judges),cum_a,cum_b,roles),"Moderator","MODERATOR")
    stitch_segments(segs,OUTPUT_FILE)
    print(f"\nCOMPLETE: {OUTPUT_FILE} — {cum_a:.1f} vs {cum_b:.1f}")
    cleanup_cache()

if __name__=="__main__":
    try: run_debate_pipeline()
    except KeyboardInterrupt: print("Cancelled")
    except Exception as e: print("FAILED"); print(str(e)); raise
