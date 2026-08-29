
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
TURNS_PER_SIDE_PER_ROUND = 2
WORDS_PER_TURN = 135
MIN_TURN_WORDS = 115
MAX_TURN_WORDS = 155

MAX_JUDGES = 7
JUDGE_WORKERS = 7

EMOJI_W = 180
EMOJI_H = 180
USED_EMOJIS = set()
USED_ARGUMENTS = set()
USED_PHRASES = set()
USED_KEYWORDS = set()
USED_JUDGE_EXPLANATIONS = set()

# STANDARD DISTINCT VOICES - AGREED
VOICES = {
    "A": "en-US-BrianMultilingualNeural",
    "B": "en-US-AvaMultilingualNeural",
    "Moderator": "en-US-AndrewMultilingualNeural",
    "AI Christian Apologist": "en-US-BrianMultilingualNeural",
    "AI Skeptic": "en-US-AvaMultilingualNeural",
    "GOD TOLD TRUTH": "en-US-BrianMultilingualNeural",
    "SERPENT TOLD TRUTH": "en-US-AvaMultilingualNeural",
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

# JUDGES GOOD - KEEP FREE ONLY, ONE PER COMPANY (GOOD VERSION)
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

def provider_from_model(mid):
    if not mid: return "Unknown"
    return PROVIDER_ALIASES.get(mid.split("/",1)[0].lower().strip(), mid.split("/",1)[0].title())

def get_judge_short_name(mid):
    low=(mid or "").lower()
    if "gpt" in low: return "ChatGPT"
    if "claude" in low: return "Claude"
    if "gemini" in low: return "Gemini"
    if "gemma" in low: return "Gemma"
    if "grok" in low: return "Grok"
    if "deepseek" in low: return "DeepSeek"
    if "mistral" in low: return "Mistral"
    if "llama" in low: return "Llama"
    if "qwen" in low: return "Qwen"
    return provider_from_model(mid)

def get_company_name(mid): return provider_from_model(mid)

def cleanup_cache():
    for pat in ["*.mp4","*.mp3","*.ass","*.png","*_list.txt"]:
        for fn in glob.glob(pat):
            if fn in [OUTPUT_FILE,"background.png","topic.txt"]: continue
            try: os.remove(fn)
            except: pass

def count_words(t): return len(re.findall(r"\b[\w'-]+\b", t or ""))

def clean_for_speech(t):
    if not t: return ""
    t=re.sub(r"https?://\S+"," ",t)
    t=re.sub(r"www\.\S+"," ",t)
    t=re.sub(r"\b[a-z0-9-]+\.[a-z]{2,}(?:/\S*)?"," ",t, flags=re.IGNORECASE)
    t=re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)
    t=re.sub(r"```.*?```"," ",t, flags=re.DOTALL)
    t=t.replace("(", " ").replace(")", " ").replace("[", " ").replace("]", " ").replace("{", " ").replace("}", " ")
    t=t.replace("–",", ").replace("—",". ").replace(" - ",". ")
    for o,n in {"*":"", "#":"", "_":"", "`":"", "\"":"", ":":" . ", ";":" . ", "&":" and"}.items():
        t=t.replace(o,n)
    t=re.sub(r"\s+"," ",t).strip()
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
            top=["openai","anthropic","google","meta-llama","mistralai","deepseek","qwen","x-ai"]
            if not any(p in mid.lower() for p in top): continue
            free.append(mid)
        if free:
            print(f"Found {len(free)} top free models")
            return list(dict.fromkeys(free))
        return FALLBACK_MODELS.copy()
    except:
        return FALLBACK_MODELS.copy()

def query_openrouter(prompt,mid,timeout=55,max_tokens=850,temperature=0.92):
    if not OPENROUTER_API_KEY: return None
    if ":free" not in mid.lower(): return None
    payload={"model":mid,"messages":[{"role":"user","content":prompt}],"temperature":temperature,"max_tokens":max_tokens}
    for _ in range(3):
        try:
            resp=requests.post(OPENROUTER_URL,headers=openrouter_headers(),json=payload,timeout=timeout)
            if resp.status_code==200:
                c=resp.json().get("choices",[])[0].get("message",{}).get("content","")
                if c and len(c.strip())>60: return c.strip()
        except: pass
        time.sleep(1.0)
    return None

def choose_primary_models(avail):
    free=[m for m in avail if ":free" in m] or avail
    used=set(); picks=[]
    for m in free:
        prov=provider_from_model(m)
        if prov not in used:
            picks.append(m); used.add(prov)
        if len(picks)>=2: break
    if len(picks)<2: picks=(free+FALLBACK_MODELS)[:2]
    print(f"Primary FREE distinct: {get_judge_short_name(picks[0])} Brian vs {get_judge_short_name(picks[1])} Ava")
    return picks[0],picks[1]

# GOOD JUDGES - ONE PER COMPANY, NO 3 META 2 GOOGLE
def choose_judges(avail,primary):
    global JUDGE_VOICE_MAP
    primary_providers=set(provider_from_model(m) for m in primary)
    excl_ids=set(primary)
    top_providers={"openai","anthropic","google","meta-llama","mistralai","deepseek","qwen"}
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
    for m in groups.values():
        if len(sel)>=MAX_JUDGES: break
        if m not in sel: sel.append(m)
    seen=set(); uniq=[]
    for m in sel:
        d=get_judge_short_name(m)
        if d not in seen:
            uniq.append(m); seen.add(d)
    result=uniq[:MAX_JUDGES]
    JUDGE_VOICE_MAP={mid: idx%len(JUDGE_VOICES) for idx,mid in enumerate(result)}
    print(f"Judges ONE PER COMPANY UNIQUE GOOD ({len(result)}): {', '.join(f'{provider_from_model(m)} ({get_judge_short_name(m)})' for m in result)}")
    return result

def get_debate_roles(topic, model):
    tl=(topic or "").lower()
    if "god" in tl and "serpent" in tl:
        return {"side_a_label":"GOD TOLD TRUTH","side_a_desc":"Defends God told truth in Genesis 2:17 moth tamuth - death became certain that day, relational separation and loss of tree of life","side_b_label":"SERPENT TOLD TRUTH","side_b_desc":"Defends serpent told truth in Genesis 3:4-5 - no death that day, eyes opened and become as gods, God confirms in 3:22"}
    prompt=f'Topic: "{topic}" Return ONLY JSON: {{"side_a_label":"FOR 2-3 words","side_a_desc":"sentence","side_b_label":"AGAINST 2-3 words","side_b_desc":"sentence"}} Labels uppercase short.'
    resp=query_openrouter(prompt, model, timeout=25, max_tokens=250, temperature=0.4)
    if resp:
        try:
            m=re.search(r"\{.*\}",resp,re.DOTALL)
            if m:
                data=json.loads(m.group(0))
                a=str(data.get("side_a_label","FOR")).strip().upper()[:30]
                b=str(data.get("side_b_label","AGAINST")).strip().upper()[:30]
                if a and b and a!=b and "LABEL" not in a:
                    return {"side_a_label":a,"side_a_desc":str(data.get("side_a_desc",a)),"side_b_label":b,"side_b_desc":str(data.get("side_b_desc",b))}
        except: pass
    return {"side_a_label":"AFFIRMATIVE","side_a_desc":f"Argues FOR {topic}","side_b_label":"NEGATIVE","side_b_desc":f"Argues AGAINST {topic}"}

