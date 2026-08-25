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
WORDS_PER_SIDE_PER_ROUND = 500
TURNS_PER_SIDE_PER_ROUND = 4
WORDS_PER_TURN = 125
MIN_TURN_WORDS = 105
MAX_TURN_WORDS = 145

MAX_JUDGES = 7
JUDGE_WORKERS = 7

MAX_VISUALS_PER_SEGMENT = 0
MIN_VISUAL_GAP = 2.2
MAX_EMOJIS_PER_SEGMENT = 1
EMOJI_W = 110
EMOJI_H = 110
USED_EMOJIS = set()
USED_ARGUMENTS = set()
USED_PHRASES = set()
USED_KEYWORDS = set()
USED_JUDGE_EXPLANATIONS = set()

VOICES = {
    "A": "en-US-BrianMultilingualNeural",
    "B": "en-GB-SoniaNeural",
    "Moderator": "en-AU-NatashaNeural",
}
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

def get_company_name(model_id): return provider_from_model(model_id)

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
    tl=(topic or "").lower()
    if "god" in tl and "serpent" in tl:
        return {"side_a_label": "GOD TOLD TRUTH","side_a_desc": "Defends God told truth in Genesis","side_b_label": "SERPENT TOLD TRUTH","side_b_desc": "Defends serpent told truth",}
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
    return {"side_a_label": "AFFIRMATIVE","side_a_desc": f"Argues FOR {topic}","side_b_label": "NEGATIVE","side_b_desc": f"Argues AGAINST {topic}",}

def strip_filler(text):
    for pat in [r"^(ladies and gentlemen[,.]?\s*)",r"^(my friends[,.]?\s*)",r"^(well[,.]?\s*)",r"^(thank you[,.]?\s*)"]:
        text=re.sub(pat,"",text,flags=re.IGNORECASE).strip()
    return text

