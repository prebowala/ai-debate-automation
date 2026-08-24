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

MAX_VISUALS_PER_SEGMENT = 0  # Animations removed per user request
MIN_VISUAL_GAP = 3.0
VISUAL_W = 520
VISUAL_H = 520
VISUAL_Y = 160
MAX_EMOJIS_PER_SEGMENT = 3
EMOJI_W = 300
EMOJI_H = 300
USED_EMOJIS = set()
USED_VISUAL_LABELS = set()
USED_JUDGE_INTROS = set()

# UNIQUE VOICES - distinct, natural, less robotic, varied accents/pitches
VOICES = {
    "A": "en-US-BrianMultilingualNeural",  # Deep authoritative - GOD
    "B": "en-GB-SoniaNeural",  # British sly - SERPENT - distinct
    "Moderator": "en-AU-NatashaNeural",  # Warm Australian - MODERATOR
}
JUDGE_VOICES = [
    "en-US-JennyNeural",        # Warm US female
    "en-GB-RyanNeural",         # Deep British male
    "en-US-GuyNeural",          # Casual US male
    "en-GB-LibbyNeural",        # Bright British female
    "en-US-DavisNeural",        # Deep serious US male
    "en-AU-WilliamNeural",      # Australian male
    "en-CA-ClaraNeural",        # Canadian female
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
    topic_short = topic[:130] if len(topic)>130 else topic
    if "GOD TOLD TRUTH" in side_label.upper():
        god_templates = {
            (1,1): "Genesis 2:17 is clear. God says in the day you eat you shall surely die. Hebrew moth tamuth, dying you shall die. Serpent says in 3:4 lo moth temuthun, you shall not surely die. Direct contradiction. What happens that day? Chapter 3:7 eyes opened, they knew naked. Shame first time. Verse 8 hid from God's presence. Hiding is separation, Bible calls death.",
            (1,2): "Notice God's generosity in 2:16. You may freely eat of every tree, every tree, one limit. Incredibly generous. Serpent twists in 3:1 did God really say you shall not eat of every tree? Makes God sound stingy, holding out. Classic deception, misrepresenting to plant doubt.",
            (1,3): "Chapter 2:17 God commanded you shall surely die. Serpent 3:4 says you shall not surely die. One says die, other says won't. Can't both be true. Chapter 3:7-10 shows eyes opened, fear, shame, hiding. God warned death, relational death starts immediately.",
            (1,4): "Look at Hebrew ki, for in the day. Genesis 2:4 says in the day Lord God made earth and heavens. Not 24-hour deadline, means when. When you eat, death becomes certain. 3:19 to dust you shall return. Serpent promised no death, false.",
            (2,1): "Opponent says serpent told truth because they didn't drop dead that day. Misses what death means. Genesis 3:10 Adam afraid, hid. Fear not full life. Verse 19 dust you return. Mortality enters. Verses 23-24 driven out, cherubim block tree of life. That day they lost access to eternal life. Process started that day as warned.",
            (2,2): "Argument they did not die that day ignores beyom usage. Genesis 2:4 beyom Lord God made earth and heavens. Means when. About certainty, not countdown. When you eat, death certain. Serpent half-truth: eyes opened, yes, but left out terrible consequence. Half-truth omitting crucial consequence is still lie.",
            (2,3): "If serpent told whole truth, where warning about pain, toil, exile? Genesis 3:16-19 curses: pain, thorns, sweat, dust. Serpent said nothing. Said you shall be as gods. Chapter 3:22 they did become like God knowing good and evil, but at what cost? God told cost upfront. Serpent hid cost.",
            (2,4): "Think tree of life 3:22-24. God says lest he take also tree of life and live forever, therefore drove man out, placed cherubim to guard. So that day they lost immortality. Death beginning. Serpent said you shall not surely die, but they lost everlasting life that day.",
            (3,1): "Pull together. God warned in day you eat you shall surely die. Serpent said you shall not surely die, you shall be as gods. What happened? Eyes opened, yes, but also shame, fear, hiding, toil, pain, cut off from tree of life. That is death biblical sense, separation and mortality. Romans 5:12 sin entered and death through sin.",
            (3,2): "Who told truth? God said death would come. Serpent said no death, just enlightenment. Story shows both enlightenment and death same time. Eyes opened, but also shame, blame, cursing, exile. If serpent told whole truth, where warning about losing Eden? Where warning returning to dust? He omitted cost. God did not.",
            (3,3): "Consider character of God versus serpent. God creates, provides every tree, warns clearly. Serpent questions, distorts, promises without warning. 3:1 did God really say? Doubt. Verse 4 you shall not surely die. Denial. Verse 5 you shall be as gods. Desire. Pattern classic temptation: doubt, denial, desire. God told truth to protect.",
            (3,4): "Final: Hebrew moth tamuth in 2:17 infinitive absolute, emphasizing surely die. Serpent lo moth temuthun, not surely die, directly negating emphasis. What happens? They do die, not instant drop. They die relationally that day, spiritually, begin dying physically. Adam lives 930 years but does die. God's surely came true.",
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
            (1,1): f"On {topic_short}, {side_label} has stronger case when you look at evidence. Facts and logic point one direction. Opposing view relies on assumptions that don't hold. We should prefer explanation that fits what we actually see in real world. That is why {side_label} preferred.",
            (1,2): f"Consider {topic_short} from first principles. {side_label} claims specific mechanism that can be tested. When we check against observation, it matches. Alternative struggles to explain common cases. That is why opening favors {side_label}.",
            (1,3): f"I want to start with definition for {topic_short}. {side_label} defines terms clearly, avoids vague language. Says what would count as evidence against it. That falsifiability matters. Opponent shifts definition when challenged.",
            (1,4): f"Look at everyday experience relevant to {topic_short}. {side_label} matches what people actually encounter daily. Explains both typical and edge cases. Alternative needs extra assumptions to fit same data.",
            (2,1): f"My opponent raised points, but they don't address core evidence for {side_label} on {topic_short}. Counterexamples actually show when examined closely {side_label} accounts for them, while other view struggles. Logic holds step by step.",
            (2,2): f"Let's answer directly what opponent said about {topic_short}. They claimed {side_label} fails on certain cases. But look closer. Those cases actually support {side_label} when you check details. They misread evidence.",
            (2,3): f"Opponent tries to redefine terms for {topic_short}, but definition was clear at start. {side_label} keeps same definition throughout. Consistency matters. If you change definition mid-debate to avoid counterexample, you are not answering.",
            (2,4): f"On {topic_short}, opponent says {side_label} has implausible consequences. But follow logic. Consequences they cite are actually what we observe. They call them implausible because they conflict with intuition, not evidence.",
            (3,1): f"To close on {topic_short}, {side_label} offers coherent view that fits all evidence. Defines terms clearly, follows logic consistently, matches what we observe. Alternative relies on vague claims.",
            (3,2): f"Final thought on {topic_short}: {side_label} explains more with less. Fewer assumptions, broader coverage, makes testable predictions. Other view needs add-ons for each new case.",
            (3,3): f"Stepping back on {topic_short}, ask which view leaves you with better understanding? {side_label} gives mechanism, examples, handles objections. Other view says opponent wrong but doesn't give positive account.",
            (3,4): f"Closing on {topic_short}, {side_label} wins on clarity, consistency, evidence. Says what it means, doesn't contradict itself, matches observations. Alternative fails at least one.",
        }
        key = (round_num, turn_num if turn_num<=4 else ((turn_num-1)%4+1))
        return generic.get(key, generic[(3,4)])

USED_ARGUMENTS = set()
USED_PHRASES = set()
USED_KEYWORDS = set()
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
    prompt="You are expert debate judge. Topic: \""+topic+"\" Round "+str(rn)+"\n"+roles['side_a_label']+": "+ap_snip+"\n"+roles['side_b_label']+": "+sk_snip+"\nScore each side 0-100 on: argument strength, rebuttal quality, clarity\nReturn ONLY valid JSON, no other text:\n{\"A_argument\": 0-100, \"A_rebuttal\": 0-100, \"A_clarity\": 0-100, \"B_argument\": 0-100, \"B_rebuttal\": 0-100, \"B_clarity\": 0-100, \"winner\": \"A or B\", \"reason\": \"1 sentence why winner won\"}\nRules: Do NOT give both sides same total. Be decisive. Winner must have higher total. Avoid 50-50. Be critical and varied."

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
    # More natural, less robotic - varied style, pitch, rate per voice
    if "Sonia" in voice or "Ava" in voice or "Jenny" in voice or "Libby" in voice or "Clara" in voice:
        # Female voices - warmer, slightly faster, more expressive
        style = "friendly"
        rate = "+4%"
        pitch = "+2%"
        degree = "1.1"
    elif "Brian" in voice or "Davis" in voice or "William" in voice or "Ryan" in voice or "Guy" in voice:
        # Male voices - deeper, slower, authoritative but natural
        style = "chat"
        rate = "-1%"
        pitch = "-3%"
        degree = "1.0"
    else:
        style = "chat"
        rate = "+2%"
        pitch = "+0%"
        degree = "1.1"
    ssml_text = f"<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' xmlns:mstts='http://www.w3.org/2001/mstts' xml:lang='en-US'><voice name='{voice}'><mstts:express-as style='{style}' styledegree='{degree}'><prosody rate='{rate}' pitch='{pitch}' volume='+0%'>{clean_text}</prosody></mstts:express-as></voice></speak>"
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


def get_story_emojis(text):
    """Return relevant emojis based on story content - clear and makes sense"""
    tl = text.lower()
    emojis = []
    mapping = [
        (["heaven", "sky", "sun", "light", "day", "god light", "creator", "lord light"], ["☀️", "🌤️", "✨"]),
        (["serpent", "snake"], ["🐍"]),
        (["apple", "fruit", "eat", "tree", "garden", "eden"], ["🍎", "🌳", "🌿"]),
        (["eyes opened", "eyes", "naked", "shame"], ["👀", "🙈"]),
        (["hide", "afraid", "fear"], ["😨", "🫣"]),
        (["pain", "sorrow", "childbirth"], ["😣", "💔"]),
        (["toil", "sweat", "thorns", "ground", "work", "push"], ["😓", "🪨", "⛏️"]),
        (["exile", "driven", "cherubim", "sword", "gate"], ["🚪", "⚔️", "👼"]),
        (["dust", "die", "death", "dust ground", "dying"], ["💀", "🌑"]),
        (["tree of life", "live forever", "immortal"], ["🌳", "♾️"]),
        (["knowledge", "wise", "know good evil"], ["🧠", "💡"]),
        (["lie", "deceive", "beguile", "trick"], ["🤥", "🎭"]),
        (["warn", "command"], ["⚠️", "📜"]),
        (["truth", "evidence"], ["📖", "✅"]),
        (["question", "choice"], ["❓", "🤔"]),
        (["ai", "robot", "computer", "intelligence", "algorithm", "data"], ["🤖", "💻", "🧠"]),
        (["scales", "justice", "judge"], ["⚖️", "👩‍⚖️"]),
        (["universe", "galaxy", "cosmos", "planet", "stars", "moon", "night"], ["🌌", "🌙", "⭐"]),
        (["earth", "land", "water", "sea"], ["🌍", "🌊"]),
        (["atom", "dna", "evolution"], ["⚛️", "🧬"]),
    ]
    for keywords, emoji_list in mapping:
        if any(kw in tl for kw in keywords):
            for e in emoji_list:
                if e not in USED_EMOJIS:
                    emojis.append(e)
                    if len(emojis) >= 2:
                        break
            if len(emojis) >= 2:
                break
    if not emojis:
        emojis = ["💭"]
    # Track used to avoid repeats
    for e in emojis:
        USED_EMOJIS.add(e)
    return emojis[:2]


def create_emoji_plan(text, words):
    """Create emoji plan - one every couple sentences, story-driven, no repeats, clear"""
    if not words:
        return []
    import re
    sents=re.split(r'[.!?]+', text)
    sents=[s.strip() for s in sents if len(s.strip())>15]
    plan=[]
    used_in_seg=set()
    for idx, sent in enumerate(sents[:6]):
        if idx%2!=0 and idx!=0:
            continue
        emojis=get_story_emojis(sent)
        if not emojis: continue
        # Find timing for this sentence
        sent_words=sent.lower().split()
        for w_idx in range(len(words)):
            if words[w_idx]["text"].lower() in sent_words:
                start=float(words[w_idx]["start"])
                end_idx=min(len(words)-1, w_idx+12)
                end=float(words[end_idx]["end"])
                if emojis[0] not in used_in_seg:
                    plan.append({"emoji":emojis[0], "start":max(0.0,start), "end":max(start+2.5,end), "label":emojis[0]})
                    used_in_seg.add(emojis[0])
                    break
        if len(plan)>=MAX_EMOJIS_PER_SEGMENT: break
    return plan


def create_emoji_asset(emoji, index):
    """Create transparent PNG with large emoji - clear, story-driven, not random"""
    filename = f"emoji_{index}.png"
    size = 300
    # Create transparent image
    img = Image.new("RGBA", (size, size), (0,0,0,0))
    draw = ImageDraw.Draw(img)
    # Try to load emoji-capable font, fallback to large default
    try:
        # Try Noto Color Emoji or Segoe UI Emoji or DejaVu
        font_paths = [
            "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
            "/usr/share/fonts/truetype/ancient-scripts/Symbola.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
        font = None
        for fp in font_paths:
            if os.path.exists(fp):
                try:
                    font = ImageFont.truetype(fp, 180)
                    break
                except:
                    continue
        if font is None:
            font = load_font(180, bold=True)
    except:
        font = load_font(180, bold=True)
    # Draw emoji centered
    try:
        bbox = draw.textbbox((0,0), emoji, font=font)
        w = bbox[2]-bbox[0]
        h = bbox[3]-bbox[1]
        x = (size - w)//2
        y = (size - h)//2 - 10
        # Add slight shadow for readability
        draw.text((x+4, y+4), emoji, font=font, fill=(0,0,0,100))
        draw.text((x, y), emoji, font=font, fill=(255,255,255,255))
    except Exception as e:
        # Fallback: draw as text
        draw.text((size//2, size//2), emoji, font=font, fill=(255,255,255,255), anchor="mm")
    img.save(filename)
    print(f"   Created emoji: {emoji} -> {filename}")
    return filename

def create_visual_asset(visual,index):
    # DEPRECATED - animations removed, now returns emoji asset instead for story
    # This wrapper keeps old calls working but creates emoji instead of random pictures
    label = visual.get('label','') if isinstance(visual, dict) else str(visual)
    emojis = get_story_emojis(label)
    if emojis:
        return create_emoji_asset(emojis[0], index)
    return create_emoji_asset("💭", index)

def create_background(position,glow,filename):
    import os
    source=os.path.join(os.path.dirname(os.path.abspath(__file__)),"background.png")
    if os.path.exists(source):
        try:
            img=Image.open(source).convert("RGB").resize((VIDEO_W,VIDEO_H),Image.LANCZOS)
            img.save(filename)
            return
        except: pass
    # Subtle dark gradient, no big circles - fixes blue circle bug
    img=Image.new("RGBA",(VIDEO_W,VIDEO_H),(12,14,24,255))
    draw=ImageDraw.Draw(img)
    for y in range(VIDEO_H):
        ratio=y/VIDEO_H
        r=int(12+10*ratio); g=int(14+16*ratio); b=int(24+28*ratio)
        draw.line([0,y,VIDEO_W,y], fill=(r,g,b,255))
    # Small subtle glow, not 380px big circles
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
    # FIXED: Background restored, soundbars restored less wide safe, emojis centered story
    duration=get_audio_duration(audio_path)
    if not duration: duration=10.0
    cmd=["ffmpeg","-y","-loop","1","-i",bg_path,"-loop","1","-i",ui_path,"-i",audio_path]
    filter_parts=[]
    filter_parts.append(f"[0:v]scale={VIDEO_W}:{VIDEO_H}:flags=lanczos[bg]")
    filter_parts.append(f"[1:v]scale={VIDEO_W}:{VIDEO_H}:flags=lanczos[ui]")
    # Soundbars less wide, safe on screen, color-matched, closer to name - like attached rounded bars
    glow_hex=glow.lstrip('#')
    filter_parts.append(f"[2:a]aformat=channel_layouts=mono,compand=gain=-6,showwaves=s=280x48:mode=p2p:colors=0x{glow_hex}:rate=30:draw=full:scale=sqrt[wave_raw]")
    filter_parts.append(f"[wave_raw]format=rgba,colorchannelmixer=aa=0.92[wave]")
    filter_parts.append(f"[bg][ui]overlay=0:0:shortest=1[bg_ui]")
    wave_w=280
    wave_x=cx + (650 - wave_w)//2
    wave_y=cy - 58
    if position=="right":
        wave_x=min(wave_x, VIDEO_W - wave_w - 20)
    filter_parts.append(f"[bg_ui][wave]overlay={wave_x}:{wave_y}:shortest=1[bg_ui_wave]")
    last_label="[bg_ui_wave]"
    visual_inputs=[]
    for idx, vis in enumerate(visual_plan):
        # visual_plan now contains emojis, not random images
        try:
            if isinstance(vis, dict):
                emoji_char=vis.get("emoji", vis.get("label","💭"))
                start_time=vis.get("start", idx*3.0)
                end_time=vis.get("end", start_time+2.5)
            else:
                emoji_char=str(vis)
                start_time=idx*3.0
                end_time=start_time+2.5
            gif_path=create_emoji_asset(emoji_char, idx+1000+random.randint(0,9999))
        except:
            gif_path=create_emoji_asset("💭", idx+1000+random.randint(0,9999))
            start_time=idx*3.0
            end_time=start_time+2.5
        visual_inputs.append((gif_path, start_time, end_time))
    for idx, (gif_path, start_time, end_time) in enumerate(visual_inputs):
        input_idx = 3 + idx
        filter_parts.append(f"[{input_idx}:v]scale={EMOJI_W}:{EMOJI_H}[v{idx}]")
        # Center emojis in middle, clear story, not random positions
        vx=(VIDEO_W-EMOJI_W)//2
        vy=180
        next_label=f"[tmp{idx}]"
        filter_parts.append(f"{last_label}[v{idx}]overlay={vx}:{vy}:enable='between(t,{start_time:.2f},{end_time:.2f})'{next_label}")
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
    safe_subs = subs_path.replace(":", "\\:")
    cmd=["ffmpeg","-y","-loop","1","-i",image_path,"-i",audio_path,"-filter_complex",f"[0:v]scale={VIDEO_W}:{VIDEO_H}:flags=lanczos,format=yuv420p,subtitles={safe_subs}[out]","-map","[out]","-map","1:a","-c:v","libx264","-c:a","aac","-shortest","-t",str(duration+0.6),output_path]
    r=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    if r.returncode!=0: print(r.stderr[-5000:]); raise RuntimeError("Scorecard render failed")

USED_JUDGE_EXPLANATIONS = set()
def generate_panel_commentary(model,side,topic,rn,ap,sk,prev,roles):
    # OVERHAULED: Ensure explanation matches winner, no repeats, not robotic, full sentences
    prov=get_judge_short_name(model); comp=get_company_name(model)
    pref_label = roles['side_a_label'] if side=="A" else roles['side_b_label']
    other_label = roles['side_b_label'] if side=="A" else roles['side_a_label']
    recent="\n".join(prev[-4:]); used_expl = "\n".join(list(USED_JUDGE_EXPLANATIONS)[-6:])
    def trim(t,mw=160): wl=t.split(); return t if len(wl)<=mw else " ".join(wl[-mw:])
    
    # Get actual scores to ensure reasoning matches
    # Force model to argue FOR pref_label, not against
    if side=="A":
        prompt=f"You are {prov} from {comp}, judging round {rn} on {topic}.\nYou scored {pref_label} HIGHER than {other_label}. Your job is to explain why {pref_label} WON this round.\n{pref_label} arguments: {trim(ap)}\n{other_label} arguments: {trim(sk)}\nRULES: You MUST argue that {pref_label} won. Do NOT say {other_label} won. Do NOT praise {other_label} as winner. Explain 2-3 specific reasons {pref_label} was stronger - use verse, logic, or rebuttal that others haven't used. Avoid repeating: {used_expl} and {recent}. Speak naturally as {prov}, full sentences, human-like, not robotic. If you scored {pref_label} higher, your reasoning MUST support {pref_label}."
    else:
        prompt=f"You are {prov} from {comp}, judging round {rn} on {topic}.\nYou scored {pref_label} HIGHER than {other_label}. Your job is to explain why {pref_label} WON this round.\n{pref_label} arguments: {trim(ap)}\n{other_label} arguments: {trim(sk)}\nRULES: You MUST argue that {pref_label} won. Do NOT say {other_label} won. Do NOT praise {other_label} as winner. Explain 2-3 specific reasons {pref_label} was stronger and point out a specific weakness in {other_label} others missed. Avoid repeating: {used_expl} and {recent}. Speak naturally as {prov}, full sentences, human-like, not robotic. If you scored {pref_label} higher, your reasoning MUST support {pref_label}."
    
    resp=query_openrouter(prompt,model,timeout=30,max_tokens=400,temperature=0.85)
    if resp and len(resp.split())>=12:
        # Validate that it actually supports pref_label, not other
        low = resp.lower()
        # If it says other_label won, or other is better, reject and use fallback
        if f"{other_label.lower()} won" in low or f"{other_label.lower()} was stronger" in low or f"i scored {other_label.lower()} higher" in low:
            print(f"Judge {prov} mismatched winner, using fallback")
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
    
    # Fallbacks that correctly match side - no mismatch
    fallbacks_a=[
        f"In round {rn}, I scored {pref_label} higher because they stuck to what Genesis actually says happened that day. Chapter 3 verse 7 says their eyes were opened, and verse 22 shows God confirming they became like us knowing good and evil. That's immediate fulfillment, not later theology. {other_label} added ideas like spiritual death that Genesis 2-3 never mentions.",
        f"For round {rn}, {pref_label} won for me because they explained the Hebrew phrase beyom. In Genesis 2 verse 4, in the day means when, not a 24-hour countdown. It emphasizes certainty, not timing. {other_label} assumed literal same-day physical drop that doesn't fit Adam living 930 years.",
        f"Round {rn} went to {pref_label} because they showed the serpent told a half-truth that hid the cost. He promised you'd be as gods, but he didn't mention pain, toil, exile, or losing access to the tree of life in verses 23-24. A half-truth omitting the painful consequence is still misleading.",
    ]
    fallbacks_b=[
        f"In round {rn}, I gave it to {pref_label} because they stayed with plain sense. Yom means day, and Genesis 1 shows evening and morning as a day. Adam didn't die that day, he lived centuries, while his eyes did open exactly as serpent said in chapter 3 verse 7. {other_label} redefined death to mean spiritual death, which the text never says.",
        f"Round {rn} for me was {pref_label} because they pointed to God's own words in chapter 3 verse 22, where God says man has become like one of us knowing good and evil. That's word for word what serpent promised in verse 5. If serpent lied, why does God confirm his second claim?",
        f"I scored {pref_label} higher in round {rn} because the immediate outcome matched serpent's prediction. God said in the day you die, serpent said you won't die but you'll be enlightened. The story reports enlightenment that day, not death that day. On what actually happened that day, serpent was more accurate.",
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
    
    prev=""; cum_a=0.0;    prev=""; cum_a=0.0; cum_b=0.0; pcom=[]
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
            a_res=[r for r in res if r["winner"]=="A"] or res
            b_res=[r for r in res if r["winner"]=="B"] or res
            ja=random.choice(a_res)
            b_filtered=[r for r in b_res if r["model"]!=ja["model"] and r["provider"]!=ja["provider"]]
            if b_filtered: jb=random.choice(b_filtered)
            else:
                b_filtered2=[r for r in b_res if r["model"]!=ja["model"]]
                jb=random.choice(b_filtered2) if b_filtered2 else random.choice(b_res)
                if jb["provider"]==ja["provider"]:
                    alt=[r for r in res if r["provider"]!=ja["provider"] and r["model"]!=ja["model"]]
                    if alt: jb=random.choice(alt)
            ca=generate_panel_commentary(ja["model"],"A",topic,rn,a_full,s_full,pcom,roles); pcom.append(ca)
            ja_voice_idx = JUDGE_VOICE_MAP.get(ja["model"], 0)
            add_segment(ca,"AI Judge",f"AI JUDGE — {ja['display_name'].upper()} ({ja['provider'].upper()})","center","#3399FF",judge_voice_index=ja_voice_idx)
            cb=generate_panel_commentary(jb["model"],"B",topic,rn,a_full,s_full,pcom,roles); pcom.append(cb)
            jb_voice_idx = JUDGE_VOICE_MAP.get(jb["model"], 1)
            if jb_voice_idx==ja_voice_idx: jb_voice_idx=(ja_voice_idx+1)%len(JUDGE_VOICES)
            add_segment(cb,"AI Judge",f"AI JUDGE — {jb['display_name'].upper()} ({jb['provider'].upper()})","center","#3399FF",judge_voice_index=jb_voice_idx)
    add_segment(build_outro(len(judges),cum_a,cum_b,roles),"Moderator","MODERATOR")
    stitch_segments(segs,OUTPUT_FILE)
    print(f"\nCOMPLETE: {OUTPUT_FILE} — {cum_a:.1f} vs {cum_b:.1f}")
    cleanup_cache()

if __name__=="__main__":
    try: run_debate_pipeline()
    except KeyboardInterrupt: print("Cancelled")
    except Exception as e: print("FAILED"); print(str(e)); raise