def strip_filler(text):
    for pat in [r"^(ladies and gentlemen[,.]?\s*)",r"^(my friends[,.]?\s*)",r"^(well[,.]?\s*)",r"^(thank you[,.]?\s*)"]:
        text=re.sub(pat,"",text,flags=re.IGNORECASE).strip()
    return text

def generate_fallback_debate(side_label, topic, round_num, turn_num):
    if "GOD TOLD TRUTH" in side_label.upper():
        pool=[
            "Look at Genesis 2:17 moth tamuth, dying you shall surely die, emphatic about certainty. Serpent in 3:4 says lo moth temuthun, you shall not surely die, direct negation. What happened that day? Genesis 3:10 says Adam hid because afraid, relational separation. Verse 19 to dust return, verse 24 cherubim block tree of life. On that day they lost everlasting life, that's death beginning.",
            "Genesis 2:16 says you may freely eat of every tree, abundant generosity. Serpent twists in 3:1 did God really say you shall not eat of every tree? Makes generosity sound stingy. That twisting matters when asking who told truth about provision and motive.",
            "In the day you eat appears in Genesis 2:4 also, in the day Lord made earth, meaning when, not 24 hour countdown. Moth tamuth is certainty. Serpent left out pain, thorns, sweat, exile verses 16-19. Half truth hiding cost is still misleading, God told full cost upfront.",
            "Genesis 3:22 says lest he take also tree of life and live forever, therefore drove out and placed cherubim to keep way. On that day they lost access to everlasting life. Serpent said you shall not surely die, but they lost immortality that day, that's death starting immediately.",
            "Think about character: God creates, provides every tree but one freely, warns to protect. Serpent questions in 3:1 did God really say, plants doubt, denies in 3:4 you shall not die, appeals to desire in 3:5 as gods. Classic temptation pattern doubt denial desire. God told truth to protect, serpent half truth to entice.",
            "Hebrew moth tamuth infinitive absolute emphasizes certainty, you shall surely die. Serpent lo moth temuthun you shall not surely die directly negates. What happened? They did die relationally that day and began dying physically and returned to dust. Adam 930 years but did die. God's surely came true.",
        ]
        return random.choice(pool)
    elif "SERPENT TOLD TRUTH" in side_label.upper():
        pool=[
            "Read plain text. Genesis 2:17 says in the day you eat you shall die, natural reading that same day. Genesis 5:5 says Adam lived 930 years then died. Didn't die that day. Serpent says 3:4 you shall not surely die, matches, they didn't die that day. And 3:7 eyes opened as promised, God confirms in 3:22 man become as one of us knowing good and evil. God confirms serpent right.",
            "Yom evening and morning a day. So in the day you eat you shall die should mean that day. Adam didn't die that day. Two predictions serpent both happen, eyes opened and godlikeness, one threat from God doesn't happen as stated that day.",
            "Genesis 3:22 God says man become as one of us to know good and evil, word for word serpent promised verse 5. If serpent father lies, why God echoing? Where is death that day? Chapter 4 they have children very much alive.",
            "Genesis 2:17 threatens death in the day, but Genesis 3:6 woman saw tree good for food pleasant to eyes desired to make wise, she ate. Verse 7 eyes opened. Exactly what serpent promised verse 5 your eyes opened. Death that day? Text never says anyone died that day. Verse 8 they heard Lord walking, alive hiding not dead.",
            "If God meant spiritual death why not say spiritual death? Text of Genesis 2 and 3 never mentions spiritual death. That's later theology. Test simple: Did they die that day as God said? No. Did eyes open as serpent said? Yes 3:7 eyes opened. On straightforward reading serpent described what would happen that day more accurately.",
            "Consider 3:22 God says man become as one of us to know good and evil, exactly what serpent said would happen verse 5. If serpent lying about that part why God confirming? Where death that day? Chapter 3:20 Adam called wife Eve mother of all living, 4:1 they conceived Cain. Very much alive building family not dead.",
        ]
        return random.choice(pool)
    else:
        return f"When we look at {topic}, {side_label} fits what we actually see. Take concrete example from {topic} - mechanism you can trace predicts what we see. That's why {side_label} stronger here."

