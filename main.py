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

MAX_VISUALS_PER_SEGMENT = 8
MIN_VISUAL_GAP = 0.35
VISUAL_W = 520
VISUAL_H = 520
VISUAL_Y = 160

# UNIQUE VOICES - each person and each judge different, no overlaps
VOICES = {
    "A": "en-US-BrianMultilingualNeural",
    "B": "en-US-AvaMultilingualNeural",
    "Moderator": "en-US-AndrewMultilingualNeural",
}
JUDGE_VOICES = [
    "en-US-EmmaMultilingualNeural",
    "en-US-JennyNeural",
    "en-US-GuyNeural",
    "en-US-AriaNeural",
    "en-US-ChristopherNeural",
    "en-US-JaneNeural",
    "en-US-JasonNeural",
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

def generate_turn(side, topic, round_num, turn_num, previous_exchange, model, role_label, role_desc, opponent_label, opponent_desc):
    prev_snip=(previous_exchange or "")[-1000:]
    used_list = list(USED_ARGUMENTS)[-15:]
    used_str = "; ".join(used_list) if used_list else "None yet"
    if round_num==1:
        round_focus=f"Opening round {turn_num}/4. Establish NEW foundation with evidence NOT used before. Avoid these already used: {used_str}"
    elif round_num==2:
        round_focus=f"Rebuttal round {turn_num}/4. Directly rebut opponent's last point with FRESH evidence and NEW angle. Do NOT repeat: {used_str}"
    else:
        round_focus=f"Closing round {turn_num}/4. Summarize with NEW synthesis, strongest FRESH points. Avoid repeating: {used_str}"
    prompt=f"You are {role_label} debating live on YouTube about: {topic}. Your position: {role_desc}. Opponent is {opponent_label} who argues: {opponent_desc}. {round_focus} Previous opponent said: {prev_snip}. Write {WORDS_PER_TURN} words as REAL HUMAN would speak - natural, conversational, passionate. Use contractions (I'm, don't, can't, it's). Vary sentence length. Each sentence full with subject+verb. No dashes, no bullet points. Quote specific evidence. Rebut directly. Start immediately with point, no greeting. CRITICAL: Bring completely fresh arguments not used before. Sound like confident human debater. {MIN_TURN_WORDS}-{MAX_TURN_WORDS} words."
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
    # GENERALIZED - works for any topic.txt, not just Genesis
    tl=text.lower()
    visuals=[]
    genesis_kws=[
        ("heaven","Heaven","heaven with sun and clouds, detailed"),
        ("earth","Earth","earth with mountains and land, detailed"),
        ("day","Day light","bright day with sun, detailed"),
        ("night","Night sky","night with moon and stars, detailed"),
        ("light","Light rays","sun with bright rays, detailed"),
        ("darkness","Darkness","dark sky with stars, detailed"),
        ("evening","Evening","evening sky with orange sunset, detailed"),
        ("morning","Morning","morning sunrise, detailed"),
        ("water","Water waves","blue water waves, detailed"),
        ("sky","Sky clouds","sky with clouds, detailed"),
        ("land","Land mountains","land with mountains, detailed"),
        ("sun","Sun bright","bright sun with rays, detailed"),
        ("moon","Moon night","moon in night sky, detailed"),
        ("stars","Stars constellation","stars in night sky, detailed"),
        ("sea","Sea waves","sea with waves, detailed"),
        ("tree","Tree in garden","tree with leaves and apples, detailed"),
        ("fruit","Eating fruit","person eating fruit, detailed"),
        ("apple","Apple on branch","red apple hanging from branch, detailed"),
        ("garden","Garden of Eden","garden with trees and flowers, detailed"),
        ("serpent","Serpent on branch","snake on branch with tongue, detailed"),
        ("snake","Snake","snake slithering, detailed"),
        ("god","God light","sun with bright rays, detailed"),
        ("lord","Lord light","bright light from above, detailed"),
        ("adam","Adam figure","man figure detailed"),
        ("eve","Eve figure","woman figure detailed"),
        ("man","Man figure","man figure detailed"),
        ("woman","Woman figure","woman figure detailed"),
        ("eyes opened","Eyes opened","eyes opening wide, detailed"),
        ("naked","Naked shame","figures covering with leaves, detailed"),
        ("hide","Hiding","figures hiding behind tree, detailed"),
        ("dust","Dust ground","dust and ground, detailed"),
        ("cherubim","Cherubim angel","angel with sword, detailed"),
        ("sword","Sword flaming","flaming sword, detailed"),
        ("die","Dying","figure lying dying, detailed"),
        ("death","Death","death and dust, detailed"),
    ]
    ai_kws=[
        ("ai","AI brain","robot brain with circuits, detailed"),
        ("artificial","Artificial intelligence","robot head detailed"),
        ("robot","Robot","robot head detailed"),
        ("regulation","Scales of justice","balanced scales detailed"),
        ("regulate","Regulation","scales of justice detailed"),
        ("bias","Bias warning","warning sign detailed"),
        ("algorithm","Algorithm","flowing data blocks detailed"),
        ("data","Data","database with data points detailed"),
        ("computer","Computer","computer with screen detailed"),
        ("technology","Technology","tech with circuits detailed"),
        ("intelligence","Intelligence brain","brain with circuits detailed"),
    ]
    cosmos_kws=[
        ("universe","Universe","galaxy with stars detailed"),
        ("creator","Creator light","bright sun with rays detailed"),
        ("cosmos","Cosmos","galaxy spiral detailed"),
        ("big bang","Big Bang","explosion with stars detailed"),
        ("galaxy","Galaxy","spiral galaxy detailed"),
        ("planet","Planet","planet with rings detailed"),
        ("atom","Atom","atom with electrons detailed"),
        ("evolution","Evolution","DNA helix detailed"),
        ("dna","DNA","dna helix detailed"),
        ("creation","Creation","creation light detailed"),
        ("exist","Existence","question mark with stars detailed"),
    ]
    generic_kws=[
        ("evidence","Evidence","open book with light detailed"),
        ("logic","Logic","lightbulb with gears detailed"),
        ("truth","Truth","lightbulb glowing detailed"),
        ("choice","Choice","fork in road with two paths detailed"),
        ("free will","Free will","brain with choice detailed"),
        ("determin","Determinism","chain links detailed"),
        ("moral","Morality","scales balancing heart and brain detailed"),
        ("ethic","Ethics","scales of justice detailed"),
        ("justice","Justice","balanced scales detailed"),
        ("argument","Debate","two podiums facing detailed"),
        ("debate","Debate stage","debate stage with podiums detailed"),
        ("question","Question","question mark with light detailed"),
        ("life","Life","heart with pulse detailed"),
        ("knowledge","Knowledge","book with light detailed"),
        ("wisdom","Wisdom","owl with book detailed"),
    ]
    all_kws = genesis_kws + ai_kws + cosmos_kws + generic_kws
    for kw,label,desc in all_kws:
        start=0
        while len(visuals)<MAX_VISUALS_PER_SEGMENT:
            idx=tl.find(kw, start)
            if idx==-1: break
            phrase=text[max(0,idx-15):idx+len(kw)+25].strip() or kw
            if not any(v["phrase"]==phrase for v in visuals):
                visuals.append({"phrase":phrase,"label":label,"description":desc,"kind":"concept"})
            start=idx+len(kw)
            if len(visuals)>=MAX_VISUALS_PER_SEGMENT: break
        if len(visuals)>=MAX_VISUALS_PER_SEGMENT: break
    if len(visuals)<3:
        if any(w in tl for w in ["ai","artificial","robot","regulation","algorithm","tech"]):
            visuals.extend([
                {"phrase":text[:30],"label":"AI brain","description":"robot brain with circuits, detailed","kind":"concept"},
                {"phrase":text[:30],"label":"Scales of justice","description":"balanced scales detailed","kind":"concept"},
            ])
        elif any(w in tl for w in ["heaven","earth","god","creator","universe"]):
            visuals.extend([
                {"phrase":text[:30],"label":"Heaven","description":"heaven with clouds and sun detailed","kind":"concept"},
                {"phrase":text[:30],"label":"Earth","description":"earth with land and water detailed","kind":"concept"},
                {"phrase":text[:30],"label":"Tree in garden","description":"tree with apples detailed","kind":"concept"},
            ])
        else:
            visuals.extend([
                {"phrase":text[:30],"label":"Debate stage","description":"two podiums with figures detailed","kind":"concept"},
                {"phrase":text[:30],"label":"Lightbulb idea","description":"lightbulb glowing detailed","kind":"concept"},
            ])
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

def create_visual_plan(text, words, model_for_visuals):
    try:
        prompt=f"Extract up to {MAX_VISUALS_PER_SEGMENT} visual concepts from: {text[:600]} Return JSON list [{{phrase,label,description}}] phrases must be exact substrings, labels short like Apple, Serpent, Heaven, Earth, AI brain, Scales, Universe, etc."
        resp=query_openrouter(prompt, model_for_visuals, timeout=20, max_tokens=400, temperature=0.5)
        if resp:
            m=re.search(r"\[.*\]", resp, re.DOTALL)
            if m:
                data=json.loads(m.group(0))
                visuals=[]
                for it in data[:MAX_VISUALS_PER_SEGMENT]:
                    ph=str(it.get("phrase",""))[:80]
                    if ph and ph.lower() in text.lower():
                        visuals.append({"phrase":ph,"label":str(it.get("label","Concept"))[:30],"description":str(it.get("description",""))[:80],"kind":"concept"})
                if len(visuals)>=2: return visuals
    except: pass
    return fallback_visual_plan(text)

# === SCRIBBLE ART - FORMED NARRATIVE, STORY-DRIVEN, DRAWING+FADE ===

def draw_formed_shape(draw, bbox, shape_type="ellipse", density=60, progress_factor=1.0):
    x0,y0,x1,y1=bbox
    visible_density = int(density * min(1.0, progress_factor*1.3))
    cx=(x0+x1)/2; cy=(y0+y1)/2; w=x1-x0; h=y1-y0
    if shape_type=="ellipse":
        for i in range(16):
            ang=i/16*2*math.pi
            px=cx + (w/2)*math.cos(ang); py=cy + (h/2)*math.sin(ang)
            px2=cx + (w/2)*math.cos(ang+2*math.pi/16); py2=cy + (h/2)*math.sin(ang+2*math.pi/16)
            if i/16 < progress_factor*1.2:
                draw.line([px,py,px2,py2], fill=(0,0,0,210), width=2)
    elif shape_type=="rect":
        if progress_factor>0.2:
            draw.rectangle([x0,y0,x1,y1], outline=(0,0,0,210), width=2)
    for _ in range(visible_density):
        rx=random.uniform(x0+3, x1-3); ry=random.uniform(y0+3, y1-3)
        if shape_type=="ellipse":
            if ((rx-cx)/(w/2))**2 + ((ry-cy)/(h/2))**2 > 1: continue
        ang=random.uniform(-0.3,0.3) + (0 if random.random()>0.5 else math.pi/2)
        length=random.uniform(6, max(10, w*0.18))
        x2=rx+length*math.cos(ang); y2=ry+length*math.sin(ang)*0.6
        x2=max(x0, min(x1, x2)); y2=max(y0, min(y1, y2))
        draw.line([rx,ry,x2,y2], fill=(0,0,0,random.randint(100,220)), width=1)

def draw_formed_human(draw, x, y, size, action="standing", arm_progress=0, eating=False, progress=1.0):
    if progress<0.15: return
    head_x=x+size*0.5; head_y=y+size*0.20; head_r=size*0.18
    if progress>0.15:
        draw_formed_shape(draw, [head_x-head_r, head_y-head_r, head_x+head_r, head_y+head_r], "ellipse", density=int(size*0.6), progress_factor=(progress-0.15)*2)
        if progress>0.4:
            eye_y=head_y+size*0.02
            draw.ellipse([head_x-size*0.08, eye_y-size*0.03, head_x-size*0.03, eye_y+0.02], fill=(0,0,0,255))
            draw.ellipse([head_x+size*0.03, eye_y-size*0.03, head_x+size*0.08, eye_y+0.02], fill=(0,0,0,255))
    body_top=y+size*0.42; body_bottom=body_top+size*0.55; body_left=x+size*0.20; body_right=x+size*0.80
    if progress>0.25:
        draw_formed_shape(draw, [body_left, body_top, body_right, body_bottom], "rect", density=int(size*0.8), progress_factor=(progress-0.25)*1.5)
    if progress>0.35:
        if action=="reaching" or eating:
            ax1=body_right-size*0.1; ay1=body_top+size*0.12
            target_x=ax1+size*0.50; target_y=ay1-size*0.15
            cur_x=ax1 + (target_x-ax1)*arm_progress; cur_y=ay1 + (target_y-ay1)*arm_progress
            for _ in range(10):
                draw.line([ax1+random.uniform(-1,1), ay1+random.uniform(-1,1), cur_x+random.uniform(-1,1), cur_y+random.uniform(-1,1)], fill=(0,0,0,200), width=2)
            if progress>0.6:
                draw_formed_shape(draw, [cur_x-6, cur_y-6, cur_x+6, cur_y+6], "ellipse", density=8, progress_factor=1.0)
        else:
            for side in [-1,1]:
                ax=head_x+side*size*0.30; ay=body_top+size*0.10
                ax2=ax+side*size*0.15; ay2=ay+size*0.25
                if progress>0.4: draw.line([ax,ay,ax2,ay2], fill=(0,0,0,180), width=2)
    if progress>0.30:
        leg_top=body_bottom-size*0.03
        for leg_x in [x+size*0.32, x+size*0.60]:
            draw.line([leg_x, leg_top, leg_x+random.uniform(-2,2), leg_top+size*0.28], fill=(0,0,0,180), width=2)

def draw_formed_tree(draw, x, y, size, apple_positions=[], progress=1.0):
    trunk_w=size*0.14; tx=x+size*0.5-trunk_w/2
    if progress>0.1:
        draw_formed_shape(draw, [tx, y+size*0.38, tx+trunk_w, y+size], "rect", density=int(size*0.35), progress_factor=(progress-0.1)*1.5)
    canopy_r=size*0.36; cx=x+size*0.5; cy=y+size*0.26
    if progress>0.25:
        draw_formed_shape(draw, [cx-canopy_r, cy-canopy_r, cx+canopy_r, cy+canopy_r], "ellipse", density=int(size*1.2), progress_factor=(progress-0.25)*1.3)
    if progress>0.5:
        for (ax, ay) in apple_positions:
            draw_formed_shape(draw, [ax-11, ay-9, ax+11, ay+11], "ellipse", density=18, progress_factor=1.0)

def create_visual_asset(visual,index):
    # FIXED: Transparent APNG (not white GIF), larger formed drawings, sensible story actions
    filename=f"visual_{index}.png"  # APNG with true alpha, not GIF with white bg
    label=(visual.get('label','')+" "+visual.get('description','')).lower()
    frames=[]
    for f in range(36):
        progress=f/36.0
        draw_progress = min(1.0, progress*1.4)
        fade_alpha = 1.0
        if progress<0.12: fade_alpha = progress/0.12
        elif progress>0.85: fade_alpha = (1.0-progress)/0.15
        frame=Image.new("RGBA",(VISUAL_W,VISUAL_H),(0,0,0,0))  # Fully transparent bg
        draw=ImageDraw.Draw(frame)
        action_progress = min(1.0, max(0, (progress-0.35)/0.45))
        # Larger, more formed, less tiny - fill canvas
        if "apple" in label or "fruit" in label or "eat" in label:
            # STORY: Adam picking fruit - large, clear, sensible
            apple_pos=[]
            if draw_progress>0.25:
                apple_pos=[(VISUAL_W//2-35, 110), (VISUAL_W//2+30, 125)]
                draw_formed_tree(draw, VISUAL_W//2-95, 10, size=190, apple_positions=apple_pos if draw_progress>0.45 else [], progress=draw_progress)
            if draw_progress>0.20:
                adam_x = 15 + 12*action_progress
                # Larger figure - size 130 not 90, fills more
                draw_formed_human(draw, adam_x, VISUAL_H-210, 130, action="reaching", arm_progress=action_progress, eating=("eat" in label and action_progress>0.7), progress=draw_progress)
                if action_progress>0.35 and apple_pos:
                    start_x, start_y = apple_pos[0]
                    hand_x = adam_x+65+55*action_progress
                    hand_y = VISUAL_H-210+54+8+55*action_progress*0.3
                    if action_progress<0.65:
                        t = (action_progress-0.35)/0.30
                        cur_ax = start_x + (hand_x-start_x)*t
                        cur_ay = start_y + (hand_y-start_y)*t
                    else:
                        if "eat" in label:
                            t = (action_progress-0.65)/0.35
                            mouth_x = adam_x+65
                            mouth_y = VISUAL_H-210+26+10
                            cur_ax = hand_x + (mouth_x-hand_x)*t
                            cur_ay = hand_y + (mouth_y-hand_y)*t
                        else:
                            cur_ax=hand_x; cur_ay=hand_y
                    if draw_progress>0.45:
                        draw_formed_shape(draw, [cur_ax-14, cur_ay-11, cur_ax+14, cur_ay+13], "ellipse", density=22, progress_factor=1.0)
            if draw_progress>0.35:
                draw_formed_human(draw, VISUAL_W-150, VISUAL_H-200, 120, action="standing", progress=draw_progress*0.9)
            # Ground - full width
            if draw_progress>0.15:
                for gx in range(0, VISUAL_W, 16):
                    if gx/VISUAL_W < draw_progress*1.2:
                        draw.line([gx, VISUAL_H-14, gx+14, VISUAL_H-14], fill=(0,0,0,180), width=2)
        elif "serpent" in label or "snake" in label:
            # STORY: Serpent on branch talking to Adam/Eve - clear narrative
            if draw_progress>0.15:
                draw_formed_tree(draw, VISUAL_W//2-85, 5, size=175, apple_positions=[], progress=draw_progress)
            if draw_progress>0.25:
                branch_y=78
                draw.line([VISUAL_W*0.22, branch_y, VISUAL_W*0.88, branch_y+10], fill=(0,0,0,220), width=3)
                serpent_x=VISUAL_W*0.28
                for i in range(0, 70, 10):
                    sx=serpent_x+i
                    sy=branch_y+4*math.sin(i*0.18+action_progress*2.5)
                    sx2=sx+12
                    sy2=branch_y+4*math.sin((i+12)*0.18+action_progress*2.5)
                    if i/70 < draw_progress:
                        draw.line([sx,sy,sx2,sy2], fill=(0,0,0,230), width=4)
                head_x=serpent_x+70+3*math.sin(action_progress*7)
                head_y=branch_y-3+1.5*math.cos(action_progress*7)
                if draw_progress>0.45:
                    draw_formed_shape(draw, [head_x-12, head_y-10, head_x+18, head_y+10], "ellipse", density=20, progress_factor=1.0)
                    draw.ellipse([head_x+6, head_y-2, head_x+10, head_y+2], fill=(0,0,0,255))
                    if f%12<4:
                        draw.line([head_x+18, head_y, head_x+28, head_y-4], fill=(0,0,0,200), width=2)
                        draw.line([head_x+18, head_y+2, head_x+28, head_y+6], fill=(0,0,0,200), width=2)
                    if action_progress>0.25:
                        for j in range(3):
                            sx=head_x+18; sy=head_y+j*3-3
                            ex=sx+18+6*math.sin(action_progress*5+j)
                            ey=sy+random.uniform(-2,2)
                            draw.line([sx,sy,ex,ey], fill=(0,0,0,120), width=2)
            if draw_progress>0.35:
                draw_formed_human(draw, 20, VISUAL_H-200, 115, action="standing", progress=draw_progress*0.9)
                draw_formed_human(draw, VISUAL_W-145, VISUAL_H-195, 112, action="standing", progress=draw_progress*0.9)
        elif "tree" in label or "garden" in label:
            if draw_progress>0.12:
                draw_formed_tree(draw, VISUAL_W//2-95, 15, size=195, apple_positions=[(VISUAL_W//2-32, 105), (VISUAL_W//2+28, 120)] if draw_progress>0.45 else [], progress=draw_progress)
            if draw_progress>0.35:
                draw_formed_human(draw, 25, VISUAL_H-200, 118, action="standing", progress=draw_progress*0.8)
                draw_formed_human(draw, VISUAL_W-145, VISUAL_H-195, 116, action="standing", progress=draw_progress*0.8)
            if draw_progress>0.55:
                for fx, fy in [(60, VISUAL_H-38), (VISUAL_W-70, VISUAL_H-42), (VISUAL_W//2, VISUAL_H-30)]:
                    draw_formed_shape(draw, [fx-7, fy-7, fx+7, fy+7], "ellipse", density=8, progress_factor=1.0)
        elif "heaven" in label or "sky" in label:
            if draw_progress>0.08:
                sun_r=38+6*action_progress
                draw_formed_shape(draw, [VISUAL_W*0.62-sun_r, 38-sun_r, VISUAL_W*0.62+sun_r, 38+sun_r], "ellipse", density=50, progress_factor=draw_progress)
                if draw_progress>0.45:
                    for ang in range(0,360,28):
                        if ang/360 < draw_progress:
                            rad=math.radians(ang); x2=VISUAL_W*0.62+100*math.cos(rad); y2=38+100*math.sin(rad)
                            draw.line([VISUAL_W*0.62,38,x2,y2], fill=(0,0,0,90), width=2)
            if draw_progress>0.28:
                for cx, cy in [(65,70),(175,48),(310,88)]:
                    if (cx/400) < draw_progress:
                        draw_formed_shape(draw, [cx, cy, cx+75, cy+32], "ellipse", density=28, progress_factor=draw_progress)
            # Birds as V
            if draw_progress>0.5:
                for i in range(3):
                    bx=40+i*50+action_progress*20; by=130+12*math.sin(action_progress*3+i)
                    draw.line([bx,by,bx+8,by+6], fill=(0,0,0,200), width=2)
                    draw.line([bx+8,by+6,bx+16,by], fill=(0,0,0,200), width=2)
        elif "earth" in label or "land" in label:
            if draw_progress>0.12:
                peaks=[(0,210),(100,75),(190,145),(305,58),(VISUAL_W,135)]
                for i in range(len(peaks)-1):
                    p1=peaks[i]; p2=peaks[i+1]
                    if i/len(peaks) < draw_progress:
                        draw.line([p1[0],p1[1],p2[0],p2[1]], fill=(0,0,0,220), width=3)
                        mid_x=(p1[0]+p2[0])/2; mid_y=(p1[1]+p2[1])/2+24
                        draw_formed_shape(draw, [mid_x-18, mid_y-12, mid_x+18, mid_y+12], "ellipse", density=12, progress_factor=draw_progress)
            if draw_progress>0.45:
                draw.line([0,200,VISUAL_W,200], fill=(0,0,0,180), width=3)
        elif "day" in label or "light" in label or "sun" in label or "god" in label or "creator" in label:
            sun_y = 150 - 90*action_progress
            if draw_progress>0.18:
                draw_formed_shape(draw, [VISUAL_W//2-34, sun_y-34, VISUAL_W//2+34, sun_y+34], "ellipse", density=42, progress_factor=draw_progress)
                if draw_progress>0.55:
                    for ang in range(-60,61,14):
                        rad=math.radians(ang); x2=VISUAL_W//2+125*math.sin(rad); y2=sun_y+125*math.cos(rad)
                        if abs(ang)/60 < draw_progress:
                            draw.line([VISUAL_W//2,sun_y,x2,y2], fill=(0,0,0,70), width=2)
        elif "night" in label or "moon" in label or "stars" in label:
            if draw_progress>0.18:
                draw_formed_shape(draw, [VISUAL_W*0.62-32, 42-32, VISUAL_W*0.62+32, 42+32], "ellipse", density=38, progress_factor=draw_progress)
            if draw_progress>0.45:
                star_count=int(24*draw_progress)
                for i in range(star_count):
                    sx=(i*57)%(VISUAL_W-20)+10; sy=(i*41)%110+18
                    draw.ellipse([sx,sy,sx+3,sy+3], fill=(0,0,0,220))
        elif "water" in label or "sea" in label:
            for y in range(80, VISUAL_H, 28):
                for _ in range(2):
                    x0=random.randint(0,VISUAL_W-50); x1=x0+random.randint(25,60)
                    if x0/VISUAL_W < draw_progress:
                        draw.line([x0, y, x1, y], fill=(0,0,0,random.randint(90,190)), width=2)
        elif "die" in label or "death" in label or "dust" in label:
            if draw_progress>0.18:
                hx=VISUAL_W//2-36; hy=VISUAL_H//2+12
                draw_formed_shape(draw, [hx-20, hy-20, hx+20, hy+20], "ellipse", density=26, progress_factor=draw_progress)
                draw.line([hx+20, hy, hx+75, hy+7], fill=(0,0,0,200), width=3)
                draw.line([hx+20, hy+4, hx+70, hy+18], fill=(0,0,0,200), width=3)
            if draw_progress>0.55:
                for i in range(int(10*action_progress)):
                    dx=VISUAL_W//2+random.uniform(-24,48); dy=VISUAL_H//2+22+random.uniform(0,22)-action_progress*26
                    draw.ellipse([dx,dy,dx+3,dy+3], fill=(0,0,0,140))
        elif "eyes" in label:
            if draw_progress>0.25:
                for ex in [VISUAL_W//2-65, VISUAL_W//2+35]:
                    ey=VISUAL_H//2-12
                    draw_formed_shape(draw, [ex-32, ey-18, ex+32, ey+14], "ellipse", density=30, progress_factor=draw_progress)
                    if draw_progress>0.55:
                        draw_formed_shape(draw, [ex-12, ey-8, ex+12, ey+6], "ellipse", density=16, progress_factor=1.0)
        elif "ai" in label or "robot" in label or "computer" in label or "intelligence" in label:
            if draw_progress>0.18:
                cx=VISUAL_W//2; cy=VISUAL_H//2-18
                draw_formed_shape(draw, [cx-72, cy-58, cx+72, cy+42], "rect", density=68, progress_factor=draw_progress)
            if draw_progress>0.45:
                for ex in [VISUAL_W//2-28, VISUAL_W//2+28]:
                    draw_formed_shape(draw, [ex-14, VISUAL_H//2-32, ex+14, VISUAL_H//2-14], "ellipse", density=18, progress_factor=1.0)
                for i in range(5):
                    y=VISUAL_H//2-6+i*11
                    if i/5 < draw_progress:
                        draw.line([VISUAL_W//2-60, y, VISUAL_W//2+60, y], fill=(0,0,0,120), width=2)
        elif "scales" in label or "justice" in label:
            if draw_progress>0.18:
                cx=VISUAL_W//2
                draw.line([cx,18,cx,68], fill=(0,0,0,210), width=3)
                tilt=7*math.sin(action_progress*2.2)
                draw.line([cx-92, 64+tilt, cx+92, 64-tilt], fill=(0,0,0,210), width=3)
                if draw_progress>0.45:
                    for px, py in [(cx-84, 64+tilt), (cx+84, 64-tilt)]:
                        draw_formed_shape(draw, [px-28, py+38, px+28, py+60], "ellipse", density=24, progress_factor=draw_progress)
                        draw.line([px, 64+(tilt if px<cx else -tilt), px-20, py+38], fill=(0,0,0,170), width=2)
                        draw.line([px, 64+(tilt if px<cx else -tilt), px+20, py+38], fill=(0,0,0,170), width=2)
        elif "universe" in label or "galaxy" in label or "cosmos" in label:
            if draw_progress>0.12:
                for _ in range(int(22*draw_progress)):
                    sx=random.randint(0,VISUAL_W); sy=random.randint(0,VISUAL_H)
                    draw.ellipse([sx,sy,sx+3,sy+3], fill=(0,0,0,random.randint(70,220)))
            if draw_progress>0.35:
                cx=VISUAL_W//2; cy=VISUAL_H//2
                for i in range(0,200,10):
                    if i/200 < draw_progress:
                        ang=i*0.05+action_progress*1.3
                        r=i*0.48
                        x=cx+r*math.cos(ang); y=cy+r*math.sin(ang)*0.58
                        draw.ellipse([x,y,x+3,y+3], fill=(0,0,0,200))
                draw_formed_shape(draw, [cx-16, cy-16, cx+16, cy+16], "ellipse", density=26, progress_factor=draw_progress)
        elif "atom" in label or "dna" in label:
            if draw_progress>0.18:
                cx=VISUAL_W//2; cy=VISUAL_H//2
                draw_formed_shape(draw, [cx-18, cy-18, cx+18, cy+18], "ellipse", density=22, progress_factor=draw_progress)
                for orbit in range(2):
                    rx=44+orbit*14
                    for a in range(0,360,36):
                        if a/360 < draw_progress:
                            rad=math.radians(a+action_progress*70)
                            ox=cx+rx*math.cos(rad); oy=cy+rx*0.62*math.sin(rad)
                            draw.ellipse([ox,oy,ox+3,oy+3], fill=(0,0,0,170))
        else:
            # Generic debate - larger, clearer
            if draw_progress>0.22:
                draw_formed_human(draw, 30, VISUAL_H//2-70, 105, action="standing", progress=draw_progress*0.8)
                draw_formed_human(draw, VISUAL_W-155, VISUAL_H//2-70, 105, action="standing", progress=draw_progress*0.8)
            if draw_progress>0.55:
                draw_formed_shape(draw, [30, VISUAL_H//2+30, 135, VISUAL_H//2+85], "rect", density=26, progress_factor=draw_progress)
                draw_formed_shape(draw, [VISUAL_W-135, VISUAL_H//2+30, VISUAL_W-30, VISUAL_H//2+85], "rect", density=26, progress_factor=draw_progress)
            if draw_progress>0.68 and action_progress>0.45:
                bx=VISUAL_W//2-52; by=VISUAL_H//2-95
                draw_formed_shape(draw, [bx, by, bx+104, by+42], "ellipse", density=26, progress_factor=1.0)
        if fade_alpha<1.0:
            overlay=Image.new("RGBA",(VISUAL_W,VISUAL_H),(255,255,255,int((1.0-fade_alpha)*190)))
            frame=Image.alpha_composite(frame, overlay)
        frames.append(frame)
    # Save as APNG with true alpha - not GIF with white bg
    frames[0].save(filename,format='PNG',save_all=True,append_images=frames[1:],duration=85,loop=0,disposal=2)
    print(f"   Created FORMED transparent APNG: {visual.get('label')} (36 frames, story-driven, no white bg)")
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
    # FIXED v2: 1) Input indices correct (bg=0, ui=1, audio=2, visuals=3+), 2) No force_style commas causing "No such filter: ''"
    duration=get_audio_duration(audio_path)
    if not duration: duration=10.0
    cmd=["ffmpeg","-y","-loop","1","-i",bg_path,"-loop","1","-i",ui_path,"-i",audio_path]
    filter_parts=[]
    filter_parts.append(f"[0:v]scale={VIDEO_W}:{VIDEO_H}:flags=lanczos[bg]")
    filter_parts.append(f"[1:v]scale={VIDEO_W}:{VIDEO_H}:flags=lanczos[ui]")
    filter_parts.append(f"[bg][ui]overlay=0:0:shortest=1[bg_ui]")
    last_label="[bg_ui]"
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
    W=1000; H=620
    img=Image.new("RGBA",(W,H),(18,18,28,255))
    draw=ImageDraw.Draw(img)
    font_title=load_font(32,bold=True); font_head=load_font(20,bold=True); font_row=load_font(18)
    draw.rectangle([0,0,W,72], fill=(0,0,0,200))
    draw.text((W//2,18), f"ROUND {round_num} SCORES - {roles['side_a_label']} vs {roles['side_b_label']}", font=font_title, fill=(255,255,255,255), anchor="mt")
    draw.text((30,88), f"Judge (Company)", font=font_head, fill=(255,255,255,200))
    draw.text((300,88), f"{roles['side_a_label'][:20]}", font=font_head, fill=(0,255,204,255))
    draw.text((500,88), f"{roles['side_b_label'][:20]}", font=font_head, fill=(255,0,255,255))
    draw.text((700,88), "Winner", font=font_head, fill=(255,255,255,200))
    y=120
    for res in results:
        bg=(30,30,45,255) if y%40==0 else (22,22,35,255)
        draw.rectangle([0,y,W,y+32], fill=bg)
        draw.text((30,y+4), f"{res['display_name']} ({res['provider']})", font=font_row, fill=(255,255,255,230))
        draw.text((300,y+4), f"{res['A_total']:.1f}", font=font_row, fill=(0,255,204,255))
        draw.text((500,y+4), f"{res['B_total']:.1f}", font=font_row, fill=(255,0,255,255))
        win_label=roles['side_a_label'] if res['winner']=="A" else roles['side_b_label']
        draw.text((700,y+4), win_label[:12], font=font_row, fill=(255,215,0,255))
        y+=34
    draw.rectangle([0,y,W,y+2], fill=(255,255,255,100))
    y+=10
    draw.text((30,y), f"Avg Round {round_num}: {avg_a:.1f} vs {avg_b:.1f} | Cumulative: {cum_a:.1f} vs {cum_b:.1f}", font=font_head, fill=(255,255,255,255))
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

def build_judge_intro(judge_model, jc):
    name=get_judge_short_name(judge_model); comp=get_company_name(judge_model)
    intros=[
        f"Hello, I am {name} from {comp}. I am one of the {jc} judges on today's panel. I will be scoring on argument, rebuttal, and clarity. Looking forward to a great debate.",
        f"Hi everyone, {name} here, from {comp}. Excited to be one of your {jc} judges today. I will be looking for specific evidence and clear reasoning. Let us begin.",
        f"Greetings, I am {name}, representing {comp}. I am honored to be among the {jc} judges. I will evaluate based on strength of argument and how well each side answers the other.",
    ]
    return random.choice(intros)

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