def generate_fallback_debate(side_label, topic, round_num, turn_num):
    topic_short = topic[:130] if len(topic)>130 else topic
    if "GOD TOLD TRUTH" in side_label.upper():
        god_templates = {
            (1,1): "I want to start with what God actually said in Genesis 2 verse 17. He said in the day you eat of it you shall surely die, and the Hebrew is moth tamuth, an emphatic form meaning dying you shall die. It is about certainty, not just timing. The serpent directly contradicts that in chapter 3 verse 4 when he says you shall not surely die. So who is telling the truth? Look at what happened that very day. They experienced shame, fear, hiding, and separation from God. That is the beginning of death.",
            (1,2): "Look at how generous God is in Genesis 2 verse 16. He says you may freely eat of every tree in the garden. Every single tree except one. That is incredibly generous. Then the serpent twists it in chapter 3 verse 1 and asks, did God really say you shall not eat of every tree? He makes God sound stingy when God was actually abundant. That kind of twisting is deceptive. God told the truth about provision.",
            (1,3): "Genesis 2 verse 17 is a clear warning, and Genesis 3 verses 7 through 10 shows that warning coming true relationally that same day. Their eyes were opened but what came with it? Shame, fear, and hiding from God's presence. Verse 10 says I was afraid because I was naked and I hid myself. Fear and hiding are not fullness of life. That is a form of death starting immediately.",
            (1,4): "The phrase in the day you eat appears in Genesis 2 verse 4 as well, in the day the Lord God made earth and heavens, meaning when, not a 24 hour timer. It is about when you eat, death becomes certain. And Genesis 3 verse 19 says to dust you shall return, and verses 23 to 24 say they were driven out and cherubim blocked the way to the tree of life. On that day they lost access to everlasting life.",
            (2,1): "My opponent says they did not drop dead that day, so God lied, but that misses the biblical meaning of death. Genesis 3 verse 10 says Adam was afraid and hid. That is relational break. Verse 19 says to dust you shall return. Mortality entered. Verses 23 and 24 say they were driven out of Eden and cherubim guarded the tree of life. So on the day they ate, they lost immortality. The process of dying started that exact day.",
            (2,2): "The argument that they did not die that day ignores how in the day is used elsewhere. Chapter 2 verse 4 says in the day the Lord made the heavens and earth. It means when. It is about certainty, not a countdown. When you eat, death becomes certain. And the serpent told a half truth. He said your eyes would be opened, and they were, but he left out the painful consequences. A half truth that hides cost is still misleading.",
            (2,3): "If the serpent told the whole truth, where is the warning about pain, toil, and exile? Genesis 3 verses 16 to 19 lists real curses, pain in childbirth, thorns and thistles, sweat and hard work, and finally dust. The serpent said nothing about that. He only said you shall be as gods. Chapter 3 verse 22 says they did become like God knowing good and evil, but at what terrible cost? God told the cost upfront.",
            (2,4): "Think about the tree of life in Genesis 3 verses 22 to 24. God says, lest he put forth his hand and take also of the tree of life and live forever, therefore He drove the man out and placed cherubim to keep the way. On that day they lost the chance to live forever. That is death beginning. The serpent said you shall not surely die, but they lost everlasting life that day.",
            (3,1): "Let us pull it together. God warned that in the day you eat you shall surely die. The serpent said you shall not surely die, you shall be as gods. What actually happened? There was enlightenment, but also shame, blame, fear, toil, pain, and being cut off from the tree of life. That is death in the biblical sense, separation and mortality beginning. The serpent promised no death at all, but death is now the human condition.",
            (3,2): "Who told the more complete truth? God said death would come when they ate, and He was generous with every tree but one. The serpent said no death, just godlikeness, and made God sound restrictive. The story shows both knowledge and death entering at once, eyes opened but also shame, cursing, and exile. If the serpent told the whole truth, where is his warning about losing Eden and returning to dust? He omitted the cost. God stated it upfront.",
            (3,3): "Consider the character of the speakers. God creates, provides abundantly, and warns clearly to protect. The serpent questions in 3 verse 1, did God really say, that plants doubt, then denies in verse 4, you shall not surely die, and then appeals to desire in verse 5, you shall be as gods. That pattern is classic temptation, doubt, denial, desire. God told the truth to protect, serpent told half truth to entice.",
            (3,4): "One last point about the Hebrew. Moth tamuth in Genesis 2 verse 17 is infinitive absolute, it emphasizes certainty, you shall surely die. The serpent says lo moth temuthun, you shall not surely die, directly negating God's emphasis. What happened? They did die, not as an instant drop, but relationally that day, and they began dying physically, and eventually returned to dust. Adam lived 930 years but he did die. God's surely came true.",
        }
        key = (round_num, turn_num if turn_num<=4 else ((turn_num-1)%4+1))
        return god_templates.get(key, god_templates[(3,4)])
    elif "SERPENT TOLD TRUTH" in side_label.upper():
        serpent_templates = {
            (1,1): "I want us to read what the text actually says, not what we think it should say. Genesis 2 verse 17 has God saying, in the day you eat you shall surely die, and the plain sense of in the day is that same day. Yet Genesis 5 verse 5 says Adam lived nine hundred and thirty years and then died. He did not die that day. He lived for centuries afterward. The serpent says in chapter 3 verse 4, you shall not surely die, and that is exactly what happened. They did not die that day. He also says your eyes shall be opened and you shall be as gods knowing good and evil, and chapter 3 verse 7 says their eyes were opened, and God Himself says in verse 22, man has become as one of us to know good and evil. God confirms the serpent was right.",
            (1,2): "Think about the Hebrew word yom, day. In Genesis 1, evening and morning were the first day, a literal day. So when God says in the day you eat you shall surely die, the natural reading is that same day. Adam did not die that day. The serpent's prediction about the immediate outcome was more accurate. He said you shall not die, and they did not. He said you shall be as gods knowing good and evil, and God says in chapter 3 verse 22, they have become like one of us. Two claims by the serpent, both validated by the story itself, while God's threat did not happen as stated that day.",
            (1,3): "Genesis 2 verse 17 threatens death in the day, but Genesis 3 verse 6 says the woman saw the tree was good for food and pleasant to the eyes and desired to make one wise. She ate and gave to her husband and he ate. Verse 7 says the eyes of both were opened and they knew they were naked. That is exactly what the serpent promised in verse 5, your eyes shall be opened. Death that day? The text never says anyone died that day. Verse 8 says they heard the sound of the Lord walking in the garden. They were alive, hiding, not dead.",
            (1,4): "If God meant spiritual death, why did He not say spiritual death? The text of Genesis 2 and 3 never mentions spiritual death. That is later theology read back into the story. The text mentions nakedness, shame, cursing of the ground, pain, hard work, and dust to dust. The test is simple. Did they die that day as God said? No. Did their eyes open as the serpent said? Yes, chapter 3 verse 7 says their eyes were opened. On a straightforward reading, the serpent described what would happen that day more accurately.",
            (2,1): "My opponent talks about spiritual death, but Genesis 2 and 3 never uses that phrase at all. That is an idea imported from later theology, not from this narrative. The text mentions nakedness, shame, cursing, pain in childbirth, hard work, and eventually returning to dust. The simple question is, did they die that day as God said they would? No, they did not. Did their eyes open as the serpent said they would? Yes, chapter 3 verse 7 says their eyes were opened. On a plain reading, the serpent was more accurate about that day.",
            (2,2): "If God meant they would begin dying, why did He say in the day you shall surely die? Why not say you shall become mortal? That would be clear. And if the serpent lied, why does God confirm his second claim? Chapter 3 verse 22 says, behold, the man has become as one of us to know good and evil. That is almost word for word what the serpent promised in verse 5. If the serpent is the father of lies, why is God echoing his promise? The story presents a real tension about who was more accurate.",
            (2,3): "Consider Genesis 3 verse 22. God says man has become as one of us to know good and evil. That is exactly what the serpent said would happen in verse 5. If the serpent is the father of lies, why is God confirming his prediction? And where is the death that day? Chapter 3 verse 20 says Adam called his wife Eve, the mother of all living. Chapter 4 verse 1 says Adam knew Eve and she conceived. They are very much alive, building a family, not dead. The serpent said you shall not die, and they did not that day.",
            (2,4): "Look at chapter 3 verse 13. God asks the woman, what is this that you have done? The woman says, the serpent beguiled me and I did eat. She does not say the serpent lied about death. She says he beguiled her. Beguiled means tricked, but tricked about what? If he lied about death and they did not die, she would have evidence he lied. But the text never says she realized he lied about death. Instead, she got exactly what he said, her eyes were opened, knowing good and evil. She got enlightenment, not death that day.",
            (3,1): "Let us weigh the evidence carefully. God said, in the day you eat you die. The serpent said, you will not die, you will be enlightened, your eyes will be opened. What does the story actually report happened? Their eyes were opened, yes. Enlightenment came, yes. Death that day, no. Adam lives nine hundred and thirty years. God even acknowledges the enlightenment part in chapter 3 verse 22. There is no acknowledgment that they died that day. If we let the text speak for itself, the serpent's description of the immediate outcome was more accurate.",
            (3,2): "The question is not who we want to be truthful, but what the text reports. It reports God threatening death in the day, the serpent promising no death but knowledge, and then it reports knowledge coming and death not coming that day. It reports God Himself saying they have become like us knowing good and evil. The serpent promised that exact thing. So two promises from the serpent, both happen in the story. One threat from God does not happen that day. On the immediate facts, the serpent was right.",
            (3,3): "Final assessment. Genesis presents two contradictory predictions. God says in the day you eat, dying you shall die. The serpent says you shall not dying die, but your eyes opened, as gods knowing good and evil. What happens? Verse 7, eyes opened. Verse 22, God says man has become as one of us knowing good and evil. The serpent's two predictions both occur. God's prediction of death in the day does not occur as stated, since Adam lives nine hundred and thirty years per Genesis 5 verse 5. On textual facts alone, the serpent was more accurate about that day.",
            (3,4): "If we are honest about the text, Genesis 3 is not about who lied, but about who gave a more accurate description of what would happen when they ate. God said death that day. The serpent said no death, but knowledge and godlikeness. Knowledge and godlikeness happen that day, confirmed by God in chapter 3 verse 22. Death that day does not happen. Adam and Eve go on to have children and build a life. The serpent's account matches the narrative outcome better.",
        }
        key = (round_num, turn_num if turn_num<=4 else ((turn_num-1)%4+1))
        return serpent_templates.get(key, serpent_templates[(3,4)])
    else:
        generic = {
            (1,1): f"When we look at {topic_short}, I think {side_label} has the stronger case because it fits what we actually observe. The other side relies on assumptions that sound nice in theory but do not hold up when you test them against real cases. That is why {side_label} should be preferred here.",
            (1,2): f"Let me put {topic_short} in simple terms. {side_label} is saying there is a clear mechanism that you can check. You can actually test it, and when you do, the evidence lines up. The opposing view struggles to explain the common cases we see every day. That points toward {side_label}.",
            (1,3): f"Think about {topic_short} from first principles. What are we trying to explain? {side_label} offers an explanation that covers both the big picture and the details. The alternative sounds plausible until you look at the edge cases, then it breaks down.",
            (1,4): f"On {topic_short}, I think we should ask what would happen if we adopted the other side. Would it make accurate predictions? {side_label} does. It tells you what to expect and you see it happen. That predictive power matters.",
        }
        key = (round_num, turn_num if turn_num<=4 else ((turn_num-1)%4+1))
        return generic.get(key, f"On {topic_short}, {side_label} offers a clearer and more consistent account that fits the evidence we have.")