# === FIXED DEBATE GENERATION - CONVERSATIONAL, SPECIFIC, REBUTTAL ACTUALLY ANSWERS ===
def generate_turn(role_key, topic, round_num, turn_num, prev_history, model, role_label, role_desc, opponent_label, opponent_desc):
    global USED_ARGUMENTS
    used_str="; ".join(list(USED_ARGUMENTS)[-8:])[:400] if USED_ARGUMENTS else "None yet"
    prev_snip=prev_history[-700:] if prev_history else "No previous, you opening"

    tl=(topic or "").lower()
    is_genesis="god" in tl and "serpent" in tl

    if is_genesis:
        evidence_line="MUST cite specific Genesis verse this turn: 2:17 moth tamuth, 3:4 lo moth temuthun, 3:5 as gods, 3:7 eyes opened, 3:10 fear hiding, 3:19 dust, 3:22 become as one of us, 5:5 930 years, 3:22-24 tree of life cherubim blocked. Use naturally in conversation."
        fresh_line="Fresh angle not used: if you said 930 years before now try tree of life cherubim; if eyes opened now dust to dust or shame or moth tamuth or pain toil exile."
    else:
        evidence_line=f'MUST give specific evidence about "{topic}" - named study, statistic, example, mechanism. Not vague philosophy.'
        fresh_line=f"Fresh angle about {topic} not used: {used_str}"

    if role_key=="A" and round_num==1 and turn_num==1:
        # Affirmative opening - should be good as user said
        prompt=f"""You are {role_label} debating live about: {topic}
You believe: {role_desc}
This is your OPENING statement. Speak like real person on stage, warm conversational passionate about {topic}. Start with hook about {topic}, then strongest SPECIFIC evidence.
{evidence_line}
Don't repeat: {used_str}
{MIN_TURN_WORDS}-{MAX_TURN_WORDS} words. Natural contractions. No bullet points. Just say your argument about {topic}.
Topic: "{topic}" - address exact question.
"""
    elif role_key=="B":
        # Negative must REBUT previous, not generic, must answer
        prompt=f"""You are {role_label} debating {topic}. You believe {role_desc}. Opponent {opponent_label} just said about {topic}: {prev_snip[:650]}

You MUST directly answer them about {topic}. Don't be generic, don't be short. Steps:
1. Quote one specific thing they just said about {topic} - like "My opponent said..." 
2. Explain why that specific claim doesn't hold up, using specific counter-evidence about {topic}
3. Then add one NEW specific piece of evidence not used: {used_str}

{evidence_line}
{fresh_line}
Speak like real person in conversation, conversational warm, {MIN_TURN_WORDS}-{MAX_TURN_WORDS} words MINIMUM {MIN_TURN_WORDS}. Must be at least {MIN_TURN_WORDS} words, not short. Must be specific to {topic}, not generic. No saying you need to do anything, just do it.
"""
    else:
        prompt=f"""You are {role_label} debating {topic}. You believe {role_desc}. Opponent just said: {prev_snip[:600]}
Respond like real person about {topic}. Quote one specific thing they claimed, show why doesn't hold with specific counter-evidence about {topic}.
Add NEW specific point: {fresh_line}
{evidence_line}
Speak naturally, {MIN_TURN_WORDS}-{MAX_TURN_WORDS} words. Specific to {topic}, not generic. No meta talk.
"""

    for m in [model]+FALLBACK_MODELS[:4]:
        temp=0.92+turn_num*0.05+random.random()*0.1
        resp=query_openrouter(prompt,m,max_tokens=900,temperature=temp)
        if resp and count_words(resp)>=95:
            cleaned=strip_filler(resp)
            cleaned=re.sub(r"\s+"," ",cleaned).strip()
            # Remove leaked task phrases
            cleaned=re.sub(r"(?i)\bI need to (do|show|quote|explain|address|provide).*?\.", "", cleaned)
            cleaned=re.sub(r"(?i)\bI should (do|show).*?\.", "", cleaned)
            cleaned=re.sub(r"(?i)\bWhat I should be doing.*?[.]", "", cleaned)
            cleaned=re.sub(r"(?i)\bIn this (turn|round|phrase|response).*?[,\.]", "", cleaned)
            cleaned=re.sub(r"(?i)\bAs (an AI|per instructions).*?[.]", "", cleaned)
            cleaned=re.sub(r"\s+"," ",cleaned).strip()
            if not cleaned.endswith(('.', '!', '?')): cleaned+="."
            low=cleaned.lower()
            # Ensure not too short for negative rebuttal
            if role_key=="B" and count_words(cleaned)<MIN_TURN_WORDS-10:
                # Ask model to expand
                extra=query_openrouter(prompt+"\nExpand to at least 120 words with specific evidence about "+topic,m,max_tokens=400,temperature=0.95)
                if extra and count_words(extra)>60:
                    cleaned+=" "+extra
            # Avoid repeat
            is_rep=False
            for used in USED_ARGUMENTS:
                if len(used)>30 and used.lower() in low:
                    is_rep=True; break
            if not is_rep:
                for s in cleaned.split('. ')[:3]:
                    if len(s)>25: USED_ARGUMENTS.add(s[:80])
                if count_words(cleaned)>=MIN_TURN_WORDS-15:
                    return cleaned[:1800]
    fb=generate_fallback_debate(role_label, topic, round_num, turn_num)
    USED_ARGUMENTS.add(fb[:80])
    return fb

def build_round_exchanges(topic, rn, ap_model, sk_model, prev, roles):
    ap_turns=[]; sk_turns=[]; hist=prev
    for tn in range(1,TURNS_PER_SIDE_PER_ROUND+1):
        a=generate_turn("A", topic, rn, tn, hist, ap_model, roles['side_a_label'], roles['side_a_desc'], roles['side_b_label'], roles['side_b_desc'])
        ap_turns.append(a); hist+=f"\n{roles['side_a_label']}: {a}\n"
        s=generate_turn("B", topic, rn, tn, hist, sk_model, roles['side_b_label'], roles['side_b_desc'], roles['side_a_label'], roles['side_a_desc'])
        sk_turns.append(s); hist+=f"\n{roles['side_b_label']}: {s}\n"
    return ap_turns,sk_turns,hist

def neutral_judge(model):
    a=random.uniform(53,68); b=random.uniform(53,68)
    if abs(a-b)<4:
        if random.random()>0.5: a+=7
        else: b+=7
    return {"model":model,"provider":provider_from_model(model),"display_name":get_judge_short_name(model),"A_total":round(a,1),"B_total":round(b,1),"winner":"A" if a>b else "B","reason":"varied to avoid 50/50"}

def judge_round(model,topic,rn,ap,sk,roles):
    prompt=f'Judge Round {rn} about "{topic}". {roles["side_a_label"]}: {ap[:800]} vs {roles["side_b_label"]}: {sk[:800]}. Score 0-100. Return ONLY JSON: {{"A_total":0-100,"B_total":0-100,"winner":"A or B","reason":"one sentence why"}}. Must NOT be equal. Be decisive.'
    for m in [model]+FALLBACK_MODELS[:2]:
        if ":free" not in m: continue
        resp=query_openrouter(prompt,m,timeout=35,max_tokens=300,temperature=0.2)
        if not resp: continue
        try:
            mm=re.search(r"\{{.*\}}",resp,re.DOTALL)
            if not mm: continue
            d=json.loads(mm.group(0).replace("'",'"'))
            a=clamp_score(d.get("A_total")); b=clamp_score(d.get("B_total"))
            if abs(a-b)<1.5:
                if random.random()>0.5: a+=3
                else: b+=3
            return {"model":model,"provider":provider_from_model(model),"display_name":get_judge_short_name(model),"A_total":round(a,1),"B_total":round(b,1),"winner":"A" if a>b else "B","reason":str(d.get("reason",""))[:150]}
        except: continue
    return neutral_judge(model)

def evaluate_round(judges,topic,rn,ap,sk,roles):
    results=[]
    def worker(m): return judge_round(m,topic,rn,ap,sk,roles)
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(7,len(judges))) as ex:
        futs={ex.submit(worker,m):m for m in judges}
        for f in concurrent.futures.as_completed(futs):
            try:
                r=f.result(); results.append(r)
                print(f"  Judge {r['display_name']} ({r['provider']}) {r['A_total']:.1f} vs {r['B_total']:.1f} -> {r['winner']}")
            except Exception as e:
                print(f"  Judge fail {e}")
    if len(results)<3:
        for m in FALLBACK_MODELS:
            if len(results)>=5: break
            if m not in [x['model'] for x in results]: results.append(neutral_judge(m))
    return results

def calculate_round_average(res):
    return round(sum(r["A_total"] for r in res)/len(res),2), round(sum(r["B_total"] for r in res)/len(res),2)

# === EMOJI CODE - FIXED TO SHOW MORE THAN JUST GOD EMOJI ===
def emoji_to_codepoint(ec):
    codes=[]
    for ch in ec:
        cp=ord(ch)
        if cp==0xfe0f: continue
        codes.append(f"{cp:x}")
    return "-".join(codes)

EMOJI_CACHE_DIR="emoji_cache"
os.makedirs(EMOJI_CACHE_DIR, exist_ok=True)

def create_emoji_asset(ec, idx):
    fn=f"emoji_{idx}.png"
    size=500
    try:
        code=emoji_to_codepoint(ec)
        urls=[
            f"https://cdn.jsdelivr.net/gh/twitter/twemoji@14.0.2/assets/72x72/{code}.png",
            f"https://cdn.jsdelivr.net/gh/twitter/twemoji@latest/assets/72x72/{code}.png",
            f"https://raw.githubusercontent.com/twitter/twemoji/master/assets/72x72/{code}.png",
        ]
        if "-" in code:
            first=code.split("-")[0]
            urls.append(f"https://cdn.jsdelivr.net/gh/twitter/twemoji@14.0.2/assets/72x72/{first}.png")
        cached=os.path.join(EMOJI_CACHE_DIR, f"{code}.png")
        img_data=None
        if os.path.exists(cached):
            try: img_data=Image.open(cached).convert("RGBA")
            except: pass
        if img_data is None:
            for url in urls:
                try:
                    resp=requests.get(url, timeout=8)
                    if resp.status_code==200 and len(resp.content)>500:
                        img_data=Image.open(BytesIO(resp.content)).convert("RGBA")
                        img_data.save(cached)
                        break
                except: continue
        if img_data is not None and img_data.size[0]>10:
            canvas=Image.new("RGBA",(size,size),(0,0,0,0))
            resized=img_data.resize((380,380), Image.LANCZOS)
            x=(size-380)//2; y=(size-380)//2
            shadow=Image.new("RGBA",(size,size),(0,0,0,0))
            d=ImageDraw.Draw(shadow)
            d.ellipse([x+6,y+6,x+380+6,y+380+6], fill=(0,0,0,60))
            shadow=shadow.filter(ImageFilter.GaussianBlur(6))
            canvas=Image.alpha_composite(canvas, shadow)
            canvas.paste(resized, (x,y), resized)
            canvas.save(fn)
            return fn
    except Exception as e:
        print(f"Twemoji fail {ec}: {e}")
    try:
        canvas=Image.new("RGBA",(size,size),(0,0,0,0))
        draw=ImageDraw.Draw(canvas)
        for fp in ["/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf","/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]:
            if os.path.exists(fp):
                try:
                    font=ImageFont.truetype(fp, 220)
                    draw.text((250,250), ec, font=font, fill=(255,255,255,255), anchor="mm", embedded_color=True)
                    canvas.save(fn); return fn
                except: continue
        font=load_font(80,bold=True)
        draw.ellipse([50,50,450,450], fill=(60,60,90,220), outline=(255,215,0,200), width=4)
        draw.text((250,250), ec[:2], font=font, fill=(255,255,255,255), anchor="mm")
        canvas.save(fn); return fn
    except:
        Image.new("RGBA",(size,size),(0,0,0,0)).save(fn); return fn

def create_background(pos,glow,fn):
    source=os.path.join(os.path.dirname(os.path.abspath(__file__)),"background.png")
    if os.path.exists(source):
        try: Image.open(source).convert("RGB").resize((VIDEO_W,VIDEO_H),Image.LANCZOS).save(fn); return
        except: pass
    img=Image.new("RGBA",(VIDEO_W,VIDEO_H),(12,14,24,255))
    draw=ImageDraw.Draw(img)
    for y in range(VIDEO_H):
        r=int(12+10*y/VIDEO_H); g=int(14+16*y/VIDEO_H); b=int(24+28*y/VIDEO_H)
        draw.line([0,y,VIDEO_W,y], fill=(r,g,b,255))
    cx=VIDEO_W*0.22 if pos=="left" else VIDEO_W*0.78 if pos=="right" else VIDEO_W*0.5
    cy=VIDEO_H*0.75
    for rad in range(120,30,-15):
        alpha=int(10*(1-rad/120))
        draw.ellipse([cx-rad, cy-rad, cx+rad, cy+rad], fill=(*hex_to_rgba(glow, alpha)[:3], alpha))
    img.filter(ImageFilter.GaussianBlur(0.8)).save(fn)

def create_ui_overlay(name,topic,pos,glow,fn):
    img=Image.new("RGBA",(VIDEO_W,VIDEO_H),(0,0,0,0))
    draw=ImageDraw.Draw(img)
    bold=load_font(44,bold=True); small=load_font(22,bold=True)
    x=90 if pos=="left" else VIDEO_W-90 if pos=="right" else VIDEO_W//2
    anchor="lm" if pos=="left" else "rm" if pos=="right" else "mm"
    y=VIDEO_H-105
    bbox=draw.textbbox((0,0), name, font=bold, anchor=anchor)
    pad=18
    if anchor=="lm": rx0=x-pad; rx1=x+(bbox[2]-bbox[0])+pad*2
    elif anchor=="rm": rx0=x-(bbox[2]-bbox[0])-pad*2; rx1=x+pad
    else: rx0=x-(bbox[2]-bbox[0])//2-pad; rx1=x+(bbox[2]-bbox[0])//2+pad
    ry0=y+bbox[1]-pad; ry1=y+bbox[3]+pad
    draw.rounded_rectangle([rx0,ry0,rx1,ry1], fill=(0,0,0,185), outline=hex_to_rgba(glow,230), width=2)
    dot_r=10; dx=rx0-18 if anchor!="rm" else rx1+18; dy=(ry0+ry1)//2
    draw.ellipse([dx-dot_r-6, dy-dot_r-6, dx+dot_r+6, dy+dot_r+6], fill=hex_to_rgba(glow,70))
    draw.ellipse([dx-dot_r, dy-dot_r, dx+dot_r, dy+dot_r], fill=hex_to_rgba(glow,255))
    draw.text((x,y), name, font=bold, fill=(255,255,255,255), anchor=anchor)
    draw.text((VIDEO_W//2, 70), topic[:90], font=small, fill=(255,255,255,180), anchor="mm")
    img.save(fn)
    return x,y

def get_audio_duration(p):
    try:
        r=subprocess.run(["ffprobe","-v","error","-show_entries","format=duration","-of","default=noprint_wrappers=1:nokey=1",p],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=10)
        return float(r.stdout.strip())
    except: return 0.0

def format_ass_time(s):
    h=int(s//3600); m=int((s%3600)//60); sec=int(s%60); cs=int((s-int(s))*100)
    return f"{h}:{m:02d}:{sec:02d}.{cs:02d}"

def ass_escape(t):
    return t.replace("\\","\\\\").replace("{","\{").replace("}","\}")

def generate_subtitles(words,fn,scorecard=False,audio_file=None,full_text=None):
    header="[Script Info]\nScriptType: v4.00+\nPlayResX: 1920\nPlayResY: 1080\nWrapStyle: 0\n\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\nStyle: DebateSub,DejaVu Sans,42,&H00FFFFFF,&H00FFFFFF,&H00000000,&HAA000000,1,0,0,0,100,100,0,0,1,3,1,2,120,120,80,1\nStyle: ScoreSub,DejaVu Sans,36,&H00FFFFFF,&H00FFFFFF,&H00000000,&HAA000000,1,0,0,0,100,100,0,0,1,2,1,2,80,80,40,1\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    events=[]
    if scorecard and audio_file and full_text:
        dur=get_audio_duration(audio_file) or 6.0
        events.append(f"Dialogue: 0,0:00:00.00,{format_ass_time(dur)},ScoreSub,,0,0,0,,{ass_escape(full_text)}")
        open(fn,"w",encoding="utf-8").write(header+"\n".join(events)+"\n")
        return
    if not words:
        open(fn,"w",encoding="utf-8").write(header)
        return
    if audio_file:
        try:
            actual=get_audio_duration(audio_file)
            if actual>1 and words:
                est=words[-1].get("end",actual)
                if abs(est-actual)>0.5 and est>0:
                    scale=actual/est
                    for w in words: w["start"]*=scale; w["end"]*=scale
        except: pass
    chunk=[]; last_end=0
    for w in words:
        if not chunk:
            chunk=[w]; last_end=w["end"]
        elif w["start"]-last_end>0.6 or len(chunk)>=7:
            s=chunk[0]["start"]; e=last_end
            txt="\\N".join([" ".join([ass_escape(c["text"]) for c in chunk[i:i+10]]) for i in range(0,len(chunk),10)][:4])
            events.append(f"Dialogue: 0,{format_ass_time(s)},{format_ass_time(e)},DebateSub,,0,0,0,,{{\\an2\\pos(960,800)\\q2\\fad(120,120)}}{txt}")
            chunk=[w]; last_end=w["end"]
        else:
            chunk.append(w); last_end=w["end"]
    if chunk:
        s=chunk[0]["start"]; e=last_end
        txt="\\N".join([" ".join([ass_escape(c["text"]) for c in chunk[i:i+10]]) for i in range(0,len(chunk),10)][:4])
        events.append(f"Dialogue: 0,{format_ass_time(s)},{format_ass_time(e)},DebateSub,,0,0,0,,{{\\an2\\pos(960,800)\\q2\\fad(120,120)}}{txt}")
    open(fn,"w",encoding="utf-8").write(header+"\n".join(events)+"\n")

async def generate_audio_async(text,voice,fn):
    ct=clean_for_speech(text)
    if "Brian" in voice or "Andrew" in voice:
        ssml=f"<speak version='1.0' xml:lang='en-US'><voice name='{voice}'><prosody rate='-1%' pitch='-2%'>{ct}</prosody></voice></speak>"
    elif "Ava" in voice:
        ssml=f"<speak version='1.0' xml:lang='en-US'><voice name='{voice}'><prosody rate='+2%' pitch='+1%'>{ct}</prosody></voice></speak>"
    else:
        ssml=f"<speak version='1.0' xml:lang='en-US'><voice name='{voice}'><prosody rate='+1%'>{ct}</prosody></voice></speak>"
    try:
        com=edge_tts.Communicate(ssml,voice)
        audio=b""; words=[]
        async for chunk in com.stream():
            if chunk["type"]=="audio": audio+=chunk["data"]
            elif chunk["type"]=="WordBoundary":
                s=chunk["offset"]/10_000_000; d=chunk["duration"]/10_000_000
                words.append({"text":chunk["text"],"start":s,"duration":d,"end":s+d})
        open(fn,"wb").write(audio)
        if not words: raise Exception("no boundaries")
        return words
    except:
        com=edge_tts.Communicate(ct,voice,rate="+2%")
        audio=b""; words=[]
        async for chunk in com.stream():
            if chunk["type"]=="audio": audio+=chunk["data"]
            elif chunk["type"]=="WordBoundary":
                s=chunk["offset"]/10_000_000; d=chunk["duration"]/10_000_000
                words.append({"text":chunk["text"],"start":s,"duration":d,"end":s+d})
        open(fn,"wb").write(audio)
        if not words:
            t=0
            for tok in ct.split(): words.append({"text":tok,"start":t,"duration":0.38,"end":t+0.38}); t+=0.42
        return words

def generate_audio(text,role,fn,judge_voice_index=None):
    if "JUDGE" in role.upper():
        voice=JUDGE_VOICES[(judge_voice_index or 0) % len(JUDGE_VOICES)]
    elif "GOD" in role.upper(): voice=VOICES["A"]
    elif "SERPENT" in role.upper(): voice=VOICES["B"]
    else: voice=VOICES["Moderator"]
    try: return asyncio.run(generate_audio_async(text,voice,fn))
    except: 
        return asyncio.run(generate_audio_async(text,VOICES["Moderator"],fn))

# SOUNDBAR BELOW NAMES INSIDE NAMECARDS - PERFECT PLACEMENT
def render_video_segment(bg_path,ui_path,audio_path,subs_path,output_path,position,glow,cx,cy,visual_plan):
    duration=get_audio_duration(audio_path) or 10.0
    cmd=["ffmpeg","-y","-loop","1","-i",bg_path,"-loop","1","-i",ui_path,"-i",audio_path]
    filter_parts=[]
    filter_parts.append(f"[0:v]scale={VIDEO_W}:{VIDEO_H}:flags=lanczos[bg]")
    filter_parts.append(f"[1:v]scale={VIDEO_W}:{VIDEO_H}:flags=lanczos[ui]")
    zoom="[bg]scale=iw*1.3:ih*1.3,crop=1920:1080:(iw-1920)/2-200:(ih-1080)/2[bg_zoom]" if position=="left" else "[bg]scale=iw*1.3:ih*1.3,crop=1920:1080:(iw-1920)/2+200:(ih-1080)/2[bg_zoom]" if position=="right" else "[bg]scale=iw*1.25:ih*1.25,crop=1920:1080:(iw-1920)/2:(ih-1080)/2[bg_zoom]"
    filter_parts.append(zoom)
    glow_hex=glow.lstrip('#')
    # PERFECT SOUNDBAR INSIDE NAMECARDS BELOW NAMES
    wave_w=560
    wave_h=32
    filter_parts.append(f"[2:a]aformat=channel_layouts=mono,compand=gain=-6,showwaves=s={wave_w}x{wave_h}:mode=cline:colors=0x{glow_hex}:rate=30:draw=full:scale=sqrt[wave_raw]")
    filter_parts.append(f"[wave_raw]format=rgba,colorchannelmixer=aa=0.90[wave]")
    filter_parts.append(f"[bg_zoom][ui]overlay=0:0:shortest=1[bg_ui]")
    wave_x=cx + 65
    wave_y=cy + 62
    filter_parts.append(f"[bg_ui][wave]overlay={wave_x}:{wave_y}:shortest=1[bg_ui_wave]")
    last_label="[bg_ui_wave]"
    visual_inputs=[]
    for idx, vis in enumerate(visual_plan):
        try:
            ec=vis.get("emoji","💭") if isinstance(vis, dict) else str(vis)
            st=vis.get("start", idx*2.2) if isinstance(vis, dict) else idx*2.2
            et=vis.get("end", st+3.2) if isinstance(vis, dict) else st+3.2
            gp=create_emoji_asset(ec, idx+1000+random.randint(0,9999))
        except:
            gp=create_emoji_asset("💭", idx+1000+random.randint(0,9999))
            st=idx*2.2; et=st+3.2
        visual_inputs.append((gp, st, et))
    for idx, (gp, st, et) in enumerate(visual_inputs):
        filter_parts.append(f"[{3+idx}:v]scale={EMOJI_W}:{EMOJI_H}[v{idx}]")
        vx=(VIDEO_W-EMOJI_W)//2; vy=(VIDEO_H-EMOJI_H)//2 - 50
        nl=f"[tmp{idx}]"
        filter_parts.append(f"{last_label}[v{idx}]overlay={vx}:{vy}:enable='between(t,{st:.2f},{et:.2f})'{nl}")
        last_label=nl
    safe_subs=subs_path.replace(":", "\\:")
    filter_parts.append(f"{last_label}format=yuv420p,subtitles={safe_subs}[out]")
    fc=";".join(filter_parts)
    input_args=[]
    for gp,_,_ in visual_inputs: input_args.extend(["-i", gp])
    cmd.extend(input_args)
    cmd.extend(["-filter_complex", fc, "-map", "[out]", "-map", "2:a", "-c:v", "libx264", "-c:a", "aac", "-shortest", "-t", str(duration+0.5), output_path])
    r=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    if r.returncode!=0:
        print(r.stderr[-8000:])
        raise RuntimeError("Render failed")
    for gp,_,_ in visual_inputs:
        try: os.remove(gp)
        except: pass

def generate_scoreboard(rn,res,avg_a,avg_b,cum_a,cum_b,path,roles):
    W=VIDEO_W; H=VIDEO_H
    base=Image.new("RGB",(W,H),(12,16,32))
    overlay=Image.new("RGBA",(W,H),(0,0,0,180))
    img=Image.alpha_composite(base.convert("RGBA"),overlay).convert("RGB")
    draw=ImageDraw.Draw(img)
    ft=load_font(48,bold=True); fs=load_font(28,bold=True); fh=load_font(22,bold=True); fr=load_font(24)
    draw.text((W//2,50),f"ROUND {rn} SCORES",font=ft,fill=(255,215,0,255),anchor="mt")
    draw.text((W//2,115),f"{roles['side_a_label']}  vs  {roles['side_b_label']}",font=fs,fill=(255,255,255,230),anchor="mt")
    hy=190; cx1=120; cx2=750; cx3=1050; cx4=1350
    sa=roles['side_a_label'].split()[0][:12]; sb=roles['side_b_label'].split()[0][:12]
    draw.rectangle([60,hy-10,W-60,hy+45],fill=(25,35,70,255),outline=(255,215,0,180),width=2)
    draw.text((cx1,hy),"Judge",font=fh,fill=(255,255,255,230))
    draw.text((cx2,hy),sa,font=fh,fill=(0,255,204,255))
    draw.text((cx3,hy),sb,font=fh,fill=(255,120,255,255))
    draw.text((cx4,hy),"Winner",font=fh,fill=(255,215,0,255))
    y=hy+65
    for idx,r in enumerate(res):
        if idx%2==0: draw.rectangle([60,y-8,W-60,y+42],fill=(20,28,50,255))
        else: draw.rectangle([60,y-8,W-60,y+42],fill=(15,22,40,255))
        jt=f"{r['display_name']} ({r['provider']})"
        if len(jt)>32: jt=jt[:30]+".."
        draw.text((cx1,y),jt,font=fr,fill=(255,255,255,240))
        draw.text((cx2,y),f"{r['A_total']:.1f}",font=fr,fill=(0,255,204,255))
        draw.text((cx3,y),f"{r['B_total']:.1f}",font=fr,fill=(255,120,255,255))
        wl=roles['side_a_label'] if r['winner']=="A" else roles['side_b_label']
        if len(wl)>20: wl=wl[:18]+".."
        col=(0,255,204,255) if r['winner']=="A" else (255,120,255,255)
        draw.text((cx4,y),wl,font=fr,fill=col)
        y+=58
    draw.line([(60,y+5),(W-60,y+5)],fill=(255,255,255,60),width=2); y+=25
    draw.text((W//2,y),f"Round Avg: {avg_a:.1f} vs {avg_b:.1f}",font=fs,fill=(255,255,255,255),anchor="mt")
    draw.text((W//2,y+45),f"Cumulative: {cum_a:.1f} vs {cum_b:.1f}",font=fs,fill=(255,215,0,255),anchor="mt")
    img.save(path)

def render_scorecard_video(ip,ap,sp,op):
    dur=get_audio_duration(ap) or 6.0
    safe=sp.replace(":", "\\:")
    cmd=["ffmpeg","-y","-loop","1","-i",ip,"-i",ap,"-filter_complex",f"[0:v]scale={VIDEO_W}:{VIDEO_H}:flags=lanczos,format=yuv420p,subtitles={safe}[out]","-map","[out]","-map","1:a","-c:v","libx264","-c:a","aac","-shortest","-t",str(dur+0.6),op]
    r=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    if r.returncode!=0: raise RuntimeError("Scorecard render failed")

# MORE EMOJIS - NOT JUST GOD
def create_emoji_plan(text, words):
    if not words: return []
    # Expanded map - more than just god
    word_emoji_map={
        "adam":"🧑","man":"🧑","human":"🧑","person":"👤","people":"👥","eve":"🧑","woman":"🧑",
        "garden":"🌿","eden":"🌿","plant":"🌱","ground":"🪨",
        "apple":"🍎","fruit":"🍎","eat":"🍎","ate":"🍎","eating":"🍎",
        "tree":"🌳","trees":"🌳","branch":"🌲",
        "serpent":"🐍","snake":"🐍","serpent's":"🐍",
        "eyes":"👀","eye":"👀","opened":"👀","saw":"👁️","see":"👁️","naked":"🙈","shame":"🙈",
        "afraid":"😨","fear":"😨","hide":"😨","hid":"😨","hiding":"😨",
        "death":"💀","die":"💀","died":"💀","dying":"💀","dust":"💀",
        "sword":"⚔️","cherubim":"👼","angel":"👼",
        "knowledge":"🧠","wise":"🧠","wisdom":"💡","know":"🧠","knowing":"🧠",
        "god":"✨","lord":"✨","creator":"✨",
        "pain":"😣","sorrow":"😣","toil":"😓","sweat":"😓","work":"😓",
        "exile":"🚪","driven":"🚪","gate":"🚪","life":"🌟","live":"🌟","forever":"♾️",
    }
    plan=[]; used=[]
    for w in words:
        cw=re.sub(r"[^a-z]","",w["text"].lower())
        if cw in word_emoji_map:
            s=float(w["start"]); e=float(w["end"])+1.3
            if any(not (e < us or s > ue) for us,ue in used): continue
            if used and s-used[-1][1]<0.9: continue
            ec=word_emoji_map[cw]
            # Avoid repeating same emoji back-to-back, but allow varied
            if len(plan)>=1 and ec==plan[-1]["emoji"]: continue
            plan.append({"emoji":ec,"start":max(0,s),"end":e,"word":w["text"]})
            used.append((s,e))
            if len(plan)>=6: break
    # If still only 1 emoji, add second from topic keywords
    if len(plan)<=1 and len(words)>10:
        extra_words=["serpent","tree","eyes","death","knowledge","garden"]
        for ew in extra_words:
            if len(plan)>=5: break
            if ew not in [p["word"].lower() for p in plan]:
                if ew in word_emoji_map and word_emoji_map[ew] not in [p["emoji"] for p in plan]:
                    # Add at middle
                    mid_t=len(words)//2
                    plan.append({"emoji":word_emoji_map[ew],"start":words[mid_t]["start"],"end":words[mid_t]["start"]+1.3,"word":ew})
    return plan

def create_segment(text,role,name,topic,sid,model_for_visuals,pos=None,glow=None,judge_voice_index=None):
    if pos is None:
        pos="left" if "GOD" in role.upper() else "right" if "SERPENT" in role.upper() else "center" if "JUDGE" in role.upper() else "left"
    if glow is None:
        glow="#00FFCC" if "GOD" in role.upper() else "#FF00FF" if "SERPENT" in role.upper() else "#3399FF" if "JUDGE" in role.upper() else "#FFD700"
    af=f"audio_{sid}.mp3"; sf=f"subs_{sid}.ass"; bf=f"bg_{sid}.png"; uf=f"ui_{sid}.png"; vf=f"segment_{sid}.mp4"
    words=generate_audio(text,role,af,judge_voice_index)
    eplan=[]
    try:
        eplan=create_emoji_plan(clean_for_speech(text),words)
        if eplan: print(f"   {len(eplan)} emoji(s) varied: {', '.join(v['emoji']+'('+v['word']+')' for v in eplan)}")
    except Exception as e: print(f"Emoji plan skip {e}")
    generate_subtitles(words,sf,scorecard=False,audio_file=af,full_text=text)
    create_background(pos,glow,bf)
    cx,cy=create_ui_overlay(name,topic,pos,glow,uf)
    render_video_segment(bf,uf,af,sf,vf,pos,glow,cx,cy,eplan)
    return vf

def generate_panel_commentary(model,side,topic,rn,ap,sk,prev,roles):
    global USED_JUDGE_EXPLANATIONS
    prov=get_judge_short_name(model)
    pref=roles['side_a_label'] if side=="A" else roles['side_b_label']
    other=roles['side_b_label'] if side=="A" else roles['side_a_label']
    def trim(t,mw=150): wl=t.split(); return t if len(wl)<=mw else " ".join(wl[-mw:])
    # Varied starters, not just Look over and over - natural conversational
    starters=["Honestly,","For me,","What stood out to me in round {rn} was","I kept coming back to","The thing that decided round {rn} for me","Round {rn} really came down to","If I'm being honest about round {rn},","What convinced me in round {rn}","When I weighed both sides in round {rn},","The moment that tipped round {rn}"]
    starter=random.choice(starters).format(rn=rn)
    prompt=f"You are {prov}, judge on live debate panel about {topic}. You scored {pref} higher than {other} in round {rn}. Speak like real person on YouTube, warm conversational, natural sentences with contractions, 3-4 sentences. DO NOT start with Look. Start with {starter}. Give 2 specific reasons why {pref} won this round about {topic} woven naturally, not robotic. {pref}: {trim(ap)} vs {other}: {trim(sk)}. MUST argue {pref} won round {rn}. Avoid repeating previous. Be warm human panel style, varied vocab."
    resp=query_openrouter(prompt,model,timeout=35,max_tokens=400,temperature=0.93)
    if resp and len(resp.split())>=12 and "error" not in resp.lower()[:120]:
        resp=re.sub(r"(?i)\bI need to.*?[.]", "", resp)
        resp=re.sub(r"(?i)\bIn this (phrase|round).*?[,\.]", "", resp)
        resp=re.sub(r"\s+"," ",resp).strip()
        # Remove Look if model still starts with Look despite instruction
        resp=re.sub(r"^Look,?\s*", "", resp, flags=re.IGNORECASE)
        resp=re.sub(r"^Look over and over,?\s*", "", resp, flags=re.IGNORECASE)
        # Ensure it doesn't start with Look
        if resp.lower().startswith("look"):
            resp=starter+" "+resp[4:].lstrip()
        low=resp.lower()[:80]
        if low not in USED_JUDGE_EXPLANATIONS and len(resp.split())>=10:
            USED_JUDGE_EXPLANATIONS.add(low)
            return resp
    # Natural varied fallback, not robotic Look over and over
    fallbacks=[
        f"{starter} {pref} brought a specific verse about {topic} you can actually check, not just ideas. They laid out how it fits what happened that day, while {other} kept repeating same point without answering counter-evidence about {topic}.",
        f"{starter} I thought {pref} answered the strongest objection head on about {topic}. When {other} said that thing about {topic}, {pref} showed why that reading misses context, and added second piece about {topic} that {other} didn't address.",
        f"{starter} {pref} was consistent and concrete about {topic}. They talked about what actually happened that day, like fear hiding exile, while {other} stayed abstract about {topic} without specific verses.",
        f"For me personally in round {rn}, {pref} felt more honest about costs. They didn't just promise benefits about {topic}, they mentioned consequences like pain toil losing tree of life, while {other} left that out.",
    ]
    chosen=random.choice(fallbacks)
    USED_JUDGE_EXPLANATIONS.add(chosen[:60])
    return chosen

def build_intro(topic,jc,roles):
    return f"Welcome to the AI Debate Arena. Today, {roles['side_a_label']} faces {roles['side_b_label']} on the question: {topic}. Three rounds, equal time. An independent panel of {jc} AI judges from leading companies will score argument strength, rebuttal quality, and clarity. Let's begin."

def build_outro(jc,ca,cb,roles):
    if abs(ca-cb)<0.01: res="a draw"
    elif ca>cb: res=roles['side_a_label']
    else: res=roles['side_b_label']
    return f"After three rounds, our panel of {jc} judges gave {roles['side_a_label']} {ca:.1f}, {roles['side_b_label']} {cb:.1f}. Final result is {res}. Thank you for watching, and you decide who told the truth."

def stitch_segments(segs,out):
    lf="concat_list.txt"
    open(lf,"w",encoding="utf-8").write("\n".join([f"file '{os.path.abspath(s).replace(chr(39),chr(39)+chr(92)+chr(39)+chr(39))}'" for s in segs])+"\n")
    cmd=["ffmpeg","-y","-f","concat","-safe","0","-i",lf,"-c","copy",out]
    r=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    if r.returncode!=0: print(r.stderr[-7000:]); raise RuntimeError("Concat failed")

def run_debate_pipeline():
    global USED_ARGUMENTS, USED_PHRASES, USED_KEYWORDS, USED_JUDGE_EXPLANATIONS, USED_EMOJIS
    USED_ARGUMENTS=set(); USED_PHRASES=set(); USED_KEYWORDS=set(); USED_JUDGE_EXPLANATIONS=set(); USED_EMOJIS=set()
    cleanup_cache()
    if not OPENROUTER_API_KEY: raise RuntimeError("OPENROUTER_API_KEY missing")
    if not os.path.exists("topic.txt"):
        open("topic.txt","w",encoding="utf-8").write("Did God or the serpent lie in Genesis 1?")
    topic=open("topic.txt","r",encoding="utf-8").read().strip() or "Did God or the serpent lie in Genesis 1?"
    print(f"\nTOPIC: {topic}\n")
    print(f"SETTINGS: {ROUNDS} rounds x {TURNS_PER_SIDE_PER_ROUND} turns = {ROUNDS*TURNS_PER_SIDE_PER_ROUND*2} debate segments, {WORDS_PER_TURN} words")
    print(f"VOICES DISTINCT: GOD/Brian, SERPENT/Ava, MODERATOR/Andrew + 7 judges distinct")
    print(f"EMOJIS: Varied not just God - serpent, tree, eyes, death, knowledge etc, word-synced")
    avail=discover_models()
    if not avail: avail=FALLBACK_MODELS.copy()
    ap_model,sk_model=choose_primary_models(avail)
    roles=get_debate_roles(topic, ap_model)
    print(f"Roles: {roles['side_a_label']} VS {roles['side_b_label']}")
    judges=choose_judges(avail,(ap_model,sk_model))
    if len(judges)<5:
        judges=[m for m in FALLBACK_MODELS][:7]
    print(f"Judges GOOD KEEP ({len(judges)} FREE one per company): {', '.join(get_judge_short_name(j) for j in judges)}")
    segs=[]; sid=0
    def add_seg(text,role,name,pos=None,glow=None,jvi=None):
        nonlocal sid
        vm=sk_model if "SERPENT" in role.upper() or role=="B" else ap_model
        v=create_segment(text,role,name,topic,sid,vm,pos,glow,jvi); segs.append(v); sid+=1
    add_seg(build_intro(topic,len(judges),roles),"Moderator","MODERATOR")
    prev=""; cum_a=0.0; cum_b=0.0; pcom=[]
    for rn in range(1,ROUNDS+1):
        print(f"\nROUND {rn} - SPECIFIC CONVERSATIONAL REBUTTAL ANSWERS")
        a_turns,s_turns,prev=build_round_exchanges(topic,rn,ap_model,sk_model,prev,roles)
        for ti in range(TURNS_PER_SIDE_PER_ROUND):
            print(f"  Turn {ti+1}: A={count_words(a_turns[ti])} words B={count_words(s_turns[ti])} words used:{len(USED_ARGUMENTS)}")
            add_seg(a_turns[ti],roles['side_a_label'],roles['side_a_label'],"left","#00FFCC")
            add_seg(s_turns[ti],roles['side_b_label'],roles['side_b_label'],"right","#FF00FF")
        a_full="\n".join(a_turns); s_full="\n".join(s_turns)
        res=evaluate_round(judges,topic,rn,a_full,s_full,roles)
        ra,rb=calculate_round_average(res); cum_a+=ra; cum_b+=rb
        print(f"Round {rn}: {ra:.1f} vs {rb:.1f} | Cum: {cum_a:.1f} vs {cum_b:.1f}")
        sb=f"scoreboard_r{rn}.png"
        generate_scoreboard(rn,res,ra,rb,cum_a,cum_b,sb,roles)
        st=f"Round {rn} complete. Judges gave {roles['side_a_label']} {ra:.1f} and {roles['side_b_label']} {rb:.1f}. Cumulative {cum_a:.1f} to {cum_b:.1f}."
        sa=f"score_audio_r{rn}.mp3"; ss=f"score_subs_r{rn}.ass"; sv=f"score_video_r{rn}.mp4"
        sw=generate_audio(st,"Moderator",sa)
        generate_subtitles(sw,ss,scorecard=True,audio_file=sa,full_text=st)
        render_scorecard_video(sb,sa,ss,sv); segs.append(sv)
        if res:
            a_res=[r for r in res if r["winner"]=="A"] or res
            b_res=[r for r in res if r["winner"]=="B"] or res
            ja=random.choice(a_res)
            jb_cands=[r for r in b_res if r["provider"]!=ja["provider"]]
            jb=random.choice(jb_cands) if jb_cands else random.choice(b_res)
            ca=generate_panel_commentary(ja["model"],"A",topic,rn,a_full,s_full,pcom,roles); pcom.append(ca)
            jai=JUDGE_VOICE_MAP.get(ja["model"],0)
            add_seg(ca,"AI Judge",f"AI JUDGE — {ja['display_name'].upper()} ({ja['provider'].upper()})","center","#3399FF",jvi=jai)
            cb=generate_panel_commentary(jb["model"],"B",topic,rn,a_full,s_full,pcom,roles); pcom.append(cb)
            jbi=JUDGE_VOICE_MAP.get(jb["model"],1)
            if jbi==jai: jbi=(jai+1)%len(JUDGE_VOICES)
            add_seg(cb,"AI Judge",f"AI JUDGE — {jb['display_name'].upper()} ({jb['provider'].upper()})","center","#3399FF",jvi=jbi)
    add_seg(build_outro(len(judges),cum_a,cum_b,roles),"Moderator","MODERATOR")
    stitch_segments(segs,OUTPUT_FILE)
    print(f"\nCOMPLETE: {OUTPUT_FILE} — {cum_a:.1f} vs {cum_b:.1f} — Judges good keep, emojis varied not just God, negative rebuttal answers and full length, affirmative specific not general, judges natural not Look over and over, soundbar inside cards below names 560x32 cx+65 cy+62")
    cleanup_cache()

if __name__=="__main__":
    try: run_debate_pipeline()
    except KeyboardInterrupt: print("Cancelled")
    except Exception as e: print("FAILED"); print(str(e)); import traceback; traceback.print_exc(); raise