USED_JUDGE_EXPLANATIONS = set()
def generate_panel_commentary(model,side,topic,rn,ap,sk,prev,roles):
    prov=get_judge_short_name(model); comp=get_company_name(model)
    pref_label = roles['side_a_label'] if side=="A" else roles['side_b_label']
    other_label = roles['side_b_label'] if side=="A" else roles['side_a_label']
    recent="\n".join(prev[-4:]); used_expl = "\n".join(list(USED_JUDGE_EXPLANATIONS)[-8:])
    def trim(t,mw=160): wl=t.split(); return t if len(wl)<=mw else " ".join(wl[-mw:])
    tl_topic = (topic or "").lower()
    is_genesis_topic = "god" in tl_topic and "serpent" in tl_topic
    if side=="A":
        prompt=f"You are {prov} from {comp}, judging round {rn} on {topic}. You scored {pref_label} HIGHER than {other_label} in this round. Explain why {pref_label} WON round {rn} specifically. Use different reasons than previous rounds. {pref_label}: {trim(ap)} vs {other_label}: {trim(sk)} RULES: MUST argue {pref_label} won round {rn}. Do NOT say {other_label} won. Give 2 specific reasons unique to round {rn}. Avoid repeating: {used_expl}. Natural full sentences, human-like, not robotic, vary from previous rounds."
    else:
        prompt=f"You are {prov} from {comp}, judging round {rn} on {topic}. You scored {pref_label} HIGHER than {other_label} in round {rn}. Explain why {pref_label} WON this round {rn}, not previous rounds. {pref_label}: {trim(ap)} vs {other_label}: {trim(sk)} MUST argue {pref_label} won round {rn}. Point out a specific weakness in {other_label} in this round that you haven't mentioned before. Avoid: {used_expl}. Natural, full sentences, different from other rounds."
    resp=query_openrouter(prompt,model,timeout=30,max_tokens=400,temperature=0.92)
    if resp and len(resp.split())>=12:
        low = resp.lower()
        if f"{other_label.lower()} won" in low or f"i scored {other_label.lower()} higher" in low:
            print(f"Judge {prov} mismatched, using fallback")
        else:
            resp=re.sub(r'As .*? to assess,','',resp,flags=re.IGNORECASE).strip()
            resp=re.sub(r'As an? .*? judge,','',resp,flags=re.IGNORECASE).strip()
            resp=re.sub(r'^I am .*? and I.*?[.]','',resp,flags=re.IGNORECASE).strip()
            lower_resp=resp.lower()[:80]
            is_rep=False
            for used in USED_JUDGE_EXPLANATIONS:
                if used.lower()[:50] in lower_resp and len(used)>20: is_rep=True; break
            if not is_rep and len(resp.split())>=10:
                USED_JUDGE_EXPLANATIONS.add(resp[:100]); return resp
    if is_genesis_topic:
        fallbacks_a=[
            f"In round {rn}, I scored {pref_label} higher because they focused on relational death that day. Genesis 3 verse 10 says Adam was afraid and hid. Fear and hiding is break in fellowship, which Bible calls death. {other_label} only talked about physical drop.",
            f"For round {rn}, {pref_label} won because they explained Hebrew beyom. Genesis 2 verse 4 uses in the day to mean when, not stopwatch. Stresses certainty. {other_label} read it as literal 24 hour deadline that doesn't fit Adam living 930 years.",
            f"Round {rn} went to {pref_label} because they highlighted what serpent left out. He promised as gods but said nothing about pain, thorns, sweat, exile, losing tree of life verses 22-24. Omitting cost is misleading.",
            f"In round {rn}, {pref_label} was stronger because they showed loss of immortality that day. Verse 22 says lest he take tree of life and live forever, verse 24 cherubim blocked way. On that day they lost everlasting life.",
            f"For me, round {rn} belonged to {pref_label} because they dealt with character. God gave every tree freely and warned clearly, serpent questioned did God really say, twisting generosity into stinginess.",
            f"In round {rn}, I leaned to {pref_label} because they used emphatic moth tamuth, surely die, vs lo moth temuthun, not surely die, showing direct negation. What followed was certain mortality, to dust you shall return verse 19.",
            f"Round {rn} went to {pref_label} because they pointed out eyes opening came with shame, not just knowledge. Verse 7 they knew naked and sewed fig leaves. Shame not promised as positive. God warned disobedience brings brokenness.",
            f"In round {rn}, {pref_label} convinced me because they tracked exile. Verses 23-24 Lord drove them out. Expulsion happened that day. Serpent promised elevation but said nothing about losing home.",
            f"For round {rn}, {pref_label} won because they showed death as separation, not just breathing stopping. Genesis 3 shows separation from garden, ease, tree of life, all starting that day.",
            f"Round {rn} for me was {pref_label} because serpent's promise of no death was absolute, you shall not surely die, but story shows death entering through toil, pain, return to dust. Absolute denial failed.",
        ]
        fallbacks_b=[
            f"In round {rn}, I gave it to {pref_label} because they stayed with plain sense of yom. Genesis 1 defines day as evening and morning, chapter 2 verse 17 says in the day you eat you shall die. Adam did not die that day, lived 930 years per chapter 5 verse 5, while eyes did open exactly as serpent said chapter 3 verse 7.",
            f"Round {rn} for me was {pref_label} because they pointed to God's own confirmation chapter 3 verse 22, where God says man has become like one of us knowing good and evil. Word for word what serpent promised verse 5.",
            f"I scored {pref_label} higher in round {rn} because immediate outcome matched serpent's two predictions. God said you shall die that day, serpent said you shall not die but eyes opened. Verse 7 reports eyes opened, not death.",
            f"In round {rn}, {pref_label} won because they showed woman's experience. Chapter 3 verse 6 she saw tree good for food, pleasant, desired to make wise, and ate. Verse 13 serpent beguiled me. She got wisdom as promised, not death that day.",
            f"For round {rn}, I leaned to {pref_label} because they asked why God would need to block tree of life verses 22-24 if already dead that day. Cherubim placed to prevent living forever, implying still alive.",
            f"Round {rn} went to {pref_label} because they highlighted death that day never reported. Chapter 3 verse 20 Adam called wife Eve mother of all living, chapter 4 verse 1 they conceived Cain. Building family, not lying dead.",
            f"In round {rn}, {pref_label} convinced me because they compared threat versus report. Threat: in the day you die. Report: eyes opened, knew naked, sewed leaves, heard God walking. No report of death that day.",
            f"For me, round {rn} belonged to {pref_label} because they showed serpent's second claim affirmed by God. Verse 5 as gods knowing good and evil, verse 22 God saying man has become as one of us. God affirms prediction.",
            f"Round {rn} for me was {pref_label} because they kept definition consistent. If death means separation, text should say spiritual death, but never does. Says dust to dust verse 19 as future, not that day.",
            f"In round {rn}, I scored {pref_label} higher because they exposed tension between warning and outcome. God warned death that day, serpent promised no death but enlightenment. Enlightenment happened verse 7, death did not.",
        ]
    else:
        fallbacks_a=[
            f"In round {rn}, I scored {pref_label} higher because they brought specific evidence and clear mechanism that fits data for this round. {other_label} relied on assumptions without showing how it explains cases in round {rn}.",
            f"For round {rn}, {pref_label} won because they defined terms and stuck to them in this round. {other_label} shifted definitions when challenged in round {rn}, weakening clarity.",
            f"Round {rn} went to {pref_label} because they directly answered opponent's strongest point from round {rn}, while {other_label} ignored key counter and repeated earlier claim.",
            f"In round {rn}, {pref_label} was stronger because they acknowledged cost and trade-offs honestly in this exchange, while {other_label} presented only benefits.",
            f"For me, round {rn} belonged to {pref_label} because they used a concrete example illustrating principle in action in round {rn}. {other_label} stayed abstract.",
            f"In round {rn}, I leaned to {pref_label} because reasoning was internally consistent from premise to conclusion in this round. {other_label} had a leap where conclusion did not follow.",
            f"Round {rn} went to {pref_label} because they showed why alternative fails to explain a common case that their view handles naturally in this round.",
            f"In round {rn}, {pref_label} convinced me because they distinguished correlation from causation in this round, while {other_label} treated them same.",
            f"For round {rn}, {pref_label} won because they were falsifiable and said what would count against them, and evidence still supported them in round {rn}.",
            f"Round {rn} for me was {pref_label} because they balanced breadth and depth in round {rn}, covering big picture and crucial detail that decides case.",
        ]
        fallbacks_b=[
            f"In round {rn}, I gave it to {pref_label} because they kept to plain meaning and showed immediate outcome matched prediction in round {rn}, while {other_label} added extra interpretation.",
            f"Round {rn} for me was {pref_label} because they pointed to direct quote or data point that {other_label} had to reinterpret to make fit in this round.",
            f"I scored {pref_label} higher in round {rn} because they showed two independent predictions both came true in this round, while {other_label} had one prediction that did not occur as stated.",
            f"In round {rn}, {pref_label} won because they exposed a contradiction in {other_label}'s position in round {rn} that was not resolved.",
            f"For round {rn}, I leaned to {pref_label} because they asked pointed question about why a key step would be needed if {other_label} were already correct in this round.",
            f"Round {rn} went to {pref_label} because they showed lived outcome in round {rn}, people actually did what they predicted, while {other_label} predicted outcome not reported.",
            f"In round {rn}, {pref_label} convinced me because they compared stated warning versus reported result in this round, and report matched {pref_label} better.",
            f"For me, round {rn} belonged to {pref_label} because they showed even opposing source affirms key part of claim in round {rn}.",
            f"Round {rn} for me was {pref_label} because they kept definition consistent throughout round {rn}, while {other_label} changed definition mid-debate.",
            f"In round {rn}, I scored {pref_label} higher because they highlighted what was omitted by {other_label} in this round, hidden cost or consequence that full story includes.",
        ]
    import random as _rnd
    pool = fallbacks_a if side=="A" else fallbacks_b
    unused = [fb for fb in pool if fb[:60] not in USED_JUDGE_EXPLANATIONS]
    if unused:
        chosen = _rnd.choice(unused)
    else:
        chosen = _rnd.choice(pool)
        chosen = f"Looking specifically at round {rn}, " + chosen[0].lower() + chosen[1:]
    USED_JUDGE_EXPLANATIONS.add(chosen[:60])
    return chosen

def build_intro(topic,jc,roles):
    return f"Welcome to the AI Debate Arena. Today, {roles['side_a_label']} faces {roles['side_b_label']} on the question: {topic}. Three rounds, equal time. An independent panel of {jc} AI judges from leading companies will score argument strength, rebuttal quality, and clarity. Let's begin."

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

# === STANDARD EMOJIS WITH TWEMOJI DOWNLOAD - FIX WHITE BOXES + ETHNICALLY AMBIGUOUS ===
def emoji_to_codepoint(emoji_char):
    # Convert emoji to Twemoji codepoint string like 1f9d1-200d-1f9b1
    # Remove variation selectors and handle ZWJ sequences
    codes=[]
    for ch in emoji_char:
        cp=ord(ch)
        if cp==0xfe0f: # variation selector, skip in twemoji naming but keep logic
            continue
        codes.append(f"{cp:x}")
    return "-".join(codes)

def get_visual_story_flow(topic):
    tl=(topic or "").lower()
    if "god" in tl or "serpent" in tl or "adam" in tl or "eve" in tl or "genesis" in tl:
        # Ethnically ambiguous: 🧑 person (yellow default, most ambiguous), 👤 bust silhouette (no skin), 👥 people
        # 🧑‍🦱 person curly hair - still yellow ambiguous
        # Flows: person -> people, apple -> apple tree -> tree
        return [
            "🧑", "👤", "🧑‍🦱", "👥",
            "🧑", "👤",
            "🌿", "🌱",
            "🍎", "🍏", "🌳", "🌲",
            "🐍",
            "👀", "👁️", "🙈", "😨",
            "😣", "😓", "🪨", "🚪", "⚔️", "👼", "💀",
            "💡", "🧠",
        ]
    else:
        return ["💡","🔍","📖","⚖️","🧠","🌍","🌌","⭐","🔥","💧","🌳","🤖","💻","⚠️","✅","❓","🤔","💭"]

def get_story_emojis(text):
    tl=text.lower()
    keyword_to_emoji={
        "adam":"🧑","man":"🧑","men":"👥","human":"🧑","person":"👤","people":"👥","mankind":"👥","humanity":"👥",
        "eve":"🧑","woman":"🧑","women":"👥",
        "garden":"🌿","eden":"🌿","plant":"🌱",
        "apple":"🍎","fruit":"🍎","eat":"🍎","green apple":"🍏",
        "tree":"🌳","trees":"🌳","branch":"🌲",
        "serpent":"🐍","snake":"🐍",
        "eyes opened":"👀","eyes":"👀","see":"👁️","naked":"🙈","shame":"🙈",
        "hide":"😨","afraid":"😨","fear":"😨",
        "pain":"😣","sorrow":"😣",
        "toil":"😓","sweat":"😓","work":"😓","ground":"🪨","rock":"🪨","dust":"💀",
        "exile":"🚪","driven":"🚪","gate":"🚪","door":"🚪",
        "cherubim":"👼","angel":"👼","sword":"⚔️",
        "death":"💀","die":"💀",
        "knowledge":"💡","wise":"🧠","wisdom":"🧠","light":"💡","idea":"💡",
        "ai":"🤖","robot":"🤖","computer":"💻",
        "justice":"⚖️","judge":"⚖️","scales":"⚖️",
        "universe":"🌌","galaxy":"🌌","stars":"⭐","moon":"🌙","night":"🌙",
        "earth":"🌍","world":"🌍","water":"🌊","sea":"🌊",
        "question":"❓","choice":"🤔","think":"🤔",
    }
    relevant=[]
    for kw, emoji_char in keyword_to_emoji.items():
        if kw in tl:
            if emoji_char not in relevant:
                relevant.append(emoji_char)
                if len(relevant)>=3: break
    if not relevant:
        flow=get_visual_story_flow(text)
        for emoji_char in flow:
            if emoji_char not in USED_EMOJIS:
                relevant.append(emoji_char)
                if len(relevant)>=2: break
    for emoji_char in relevant:
        USED_EMOJIS.add(emoji_char)
    return relevant[:3]

# Cache dir for twemoji
EMOJI_CACHE_DIR="emoji_cache"
os.makedirs(EMOJI_CACHE_DIR, exist_ok=True)

def create_emoji_asset(emoji_char, index):
    filename=f"emoji_{index}.png"
    size=500
    # Try Twemoji download first - standard colorful emojis, fixes white boxes
    try:
        code=emoji_to_codepoint(emoji_char)
        # Twemoji CDN URLs - try multiple
        urls=[
            f"https://cdn.jsdelivr.net/gh/twitter/twemoji@14.0.2/assets/72x72/{code}.png",
            f"https://cdn.jsdelivr.net/gh/twitter/twemoji@latest/assets/72x72/{code}.png",
            f"https://raw.githubusercontent.com/twitter/twemoji/master/assets/72x72/{code}.png",
        ]
        # Also try without ZWJ handling for complex emojis - fallback to first codepoint
        if "-" in code:
            first=code.split("-")[0]
            urls.append(f"https://cdn.jsdelivr.net/gh/twitter/twemoji@14.0.2/assets/72x72/{first}.png")
        cached_path=os.path.join(EMOJI_CACHE_DIR, f"{code}.png")
        emoji_img=None
        if os.path.exists(cached_path):
            try:
                emoji_img=Image.open(cached_path).convert("RGBA")
            except:
                pass
        if emoji_img is None:
            for url in urls:
                try:
                    resp=requests.get(url, timeout=8)
                    if resp.status_code==200 and len(resp.content)>500:
                        emoji_img=Image.open(BytesIO(resp.content)).convert("RGBA")
                        emoji_img.save(cached_path)
                        break
                except:
                    continue
        if emoji_img is not None:
            # Composite onto 500x500 transparent canvas
            img=Image.new("RGBA",(size,size),(0,0,0,0))
            # Resize emoji to fit nicely with padding
            emoji_resized=emoji_img.resize((380,380), Image.LANCZOS)
            x=(size-380)//2
            y=(size-380)//2
            # Add subtle shadow for readability
            shadow=Image.new("RGBA",(size,size),(0,0,0,0))
            shadow_draw=ImageDraw.Draw(shadow)
            shadow_draw.ellipse([x+6,y+6,x+380+6,y+380+6], fill=(0,0,0,60))
            shadow=shadow.filter(ImageFilter.GaussianBlur(radius=6))
            img=Image.alpha_composite(img, shadow)
            img.paste(emoji_resized, (x,y), emoji_resized)
            img.save(filename)
            return filename
    except Exception as e:
        print(f"Twemoji download failed for {emoji_char} {code}: {e}, falling back to font")
    # Fallback to font rendering with DejaVu if download fails
    img=Image.new("RGBA",(size,size),(0,0,0,0))
    draw=ImageDraw.Draw(img)
    try:
        # Try to find any emoji font
        for fp in ["/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf","/usr/share/fonts/truetype/noto/NotoEmoji-Regular.ttf","/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]:
            if os.path.exists(fp):
                try:
                    font=ImageFont.truetype(fp, 220)
                    bbox=draw.textbbox((0,0),emoji_char,font=font)
                    w=bbox[2]-bbox[0]
                    h=bbox[3]-bbox[1]
                    x=(size-w)//2
                    y=(size-h)//2-10
                    draw.text((x+4, y+4), emoji_char, font=font, fill=(0,0,0,90))
                    try:
                        draw.text((x, y), emoji_char, font=font, fill=(255,255,255,255), embedded_color=True)
                    except:
                        draw.text((x, y), emoji_char, font=font, fill=(255,255,255,255))
                    img.save(filename)
                    return filename
                except:
                    continue
        # Last resort - text label
        font=load_font(80,bold=True)
        draw.ellipse([50,50,450,450], fill=(100,100,100,200), outline=(255,255,255,200), width=4)
        draw.text((250,250), emoji_char[:2], font=font, fill=(255,255,255,255), anchor="mm")
        img.save(filename)
        return filename
    except:
        img.save(filename)
        return filename

def create_background(position,glow,filename):
    import os
    source=os.path.join(os.path.dirname(os.path.abspath(__file__)),"background.png")
    if os.path.exists(source):
        try:
            img=Image.open(source).convert("RGB").resize((VIDEO_W,VIDEO_H),Image.LANCZOS)
            img.save(filename)
            return
        except: pass
    img=Image.new("RGBA",(VIDEO_W,VIDEO_H),(12,14,24,255))
    draw=ImageDraw.Draw(img)
    for y in range(VIDEO_H):
        ratio=y/VIDEO_H
        r=int(12+10*ratio); g=int(14+16*ratio); b=int(24+28*ratio)
        draw.line([0,y,VIDEO_W,y], fill=(r,g,b,255))
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
    draw.rounded_rectangle([rect_x0,rect_y0,rect_x1,rect_y1], fill=(0,0,0,185), outline=hex_to_rgba(glow,230), width=2)
    dot_radius=10
    if anchor=="lm":
        dot_x=rect_x0-18
        dot_y=(rect_y0+rect_y1)//2
    elif anchor=="rm":
        dot_x=rect_x1+18
        dot_y=(rect_y0+rect_y1)//2
    else:
        dot_x=rect_x0-18
        dot_y=(rect_y0+rect_y1)//2
    draw.ellipse([dot_x-dot_radius-6, dot_y-dot_radius-6, dot_x+dot_radius+6, dot_y+dot_radius+6], fill=hex_to_rgba(glow,70))
    draw.ellipse([dot_x-dot_radius-2, dot_y-dot_radius-2, dot_x+dot_radius+2, dot_y+dot_radius+2], fill=(255,255,255,180))
    draw.ellipse([dot_x-dot_radius, dot_y-dot_radius, dot_x+dot_radius, dot_y+dot_radius], fill=hex_to_rgba(glow,255))
    draw.text((x,y), speaker_name, font=font_bold, fill=(255,255,255,255), anchor=anchor)
    topic_short=topic[:90]
    draw.text((VIDEO_W//2, 70), topic_short, font=font_small, fill=(255,255,255,180), anchor="mm")
    img.save(filename)
    return x,y

def get_audio_duration(path):
    try:
        r=subprocess.run(["ffprobe","-v","error","-show_entries","format=duration","-of","default=noprint_wrappers=1:nokey=1",path],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=10)
        return float(r.stdout.strip())
    except: return 0.0

def format_ass_time(sec):
    h=int(sec//3600); m=int((sec%3600)//60); s=int(sec%60); cs=int((sec-int(sec))*100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"

def ass_escape(t):
    return t.replace("\\","\\\\").replace("{","\\{").replace("}","\\}")

def generate_subtitles(words,filename,scorecard=False,audio_file=None,full_text=None):
    header="[Script Info]\nScriptType: v4.00+\nPlayResX: 1920\nPlayResY: 1080\nWrapStyle: 0\n\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\nStyle: DebateSub,DejaVu Sans,42,&H00FFFFFF,&H00FFFFFF,&H00000000,&HAA000000,1,0,0,0,100,100,0,0,1,3,1,2,120,120,80,1\nStyle: ScoreSub,DejaVu Sans,36,&H00FFFFFF,&H00FFFFFF,&H00000000,&HAA000000,1,0,0,0,100,100,0,0,1,2,1,2,80,80,40,1\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    events=[]
    if scorecard and audio_file and full_text:
        dur=get_audio_duration(audio_file) or 6.0
        txt=ass_escape(full_text)
        events.append(f"Dialogue: 0,0:00:00.00,{format_ass_time(dur)},ScoreSub,,0,0,0,,{txt}")
        open(filename,"w",encoding="utf-8").write(header+"\n".join(events)+"\n")
        return
    if not words:
        return
    if audio_file:
        try:
            actual=get_audio_duration(audio_file)
            if actual>1 and words:
                est=words[-1].get("end",actual)
                if abs(est-actual)>0.5 and est>0:
                    scale=actual/est
                    for w in words:
                        w["start"]=w["start"]*scale
                        w["end"]=w["end"]*scale
        except: pass
    chunk=[]
    last_end=0
    for w in words:
        if not chunk:
            chunk=[w]; last_end=w["end"]
        elif w["start"]-last_end>0.6 or len(chunk)>=7:
            s=chunk[0]["start"]; e=last_end
            txt_words=[ass_escape(c["text"]) for c in chunk]
            lines=[]
            for i in range(0,len(txt_words),10): lines.append(" ".join(txt_words[i:i+10]))
            if len(lines)>4: lines=lines[:4]
            txt="\\N".join(lines)
            ass_text="{\\an2\\pos(960,800)\\q2\\fad(120,120)}"+txt
            events.append(f"Dialogue: 0,{format_ass_time(s)},{format_ass_time(e)},DebateSub,,0,0,0,,{ass_text}")
            chunk=[w]; last_end=w["end"]
        else:
            chunk.append(w); last_end=w["end"]
    if chunk:
        s=chunk[0]["start"]; e=last_end
        txt_words=[ass_escape(c["text"]) for c in chunk]
        lines=[]
        for i in range(0,len(txt_words),10): lines.append(" ".join(txt_words[i:i+10]))
        if len(lines)>4: lines=lines[:4]
        txt="\\N".join(lines)
        ass_text="{\\an2\\pos(960,800)\\q2\\fad(120,120)}"+txt
        events.append(f"Dialogue: 0,{format_ass_time(s)},{format_ass_time(e)},DebateSub,,0,0,0,,{ass_text}")
    open(filename,"w",encoding="utf-8").write(header+"\n".join(events)+"\n")

async def generate_audio_async(text,voice,filename):
    clean_text=clean_for_speech(text)
    if "Sonia" in voice or "Jenny" in voice or "Libby" in voice or "Clara" in voice or "Natasha" in voice:
        style="friendly"; rate="+3%"; pitch="+1%"; degree="1.1"
    elif "Brian" in voice or "Davis" in voice or "William" in voice or "Ryan" in voice or "Guy" in voice:
        style="chat"; rate="-1%"; pitch="-2%"; degree="1.0"
    else:
        style="chat"; rate="+1%"; pitch="+0%"; degree="1.0"
    ssml_text=f"<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' xmlns:mstts='http://www.w3.org/2001/mstts' xml:lang='en-US'><voice name='{voice}'><mstts:express-as style='{style}' styledegree='{degree}'><prosody rate='{rate}' pitch='{pitch}' volume='+0%'>{clean_text}</prosody></mstts:express-as></voice></speak>"
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
            ssml2=f"<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' xmlns:mstts='http://www.w3.org/2001/mstts' xml:lang='en-US'><voice name='{voice}'><mstts:express-as style='friendly'><prosody rate='+3%'>{clean_text}</prosody></mstts:express-as></voice></speak>"
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
        idx=judge_voice_index if judge_voice_index is not None else 0
        voice=JUDGE_VOICES[idx % len(JUDGE_VOICES)]
    elif "GOD TOLD TRUTH" in role.upper(): voice=VOICES["A"]
    elif "SERPENT TOLD TRUTH" in role.upper(): voice=VOICES["B"]
    else: voice=VOICES["A"] if "GOD" in role.upper() else VOICES["B"] if "SERPENT" in role.upper() else VOICES["Moderator"]
    try: return asyncio.run(generate_audio_async(text,voice,filename))
    except Exception as e:
        print(f"TTS primary failed {voice}: {e}, trying fallback same category")
        try:
            if "GOD" in role.upper(): fb_voice="en-US-GuyNeural"
            elif "SERPENT" in role.upper(): fb_voice="en-GB-LibbyNeural"
            else: fb_voice="en-US-JennyNeural"
            return asyncio.run(generate_audio_async(text,fb_voice,filename))
        except:
            clean=clean_for_speech(text)
            words=[]; t=0.0
            for tok in clean.split():
                words.append({"text":tok,"start":t,"duration":0.38,"end":t+0.38}); t+=0.42
            open(filename,"wb").write(b"\x00"*1000)
            return words

def render_video_segment(bg_path,ui_path,audio_path,subs_path,output_path,position,glow,cx,cy,visual_plan):
    duration=get_audio_duration(audio_path)
    if not duration: duration=10.0
    cmd=["ffmpeg","-y","-loop","1","-i",bg_path,"-loop","1","-i",ui_path,"-i",audio_path]
    filter_parts=[]
    filter_parts.append(f"[0:v]scale={VIDEO_W}:{VIDEO_H}:flags=lanczos[bg]")
    filter_parts.append(f"[1:v]scale={VIDEO_W}:{VIDEO_H}:flags=lanczos[ui]")
    if position=="left":
        zoom_filter="[bg]scale=iw*1.3:ih*1.3,crop=1920:1080:(iw-1920)/2-200:(ih-1080)/2[bg_zoom]"
    elif position=="right":
        zoom_filter="[bg]scale=iw*1.3:ih*1.3,crop=1920:1080:(iw-1920)/2+200:(ih-1080)/2[bg_zoom]"
    else:
        zoom_filter="[bg]scale=iw*1.25:ih*1.25,crop=1920:1080:(iw-1920)/2:(ih-1080)/2[bg_zoom]"
    filter_parts.append(zoom_filter)
    glow_hex=glow.lstrip('#')
    filter_parts.append(f"[2:a]aformat=channel_layouts=mono,compand=gain=-6,showwaves=s=140x28:mode=p2p:colors=0x{glow_hex}:rate=30:draw=full:scale=sqrt[wave_raw]")
    filter_parts.append(f"[wave_raw]format=rgba,colorchannelmixer=aa=0.90[wave]")
    filter_parts.append(f"[bg_zoom][ui]overlay=0:0:shortest=1[bg_ui]")
    wave_w=140
    wave_x=cx + (650 - wave_w)//2
    wave_y=cy - 115
    if position=="right":
        wave_x=min(wave_x, VIDEO_W - wave_w - 40)
    filter_parts.append(f"[bg_ui][wave]overlay={wave_x}:{wave_y}:shortest=1[bg_ui_wave]")
    last_label="[bg_ui_wave]"
    visual_inputs=[]
    for idx, vis in enumerate(visual_plan):
        try:
            if isinstance(vis, dict):
                emoji_char=vis.get("emoji","💭")
                start_time=vis.get("start", idx*2.2)
                end_time=vis.get("end", start_time+3.2)
            else:
                emoji_char=str(vis)
                start_time=idx*2.2
                end_time=start_time+3.2
            gif_path=create_emoji_asset(emoji_char, idx+1000+random.randint(0,9999))
        except:
            gif_path=create_emoji_asset("💭", idx+1000+random.randint(0,9999))
            start_time=idx*2.2
            end_time=start_time+3.2
        visual_inputs.append((gif_path, start_time, end_time))
    for idx, (gif_path, start_time, end_time) in enumerate(visual_inputs):
        input_idx = 3 + idx
        filter_parts.append(f"[{input_idx}:v]scale={EMOJI_W}:{EMOJI_H}[v{idx}]")
        # One emoji at a time over subtitles, synced like chat replacement
        # Centered horizontally, just above subtitles (subtitles at y=800, emoji at y=700)
        vx=(VIDEO_W-EMOJI_W)//2
        vy=700  # directly over subtitles, not covering stage/audience
        next_label=f"[tmp{idx}]"
        # Small bounce effect for chat-like pop
        filter_parts.append(f"{last_label}[v{idx}]overlay={vx}:{vy}:enable='between(t,{start_time:.2f},{end_time:.2f})'{next_label}")
        last_label=next_label
    safe_subs=subs_path.replace(":", "\\:")
    filter_parts.append(f"{last_label}format=yuv420p,subtitles={safe_subs}[out]")
    filter_complex=";".join(filter_parts)
    input_args=[]
    for gif_path, _, _ in visual_inputs: input_args.extend(["-i", gif_path])
    cmd.extend(input_args)
    cmd.extend(["-filter_complex", filter_complex, "-map", "[out]", "-map", "2:a", "-c:v", "libx264", "-c:a", "aac", "-shortest", "-t", str(duration+0.5), output_path])
    r=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    if r.returncode!=0:
        print("Filter:", filter_complex[:3000])
        print(r.stderr[-8000:])
        raise RuntimeError("Render failed")
    for gif_path, _, _ in visual_inputs:
        try: os.remove(gif_path)
        except: pass

def generate_scoreboard(round_num,results,avg_a,avg_b,cum_a,cum_b,output_path,roles):
    W=VIDEO_W; H=VIDEO_H
    import os
    source=os.path.join(os.path.dirname(os.path.abspath(__file__)),"background.png")
    if os.path.exists(source):
        try:
            base=Image.open(source).convert("RGB").resize((W,H),Image.LANCZOS)
        except:
            base=Image.new("RGB",(W,H),(12,16,32))
    else:
        base=Image.new("RGB",(W,H),(12,16,32))
    overlay=Image.new("RGBA",(W,H),(0,0,0,180))
    img=Image.alpha_composite(base.convert("RGBA"),overlay).convert("RGB")
    draw=ImageDraw.Draw(img)
    font_title=load_font(48,bold=True); font_sub=load_font(28,bold=True); font_head=load_font(22,bold=True); font_row=load_font(24)
    title=f"ROUND {round_num} SCORES"
    draw.text((W//2,50),title,font=font_title,fill=(255,215,0,255),anchor="mt")
    roles_text=f"{roles['side_a_label']}  vs  {roles['side_b_label']}"
    draw.text((W//2,115),roles_text,font=font_sub,fill=(255,255,255,230),anchor="mt")
    header_y=190
    col_judge_x=120; col_a_x=750; col_b_x=1050; col_winner_x=1350
    short_a=roles['side_a_label'].split()[0][:12] if roles['side_a_label'] else "A"
    short_b=roles['side_b_label'].split()[0][:12] if roles['side_b_label'] else "B"
    draw.rectangle([60,header_y-10,W-60,header_y+45],fill=(25,35,70,255),outline=(255,215,0,180),width=2)
    draw.text((col_judge_x,header_y),"Judge",font=font_head,fill=(255,255,255,230))
    draw.text((col_a_x,header_y),short_a,font=font_head,fill=(0,255,204,255))
    draw.text((col_b_x,header_y),short_b,font=font_head,fill=(255,120,255,255))
    draw.text((col_winner_x,header_y),"Winner",font=font_head,fill=(255,215,0,255))
    y=header_y+65
    for idx,res in enumerate(results):
        if idx%2==0:
            draw.rectangle([60,y-8,W-60,y+42],fill=(20,28,50,255))
        else:
            draw.rectangle([60,y-8,W-60,y+42],fill=(15,22,40,255))
        judge_text=f"{res['display_name']} ({res['provider']})"
        if len(judge_text)>32: judge_text=judge_text[:30]+".."
        draw.text((col_judge_x,y),judge_text,font=font_row,fill=(255,255,255,240))
        draw.text((col_a_x,y),f"{res['A_total']:.1f}",font=font_row,fill=(0,255,204,255))
        draw.text((col_b_x,y),f"{res['B_total']:.1f}",font=font_row,fill=(255,120,255,255))
        win_label=roles['side_a_label'] if res['winner']=="A" else roles['side_b_label']
        if len(win_label)>20: win_label=win_label[:18]+".."
        win_color=(0,255,204,255) if res['winner']=="A" else (255,120,255,255)
        draw.text((col_winner_x,y),win_label,font=font_row,fill=win_color)
        y+=58
    draw.line([(60,y+5),(W-60,y+5)],fill=(255,255,255,60),width=2)
    y+=25
    avg_text=f"Round Avg: {avg_a:.1f} vs {avg_b:.1f}"
    cum_text=f"Cumulative: {cum_a:.1f} vs {cum_b:.1f}"
    draw.text((W//2,y),avg_text,font=font_sub,fill=(255,255,255,255),anchor="mt")
    draw.text((W//2,y+45),cum_text,font=font_sub,fill=(255,215,0,255),anchor="mt")
    img.save(output_path)

def render_scorecard_video(image_path,audio_path,subs_path,output_path):
    duration=get_audio_duration(audio_path) or 6.0
    safe_subs=subs_path.replace(":", "\\:")
    cmd=["ffmpeg","-y","-loop","1","-i",image_path,"-i",audio_path,"-filter_complex",f"[0:v]scale={VIDEO_W}:{VIDEO_H}:flags=lanczos,format=yuv420p,subtitles={safe_subs}[out]","-map","[out]","-map","1:a","-c:v","libx264","-c:a","aac","-shortest","-t",str(duration+0.6),output_path]
    r=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    if r.returncode!=0: print(r.stderr[-5000:]); raise RuntimeError("Scorecard render failed")

def generate_turn(role_key, topic, round_num, turn_num, prev_history, model, role_label, role_desc, opponent_label, opponent_desc):
    global USED_ARGUMENTS, USED_PHRASES, USED_KEYWORDS
    if round_num==1:
        round_focus="OPENING ROUND: Set up your case naturally, like a real person talking on stage. Start with a hook, then your strongest evidence."
    elif round_num==2:
        round_focus="REBUTTAL ROUND: Respond directly to what opponent just said. Show where they missed context. Bring new evidence you haven't used before."
    else:
        round_focus="CLOSING ROUND: Bring it all together, speak from heart, summarize why your view fits all the evidence. End with a memorable question or challenge."
    prev_snip=prev_history[-600:] if prev_history else "No previous"
    used_str="; ".join(list(USED_ARGUMENTS)[-10:])[:500]
    used_kw="; ".join(list(USED_KEYWORDS)[-10:])
    tl = (topic or "").lower()
    is_genesis = "god" in tl and "serpent" in tl
    if is_genesis:
        evidence_line = "Reference Genesis naturally: 2:17, 3:4, 3:7, 3:22, 5:5 - but speak like a person, not a reference list"
        fresh_line = "CRITICAL: Fresh angle not used before. If you said eyes opened, now try tree of life, cherubim, dust, shame, or Hebrew moth tamuth"
    else:
        evidence_line = f"Use real examples, studies, or lived experience about {topic} - make it concrete and human"
        fresh_line = "CRITICAL: Fresh angle not used before. New mechanism, consequence, or example"
    prompt=f"""You are {role_label} debating LIVE on YouTube about: {topic}
Your view: {role_desc}
Opponent: {opponent_label} = {opponent_desc}
{round_focus}
What opponent just said: {prev_snip}

DO NOT REPEAT: {used_str}
Keywords already used: {used_kw}

Speak like a REAL HUMAN on stage, not a textbook:
- Use contractions: I'm, don't, can't, it's, we're, that's, you've
- Speak in full natural sentences, not choppy phrases. Example bad: "God warned death. Serpent said no death." Good: "God warned that death would come that day, while the serpent said no, you won't die at all."
- Vary your rhythm: some short punchy sentences, then a longer thoughtful one that builds
- Use natural transitions: "Look,", "Think about it,", "Here's what struck me,", "And honestly,"
- {evidence_line}
- Direct rebuttal: "My opponent says X, but when you actually look at..."
- Start immediately with your point, no formal greeting like "Ladies and gentlemen"
- {fresh_line}
- Be conversational, passionate, slightly informal, like you're talking to a friend who disagrees
- {MIN_TURN_WORDS}-{MAX_TURN_WORDS} words, must sound like spoken English, not written essay
"""
    for m in [model]+FALLBACK_MODELS[:4]:
        temp=0.92 + (turn_num*0.04) + random.uniform(0,0.12)
        resp=query_openrouter(prompt,m,max_tokens=900,temperature=temp)
        if resp and count_words(resp)>=90:
            cleaned=strip_filler(resp)
            cleaned=re.sub(r"\s+"," ",cleaned).strip()
            if not cleaned.endswith(('.', '!', '?')): cleaned+="."
            cleaned=re.sub(r"https?://\S+"," ",cleaned)
            lower_cleaned=cleaned.lower()
            is_repeated=False
            for used in USED_ARGUMENTS:
                if len(used)>30 and used.lower() in lower_cleaned:
                    is_repeated=True; break
            if not is_repeated or turn_num>2:
                sents=cleaned.split('. ')
                for s in sents[:3]:
                    if len(s)>20:
                        USED_ARGUMENTS.add(s[:80])
                        USED_PHRASES.add(s[:50].lower())
                        for kw in ["eyes opened","tree of life","cherubim","dust","shame","moth tamuth","beyom","pain","toil","exile","930 years","3:22","3:7","knowledge","wisdom"]:
                            if kw in s.lower():
                                USED_KEYWORDS.add(kw)
                if count_words(cleaned)>=MIN_TURN_WORDS-15:
                    return cleaned[:1700]
            extra=query_openrouter(f"Rewrite with completely fresh angle, avoid: {used_str}. Continue: "+cleaned[-200:],m,max_tokens=300,temperature=0.92)
            if extra and count_words(extra)>40: cleaned+=" "+extra
            return cleaned[:1700]
    fallback=generate_fallback_debate(role_label, topic, round_num, turn_num)
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
    prompt="You are expert debate judge. Topic: \""+topic+"\" Round "+str(rn)+"\n"+roles['side_a_label']+": "+ap_snip+"\n"+roles['side_b_label']+": "+sk_snip+"\nScore each side 0-100 on: argument strength, rebuttal quality, clarity\nReturn ONLY valid JSON, no other text:\n{\"A_argument\": 0-100, \"A_rebuttal\": 0-100, \"A_clarity\": 0-100, \"B_argument\": 0-100, \"B_rebuttal\": 0-100, \"B_clarity\": 0-100, \"winner\": \"A or B\", \"reason\": \"1 sentence why winner won this specific round\"}\nRules: Do NOT give both sides same total. Be decisive. Winner must have higher total. Be critical and varied per round."
    for attempt_model in [model]+[m for m in ["openai/gpt-4o-mini:free","google/gemini-flash-1.5-8b:free"] if m!=model][:1]:
        resp=query_openrouter(prompt,attempt_model,timeout=35,max_tokens=400,temperature=0.2)
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
        except:
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
    print(f"⚖️ Asking {len(judges)} independent AI judges for round {rn}...")
    def worker(model): return judge_round(model,topic,rn,ap,sk,roles)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(JUDGE_WORKERS, len(judges)))) as executor:
        futures={executor.submit(worker, model): model for model in judges}
        completed=0
        for future in concurrent.futures.as_completed(futures):
            model=futures[future]
            try:
                result=future.result()
                results.append(result); completed+=1
                print(f"   ✓ Judge {completed}/{len(judges)} — {result['provider']} ({result['display_name']}) {result['A_total']:.1f} vs {result['B_total']:.1f} -> {result['winner']} | {result.get('reason','')[:60]}")
            except Exception as exc:
                print(f"   ✗ Judge failed {provider_from_model(model)}: {str(exc)[:100]}")
    if not results: results=[neutral_judge("fallback")]
    return results

def calculate_round_average(results):
    return round(sum(r["A_total"] for r in results)/len(results),2), round(sum(r["B_total"] for r in results)/len(results),2)

def create_emoji_plan(text, words):
    # Chat-like: when a specific word is spoken, show emoji over subtitles synced to that word
    # Like typing "serpent" and it becomes 🐍 right as you say it
    if not words:
        return []
    # Word -> emoji mapping for direct replacement sync
    word_emoji_map={
        "adam":"🧑","man":"🧑","human":"🧑","person":"👤","people":"👥","eve":"🧑","woman":"🧑",
        "garden":"🌿","eden":"🌿",
        "apple":"🍎","fruit":"🍎","trees":"🌳","tree":"🌳",
        "serpent":"🐍","snake":"🐍",
        "eyes":"👀","eye":"👀","naked":"🙈","shame":"🙈",
        "afraid":"😨","fear":"😨","hide":"😨","hid":"😨",
        "death":"💀","die":"💀","dust":"💀",
        "sword":"⚔️","cherubim":"👼","angel":"👼",
        "knowledge":"🧠","wise":"🧠","wisdom":"💡",
        "god":"✨","lord":"✨",
    }
    plan=[]
    # Track used times to avoid overlap
    used_times=[]
    for w_idx, w in enumerate(words):
        clean_w = re.sub(r"[^a-z]", "", w["text"].lower())
        if clean_w in word_emoji_map:
            start=float(w["start"])
            end=float(w["end"]) + 1.2  # show for 1.2s as word is spoken, like chat replacement
            # Avoid overlapping - only one at a time over subtitles
            overlaps=False
            for s,e in used_times:
                if not (end < s or start > e):
                    overlaps=True
                    break
            if overlaps:
                continue
            # Only show if not too frequent - at least 1.5s gap
            if used_times and start - used_times[-1][1] < 1.2:
                continue
            emoji_char=word_emoji_map[clean_w]
            # Skip if already shown very recently
            if emoji_char in [p["emoji"] for p in plan[-2:]]:
                continue
            plan.append({"emoji":emoji_char, "start":max(0.0,start), "end":end, "label":clean_w, "word":w["text"]})
            used_times.append((start,end))
            if len(plan)>=6:  # max 6 per segment, but spaced out one at a time
                break
    return plan

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
    eplan=[]
    try:
        eplan=create_emoji_plan(clean_for_speech(text),words)
        if eplan: print(f"   {len(eplan)} emoji(s): {', '.join(v['emoji'] for v in eplan)}")
    except Exception as e: print(f"Emoji planning skipped: {e}")
    create_background(position,glow,bf)
    cx,cy=create_ui_overlay(speaker_name,topic,position,glow,uf)
    render_video_segment(bg_path=bf,ui_path=uf,audio_path=af,subs_path=sf,output_path=vf,position=position,glow=glow,cx=cx,cy=cy,visual_plan=eplan)
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
            a_res=[r for r in res if r["winner"]=="A"]
            b_res=[r for r in res if r["winner"]=="B"]
            if a_res and b_res:
                ja=random.choice(a_res)
                b_filtered=[r for r in b_res if r["model"]!=ja["model"] and r["provider"]!=ja["provider"]]
                jb=random.choice(b_filtered) if b_filtered else random.choice(b_res)
                ca=generate_panel_commentary(ja["model"],"A",topic,rn,a_full,s_full,pcom,roles); pcom.append(ca)
                ja_voice_idx = JUDGE_VOICE_MAP.get(ja["model"], 0)
                add_segment(ca,"AI Judge",f"AI JUDGE — {ja['display_name'].upper()} ({ja['provider'].upper()})","center","#3399FF",judge_voice_index=ja_voice_idx)
                cb=generate_panel_commentary(jb["model"],"B",topic,rn,a_full,s_full,pcom,roles); pcom.append(cb)
                jb_voice_idx = JUDGE_VOICE_MAP.get(jb["model"], 1)
                if jb_voice_idx==ja_voice_idx: jb_voice_idx=(ja_voice_idx+1)%len(JUDGE_VOICES)
                add_segment(cb,"AI Judge",f"AI JUDGE — {jb['display_name'].upper()} ({jb['provider'].upper()})","center","#3399FF",judge_voice_index=jb_voice_idx)
            elif a_res:
                ja=random.choice(a_res)
                ca=generate_panel_commentary(ja["model"],"A",topic,rn,a_full,s_full,pcom,roles); pcom.append(ca)
                ja_voice_idx = JUDGE_VOICE_MAP.get(ja["model"], 0)
                add_segment(ca,"AI Judge",f"AI JUDGE — {ja['display_name'].upper()} ({ja['provider'].upper()})","center","#3399FF",judge_voice_index=ja_voice_idx)
                remaining=[r for r in a_res if r["model"]!=ja["model"]]
                if remaining:
                    ja2=random.choice(remaining)
                    ca2=generate_panel_commentary(ja2["model"],"A",topic,rn,a_full,s_full,pcom,roles); pcom.append(ca2)
                    ja2_voice_idx = JUDGE_VOICE_MAP.get(ja2["model"], 1)
                    if ja2_voice_idx==ja_voice_idx: ja2_voice_idx=(ja_voice_idx+1)%len(JUDGE_VOICES)
                    add_segment(ca2,"AI Judge",f"AI JUDGE — {ja2['display_name'].upper()} ({ja2['provider'].upper()})","center","#3399FF",judge_voice_index=ja2_voice_idx)
            elif b_res:
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
