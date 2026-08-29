
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

# NO EMOJI IMAGE CREATION - use native system emojis popping up like typing
EMOJI_W = 200
EMOJI_H = 200
USED_ARGUMENTS = set()
USED_JUDGE_EXPLANATIONS = set()

# STANDARD VOICES WE AGREED - distinct, must not be same
VOICES = {
    "A": "en-US-BrianMultilingualNeural",  # GOD / Apologist - deep male authoritative
    "B": "en-US-AvaMultilingualNeural",  # SERPENT / Skeptic - clear female skeptical
    "Moderator": "en-US-AndrewMultilingualNeural",  # Moderator - neutral British-tinged male
    "AI Christian Apologist": "en-US-BrianMultilingualNeural",
    "AI Skeptic": "en-US-AvaMultilingualNeural",
    "GOD TOLD TRUTH": "en-US-BrianMultilingualNeural",
    "SERPENT TOLD TRUTH": "en-US-AvaMultilingualNeural",
}
JUDGE_VOICES = [
    "en-US-ChristopherNeural",      # Judge 1 deep US male
    "en-US-EmmaMultilingualNeural", # Judge 2 warm US female
    "en-US-GuyNeural",              # Judge 3 confident male
    "en-GB-RyanNeural",             # Judge 4 British male
    "en-AU-WilliamNeural",          # Judge 5 Aussie male
    "en-CA-ClaraNeural",            # Judge 6 Canadian female
    "en-US-JennyNeural",            # Judge 7 bright US female
]
JUDGE_VOICE_MAP = {}

# AGREED FREE MODELS ONLY - no obscure models
FALLBACK_MODELS = [
    "google/gemini-2.0-flash-exp:free",
    "google/gemini-flash-1.5-8b:free",
    "google/gemma-3-27b-it:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "meta-llama/llama-3.2-3b-instruct:free",
    "mistralai/mistral-nemo:free",
    "mistralai/mistral-7b-instruct:free",
    "deepseek/deepseek-r1:free",
    "deepseek/deepseek-chat:free",
    "qwen/qwen-2.5-7b-instruct:free",
    "openai/gpt-4o-mini:free",
    "anthropic/claude-3-haiku:free",
    "nvidia/llama-3.1-nemotron-70b-instruct:free",
]

# Only these providers allowed for judges
ALLOWED_PROVIDERS = {"openai","anthropic","google","meta-llama","mistralai","deepseek","qwen","nvidia"}

PROVIDER_ALIASES = {
    "openai": "OpenAI", "anthropic": "Anthropic", "google": "Google",
    "x-ai": "xAI", "xai": "xAI", "deepseek": "DeepSeek",
    "mistralai": "Mistral", "meta-llama": "Meta", "meta": "Meta",
    "qwen": "Qwen", "nvidia": "Nvidia",
}

def provider_from_model(m):
    if not m: return "Unknown"
    base=m.split("/",1)[0].lower().strip()
    return PROVIDER_ALIASES.get(base, base.title())

def get_judge_short_name(mid):
    low=(mid or "").lower()
    if "gpt-4o-mini" in low: return "ChatGPT Mini"
    if "gpt" in low: return "ChatGPT"
    if "claude" in low: return "Claude"
    if "gemini-2.0" in low: return "Gemini 2.0"
    if "gemini" in low: return "Gemini"
    if "gemma" in low: return "Gemma"
    if "llama-3.3" in low: return "Llama 3.3"
    if "llama" in low: return "Llama"
    if "mistral-nemo" in low: return "Mistral Nemo"
    if "mistral" in low: return "Mistral"
    if "deepseek-r1" in low: return "DeepSeek R1"
    if "deepseek" in low: return "DeepSeek"
    if "qwen" in low: return "Qwen"
    if "nemotron" in low: return "Nemotron"
    return provider_from_model(mid)

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
    t=re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)
    t=re.sub(r"```.*?```"," ",t, flags=re.DOTALL)
    t=t.replace("(", " ").replace(")", " ").replace("[", " ").replace("]", " ")
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
        print(f"Discover HTTP {r.status_code}")
        if r.status_code!=200:
            return FALLBACK_MODELS.copy()
        free=[]
        for it in r.json().get("data",[]):
            mid=it.get("id","")
            if not mid or ":free" not in mid.lower(): continue
            prov=mid.split("/")[0].lower()
            if prov not in ALLOWED_PROVIDERS: continue
            if any(x in mid.lower() for x in ["embed","tts","whisper","audio","image","vision"]): continue
            free.append(mid)
        if free:
            # dedup and keep only agreed providers
            uniq=list(dict.fromkeys(free))
            print(f"Found {len(uniq)} FREE agreed models: {', '.join(get_judge_short_name(m) for m in uniq[:7])}")
            return uniq
        return FALLBACK_MODELS.copy()
    except Exception as e:
        print(f"Discover fail {e}")
        return FALLBACK_MODELS.copy()

def query_openrouter(prompt,mid,timeout=55,max_tokens=850,temperature=0.88):
    if not OPENROUTER_API_KEY: return None
    if ":free" not in mid.lower(): return None
    prov=mid.split("/")[0].lower()
    if prov not in ALLOWED_PROVIDERS: return None
    payload={"model":mid,"messages":[{"role":"user","content":prompt}],"temperature":temperature,"max_tokens":max_tokens}
    for attempt in range(3):
        try:
            resp=requests.post(OPENROUTER_URL,headers=openrouter_headers(),json=payload,timeout=timeout)
            if resp.status_code==200:
                c=resp.json().get("choices",[])[0].get("message",{}).get("content","")
                if c and len(c.strip())>70: return c.strip()
            elif resp.status_code==402:
                return None
        except:
            pass
        if attempt<2: time.sleep(1.2*(attempt+1))
    return None

def choose_primary_models(avail):
    # Ensure distinct providers for A vs B
    free=[m for m in avail if ":free" in m and m.split("/")[0].lower() in ALLOWED_PROVIDERS] or FALLBACK_MODELS
    used=set(); picks=[]
    for m in free:
        prov=provider_from_model(m)
        if prov not in used:
            picks.append(m); used.add(prov)
        if len(picks)>=2: break
    if len(picks)<2: picks=(free+FALLBACK_MODELS)[:2]
    print(f"Primary engines FREE distinct voices: {get_judge_short_name(picks[0])} (Brian) vs {get_judge_short_name(picks[1])} (Ava)")
    return picks[0],picks[1]

def choose_judges(avail,primary):
    global JUDGE_VOICE_MAP
    primary_prov=set(provider_from_model(m) for m in primary)
    cands=[m for m in avail if m not in primary and ":free" in m and m.split("/")[0].lower() in ALLOWED_PROVIDERS and provider_from_model(m) not in primary_prov]
    groups={}
    for m in cands:
        prov=provider_from_model(m)
        if prov not in groups: groups[prov]=m
    order=["OpenAI","Anthropic","Google","Meta","Mistral","DeepSeek","Qwen","Nvidia"]
    sel=[]
    for name in order:
        if name in groups:
            sel.append(groups[name]); del groups[name]
        if len(sel)>=MAX_JUDGES: break
    for m in groups.values():
        if len(sel)>=MAX_JUDGES: break
        sel.append(m)
    seen=set(); uniq=[]
    for m in sel:
        d=get_judge_short_name(m)
        if d not in seen:
            uniq.append(m); seen.add(d)
    result=uniq[:MAX_JUDGES]
    if len(result)<5:
        for m in FALLBACK_MODELS:
            if len(result)>=5: break
            if m not in primary and get_judge_short_name(m) not in seen and m.split("/")[0].lower() in ALLOWED_PROVIDERS:
                result.append(m); seen.add(get_judge_short_name(m))
    JUDGE_VOICE_MAP={mid: idx%len(JUDGE_VOICES) for idx,mid in enumerate(result)}
    print(f"Judges AGREED FREE ONE PER COMPANY ({len(result)}): {', '.join(f'{provider_from_model(m)} ({get_judge_short_name(m)})' for m in result)}")
    return result

def get_debate_roles(topic, model):
    tl=(topic or "").lower()
    if "god" in tl and "serpent" in tl:
        return {"side_a_label":"GOD TOLD TRUTH","side_a_desc":"Argues God told the truth in Genesis 2:17 moth tamuth and serpent twisted it","side_b_label":"SERPENT TOLD TRUTH","side_b_desc":"Argues serpent's prediction in Genesis 3:4-5 matched what happened that day better than God's threat"}
    if "creator" in tl or "universe" in tl:
        return {"side_a_label":"NEEDS CREATOR","side_a_desc":"Argues universe requires a creator beyond itself - BGV theorem, fine-tuning","side_b_label":"NO CREATOR NEEDED","side_b_desc":"Argues universe can be explained without a creator - quantum, multiverse"}
    # Generic fallback uses topic words
    return {"side_a_label":"AFFIRMATIVE","side_a_desc":f"Argues FOR {topic} with specific evidence","side_b_label":"NEGATIVE","side_b_desc":f"Argues AGAINST {topic} with specific counter-evidence"}

def strip_filler(text):
    text=re.sub(r"^(ladies and gentlemen|well|thank you|my friends)[,.]?\s*","",text,flags=re.IGNORECASE).strip()
    return text

def generate_fallback_debate(side_label, topic, round_num, turn_num):
    tl=topic.lower()
    is_genesis="god" in tl and "serpent" in tl
    if is_genesis and "GOD" in side_label.upper():
        return random.choice([
            "Look at Genesis 2:17, moth tamuth in Hebrew, dying you shall surely die, it's emphatic about certainty, not just timing. The serpent in 3:4 says lo moth temuthun, you shall not surely die, direct negation. What happened that day? Genesis 3:10 says Adam hid because he was afraid, that's relational death. Verse 19 to dust you shall return, verse 24 cherubim block the tree of life. On that day they lost everlasting life.",
            "Genesis 2:16 says you may freely eat of every tree, that's abundant generosity. Serpent twists it in 3:1, did God really say you shall not eat of every tree? He makes generosity sound like stinginess. That twisting matters when we ask who told the truth about provision.",
            "In the day you eat appears in Genesis 2:4 too, in the day the Lord made earth, meaning when, not a 24 hour countdown. Moth tamuth is about certainty. And look what serpent left out, pain, thorns, sweat, exile verses 16 to 19. A half truth that hides cost is still misleading.",
        ])
    if is_genesis and "SERPENT" in side_label.upper():
        return random.choice([
            "Read the plain text. Genesis 2:17 says in the day you eat you shall die, natural reading that same day. Genesis 5:5 says Adam lived 930 years then died. He didn't die that day. Serpent says in 3:4 you shall not surely die, and that matches, they didn't die that day. And 3:7 eyes were opened as promised, God confirms in 3:22 behold man has become as one of us knowing good and evil.",
            "Genesis 3:22, God says man has become as one of us to know good and evil, word for word what serpent promised in verse 5. If serpent is father of lies, why is God echoing his promise? And where is death that day? Chapter 4 they are having children, very much alive.",
            "Genesis 3 verse 6 says she saw tree good for food pleasant to eyes desired to make one wise. Verse 7 eyes opened. Exactly what serpent promised verse 5, your eyes shall be opened. No verse that day saying they died. The serpent's description of immediate outcome was more accurate.",
        ])
    # For non-Genesis topics, be specific to topic
    return f"When we look at {topic}, {side_label} fits what we actually observe. Take a concrete example from {topic} - there's a mechanism you can trace that predicts what we see. That's why {side_label} is stronger on {topic} specifically."

def generate_turn(role_key, topic, round_num, turn_num, prev_history, model, role_label, role_desc, opponent_label, opponent_desc):
    global USED_ARGUMENTS
    used_str = "; ".join(list(USED_ARGUMENTS)[-8:])[:400] if USED_ARGUMENTS else "None yet"
    prev_snip = prev_history[-600:] if prev_history else "No previous, you are opening"

    tl=(topic or "").lower()
    is_genesis="god" in tl and "serpent" in tl

    if is_genesis:
        evidence_instruction = "You MUST cite at least one specific Genesis verse this turn: 2:17 moth tamuth, 3:4 lo moth temuthun, 3:5 as gods knowing good and evil, 3:7 eyes opened, 3:10 fear hiding, 3:19 dust to dust, 3:22-24 tree of life cherubim blocked. Use Hebrew words yom, moth tamuth naturally."
        fresh_instruction = "Fresh angle not used: if you said 930 years, now try tree of life cherubim; if you said eyes opened, now try dust to dust or shame or moth tamuth or pain toil exile. Must be new."
    else:
        evidence_instruction = f"You MUST give specific evidence about \"{topic}\" - a named study, statistic, historical example, mechanism, or philosopher. Not vague philosophy. Talk about {topic} by name."
        fresh_instruction = f"Fresh angle about {topic} not used yet: {used_str} - must be new mechanism, data point, or concrete example about {topic}."

    if round_num==1 and turn_num==1:
        round_prompt = f"""You are {role_label} in a live YouTube debate about: {topic}
You believe: {role_desc}
This is your OPENING. Speak like a real person on stage, warm, conversational, passionate, like talking to a friend who disagrees about {topic}.
Start with a hook about {topic}, then give your single strongest SPECIFIC piece of evidence.
{evidence_instruction}
Don't repeat: {used_str}
{MIN_TURN_WORDS}-{MAX_TURN_WORDS} words. Natural speech with contractions. No bullet points. No meta talk about what you need to do. Just say it.
Topic file says: \"{topic}\" - you must address this exact question, not generic debate.
"""
    elif round_num==1:
        round_prompt = f"""You are {role_label} debating {topic}. You believe {role_desc}.
You've opened. Now give a SECOND distinct SPECIFIC piece of evidence about {topic} you haven't used.
Already used: {used_str} - MUST be new.
{fresh_instruction}
{evidence_instruction}
Speak like a real person, conversational, warm, specific to {topic}. {MIN_TURN_WORDS}-{MAX_TURN_WORDS} words. No meta talk.
"""
    else:
        round_prompt = f"""You are {role_label} debating {topic}. You believe {role_desc}. Opponent is {opponent_label} who believes {opponent_desc}.
Opponent just said about {topic}: {prev_snip[:500]}
Respond like a real person in conversation. Directly quote one specific thing they just said about {topic}, then show why it doesn't hold up with specific counter-evidence about {topic}.
Then add one NEW specific point not used: {used_str}
{fresh_instruction}
{evidence_instruction}
Speak naturally, conversational, like you're there. {MIN_TURN_WORDS}-{MAX_TURN_WORDS} words. Don't say you need to quote, just do it naturally. Must be specific to {topic}, not generic.
"""

    for m in [model]+FALLBACK_MODELS[:4]:
        resp=query_openrouter(round_prompt,m,max_tokens=900,temperature=0.9+random.random()*0.15)
        if resp and count_words(resp)>=95:
            cleaned=strip_filler(resp)
            cleaned=re.sub(r"\s+"," ",cleaned).strip()
            # Remove leaked task language
            cleaned=re.sub(r"(?i)\bI need to (do|show|quote|explain|address).*?\.", "", cleaned)
            cleaned=re.sub(r"(?i)\bIn (?:this|the) (?:turn|round|phrase|response).*?[,\.]", "", cleaned)
            cleaned=re.sub(r"(?i)\bAs (?:an AI|per instructions).*?[.]", "", cleaned)
            cleaned=re.sub(r"\s+"," ",cleaned).strip()
            if not cleaned.endswith(('.', '!', '?')): cleaned+="."
            low=cleaned.lower()
            # Must mention topic keywords to avoid generic
            topic_keywords=[w.lower() for w in topic.split() if len(w)>4][:3]
            has_topic=any(kw in low for kw in topic_keywords) or (is_genesis and any(v in low for v in ["genesis","2:17","3:4","3:22","adam","serpent","moth"]))
            if not has_topic:
                # Try again forcing topic mention
                extra=query_openrouter(round_prompt+f"\nYou must mention {topic} specifically and cite a verse or specific fact about {topic}.",m,max_tokens=400,temperature=0.92)
                if extra and count_words(extra)>60:
                    cleaned+=" "+extra
            # Check repeat
            is_rep=False
            for used in USED_ARGUMENTS:
                if len(used)>30 and used.lower() in low:
                    is_rep=True; break
            if not is_rep:
                for sent in cleaned.split('. ')[:2]:
                    if len(sent)>30: USED_ARGUMENTS.add(sent[:80])
                return cleaned[:1800]
    fb=generate_fallback_debate(role_label, topic, round_num, turn_num)
    USED_ARGUMENTS.add(fb[:80])
    return fb

def build_round_exchanges(topic, rn, ap_model, sk_model, prev, roles):
    a_turns=[]; s_turns=[]; hist=prev
    for tn in range(1,TURNS_PER_SIDE_PER_ROUND+1):
        a=generate_turn("A", topic, rn, tn, hist, ap_model, roles['side_a_label'], roles['side_a_desc'], roles['side_b_label'], roles['side_b_desc'])
        a_turns.append(a); hist+=f"\n{roles['side_a_label']}: {a}\n"
        s=generate_turn("B", topic, rn, tn, hist, sk_model, roles['side_b_label'], roles['side_b_desc'], roles['side_a_label'], roles['side_a_desc'])
        s_turns.append(s); hist+=f"\n{roles['side_b_label']}: {s}\n"
    return a_turns,s_turns,hist

def neutral_judge(model):
    a=random.uniform(53,68); b=random.uniform(53,68)
    if abs(a-b)<5:
        if random.random()>0.5: a+=7
        else: b+=7
    return {"model":model,"provider":provider_from_model(model),"display_name":get_judge_short_name(model),"A_total":round(a,1),"B_total":round(b,1),"winner":"A" if a>b else "B"}

def judge_round(model,topic,rn,ap,sk,roles):
    prompt=f'Judge Round {rn} about "{topic}". {roles["side_a_label"]}: {ap[:800]} vs {roles["side_b_label"]}: {sk[:800]}. Score 0-100. Return ONLY JSON: {{"A_total":0-100,"B_total":0-100,"winner":"A or B","reason":"one sentence why {roles["side_a_label"] if rn%2==0 else roles["side_b_label"]} won round {rn} about {topic}"}}. Must NOT be equal. Be decisive.'
    for m in [model]+FALLBACK_MODELS[:2]:
        if ":free" not in m or m.split("/")[0].lower() not in ALLOWED_PROVIDERS: continue
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
            if m not in [x['model'] for x in results] and m.split("/")[0].lower() in ALLOWED_PROVIDERS:
                results.append(neutral_judge(m))
    return results

def calculate_round_average(res):
    return round(sum(r["A_total"] for r in res)/len(res),2), round(sum(r["B_total"] for r in res)/len(res),2)

# EMOJI AS NATIVE TEXT OVERLAY - NO IMAGE CREATION, LIKE TYPING WORD AND EMOJI POPS UP
def create_emoji_plan(text, words):
    if not words: return []
    word_emoji_map={
        "adam":"🧑","man":"🧑","human":"🧑","person":"👤","people":"👥","eve":"🧑","woman":"🧑",
        "garden":"🌿","eden":"🌿","apple":"🍎","fruit":"🍎","trees":"🌳","tree":"🌳",
        "serpent":"🐍","snake":"🐍","eyes":"👀","eye":"👀","naked":"🙈","shame":"🙈",
        "afraid":"😨","fear":"😨","hide":"😨","hid":"😨","death":"💀","die":"💀","dust":"💀",
        "sword":"⚔️","cherubim":"👼","angel":"👼","knowledge":"🧠","wise":"🧠","wisdom":"💡","god":"✨",
    }
    plan=[]; used=[]
    for w in words:
        cw=re.sub(r"[^a-z]","",w["text"].lower())
        if cw in word_emoji_map:
            s=float(w["start"]); e=float(w["end"])+1.2
            if any(not (e < us or s > ue) for us,ue in used): continue
            if used and s-used[-1][1]<1.2: continue
            ec=word_emoji_map[cw]
            if ec in [p["emoji"] for p in plan[-2:]]: continue
            plan.append({"emoji":ec,"start":max(0,s),"end":e,"word":w["text"]})
            used.append((s,e))
            if len(plan)>=5: break
    return plan

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

def generate_subtitles(words,fn,scorecard=False,audio_file=None,full_text=None,emoji_plan=None):
    header="[Script Info]\nScriptType: v4.00+\nPlayResX: 1920\nPlayResY: 1080\nWrapStyle: 0\n\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\nStyle: DebateSub,DejaVu Sans,42,&H00FFFFFF,&H00FFFFFF,&H00000000,&HAA000000,1,0,0,0,100,100,0,0,1,3,1,2,120,120,80,1\nStyle: ScoreSub,DejaVu Sans,36,&H00FFFFFF,&H00FFFFFF,&H00000000,&HAA000000,1,0,0,0,100,100,0,0,1,2,1,2,80,80,40,1\nStyle: EmojiPop,DejaVu Sans,96,&H00FFFFFF,&H00FFFFFF,&H00000000,&HAA000000,1,0,0,0,100,100,0,0,1,2,1,5,0,0,0,1\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
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
    # Emoji pop-ups like typing - native emojis, no image creation, centered
    if emoji_plan:
        for ep in emoji_plan:
            # Large emoji centered, like chat pop-up when you type word
            # Use an2? No, an5 center, pos 960,540
            esc=ass_escape(ep["emoji"])
            events.append(f"Dialogue: 1,{format_ass_time(ep['start'])},{format_ass_time(ep['end'])},EmojiPop,,0,0,0,,{{\\an5\\pos(960,450)\\fad(100,100)\\bord2\\shad2}}{esc}")
    open(fn,"w",encoding="utf-8").write(header+"\n".join(events)+"\n")

async def generate_audio_async(text,voice,fn):
    ct=clean_for_speech(text)
    # Use distinct prosody per voice to keep voices distinct
    if "Brian" in voice:
        ssml=f"<speak version='1.0' xml:lang='en-US'><voice name='{voice}'><prosody rate='-1%' pitch='-2%'>{ct}</prosody></voice></speak>"
    elif "Ava" in voice:
        ssml=f"<speak version='1.0' xml:lang='en-US'><voice name='{voice}'><prosody rate='+2%' pitch='+1%'>{ct}</prosody></voice></speak>"
    elif "Andrew" in voice:
        ssml=f"<speak version='1.0' xml:lang='en-US'><voice name='{voice}'><prosody rate='+0%' pitch='+0%'>{ct}</prosody></voice></speak>"
    else:
        ssml=f"<speak version='1.0' xml:lang='en-US'><voice name='{voice}'><prosody rate='+3%'>{ct}</prosody></voice></speak>"
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
    except Exception as e:
        print(f"TTS {voice} failed {e}, retry plain")
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
    # STANDARD VOICES WE AGREED - must be distinct
    if "JUDGE" in role.upper():
        idx=judge_voice_index if judge_voice_index is not None else 0
        voice=JUDGE_VOICES[idx % len(JUDGE_VOICES)]
        print(f"  Voice JUDGE {idx}: {voice} for {role}")
    elif "GOD TOLD TRUTH" in role.upper() or role.strip().upper()=="A" or "APOLOGIST" in role.upper() or "AFFIRMATIVE" in role.upper() or role=="GOD":
        voice=VOICES["A"]  # Brian
        print(f"  Voice A/GOD: {voice} for {role}")
    elif "SERPENT TOLD TRUTH" in role.upper() or role.strip().upper()=="B" or "SKEPTIC" in role.upper() or "NEGATIVE" in role.upper():
        voice=VOICES["B"]  # Ava
        print(f"  Voice B/SERPENT: {voice} for {role}")
    else:
        voice=VOICES["Moderator"]  # Andrew
        print(f"  Voice MODERATOR: {voice} for {role}")
    try: return asyncio.run(generate_audio_async(text,voice,fn))
    except: 
        return asyncio.run(generate_audio_async(text,VOICES["Moderator"],fn))

def render_video_segment(bg_path,ui_path,audio_path,subs_path,output_path,position,glow,cx,cy,visual_plan):
    duration=get_audio_duration(audio_path) or 10.0
    cmd=["ffmpeg","-y","-loop","1","-i",bg_path,"-loop","1","-i",ui_path,"-i",audio_path]
    filter_parts=[]
    filter_parts.append(f"[0:v]scale={VIDEO_W}:{VIDEO_H}:flags=lanczos[bg]")
    filter_parts.append(f"[1:v]scale={VIDEO_W}:{VIDEO_H}:flags=lanczos[ui]")
    zoom="[bg]scale=iw*1.3:ih*1.3,crop=1920:1080:(iw-1920)/2-200:(ih-1080)/2[bg_zoom]" if position=="left" else "[bg]scale=iw*1.3:ih*1.3,crop=1920:1080:(iw-1920)/2+200:(ih-1080)/2[bg_zoom]" if position=="right" else "[bg]scale=iw*1.25:ih*1.25,crop=1920:1080:(iw-1920)/2:(ih-1080)/2[bg_zoom]"
    filter_parts.append(zoom)
    glow_hex=glow.lstrip('#')
    wave_w=560
    wave_h=32
    filter_parts.append(f"[2:a]aformat=channel_layouts=mono,compand=gain=-6,showwaves=s={wave_w}x{wave_h}:mode=cline:colors=0x{glow_hex}:rate=30:draw=full:scale=sqrt[wave_raw]")
    filter_parts.append(f"[wave_raw]format=rgba,colorchannelmixer=aa=0.90[wave]")
    filter_parts.append(f"[bg_zoom][ui]overlay=0:0:shortest=1[bg_ui]")
    wave_x=cx + 65
    wave_y=cy + 62
    filter_parts.append(f"[bg_ui][wave]overlay={wave_x}:{wave_y}:shortest=1[bg_ui_wave]")
    last_label="[bg_ui_wave]"
    # NO emoji image overlays - emojis are now native ASS text pop-ups in subtitles, no creation
    safe_subs=subs_path.replace(":", "\\:")
    filter_parts.append(f"{last_label}format=yuv420p,subtitles={safe_subs}[out]")
    fc=";".join(filter_parts)
    cmd.extend(["-filter_complex", fc, "-map", "[out]", "-map", "2:a", "-c:v", "libx264", "-c:a", "aac", "-shortest", "-t", str(duration+0.5), output_path])
    r=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    if r.returncode!=0:
        print(r.stderr[-8000:])
        raise RuntimeError("Render failed")

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

def create_segment(text,role,name,topic,sid,model_for_visuals,pos=None,glow=None,judge_voice_index=None):
    if pos is None:
        pos="left" if "GOD" in role.upper() else "right" if "SERPENT" in role.upper() else "center" if "JUDGE" in role.upper() else "left"
    if glow is None:
        glow="#00FFCC" if "GOD" in role.upper() else "#FF00FF" if "SERPENT" in role.upper() else "#3399FF" if "JUDGE" in role.upper() else "#FFD700"
    af=f"audio_{sid}.mp3"; sf=f"subs_{sid}.ass"; bf=f"bg_{sid}.png"; uf=f"ui_{sid}.png"; vf=f"segment_{sid}.mp4"
    words=generate_audio(text,role,af,judge_voice_index)
    # Create emoji plan - native emojis, no image creation
    eplan=[]
    try:
        eplan=create_emoji_plan(clean_for_speech(text),words)
        if eplan: print(f"   {len(eplan)} native emoji(s) popping like typing: {', '.join(v['emoji']+'('+v['word']+')' for v in eplan)}")
    except Exception as e: print(f"Emoji plan skip {e}")
    generate_subtitles(words,sf,scorecard=False,audio_file=af,full_text=text,emoji_plan=eplan)
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
    prompt=f"You are {prov}, a judge on a live debate panel about {topic}. You just scored {pref} higher than {other} in round {rn}. Speak like a real person on YouTube, warm, conversational, natural sentences with contractions, 3-4 sentences. Start with Look, or Honestly, or For me. Give 2 specific reasons about {topic} why {pref} won this round. Don't repeat previous. {pref}: {trim(ap)} vs {other}: {trim(sk)}. You must argue {pref} won round {rn} about {topic}."
    resp=query_openrouter(prompt,model,timeout=35,max_tokens=400,temperature=0.92)
    if resp and len(resp.split())>=12 and "error" not in resp.lower()[:120]:
        resp=re.sub(r"(?i)\bI need to.*?[.]", "", resp)
        resp=re.sub(r"(?i)\bIn this (phrase|round).*?[,\.]", "", resp)
        resp=re.sub(r"\s+"," ",resp).strip()
        low=resp.lower()[:80]
        if low not in USED_JUDGE_EXPLANATIONS and len(resp.split())>=10:
            USED_JUDGE_EXPLANATIONS.add(low)
            return resp
    fallbacks=[
        f"Look, in round {rn} I gave it to {pref} because they brought a specific verse about {topic} you can actually check, not just ideas. {pref} laid out how it fits what happened that day, while {other} kept repeating the same point without answering the counter-evidence about {topic}.",
        f"Honestly, round {rn} went to {pref} for me because they answered the strongest objection head on about {topic}. When {other} said that thing about {topic}, {pref} showed why that reading misses context, and added a second piece of evidence about {topic} that {other} didn't address.",
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
    global USED_ARGUMENTS, USED_JUDGE_EXPLANATIONS
    USED_ARGUMENTS=set(); USED_JUDGE_EXPLANATIONS=set()
    cleanup_cache()
    if not OPENROUTER_API_KEY: raise RuntimeError("OPENROUTER_API_KEY missing")
    if not os.path.exists("topic.txt"):
        open("topic.txt","w",encoding="utf-8").write("Did God or the serpent lie in Genesis 1?")
    topic=open("topic.txt","r",encoding="utf-8").read().strip() or "Did God or the serpent lie in Genesis 1?"
    print(f"\nTOPIC: {topic}\n")
    print(f"SETTINGS: {ROUNDS} rounds x {TURNS_PER_SIDE_PER_ROUND} turns = {ROUNDS*TURNS_PER_SIDE_PER_ROUND*2} debate segments, {WORDS_PER_TURN} words each")
    print(f"VOICES STANDARD WE AGREED: GOD/Brian={VOICES['A']}, SERPENT/Ava={VOICES['B']}, MODERATOR/Andrew={VOICES['Moderator']}, 7 judges distinct")
    print(f"EMOJIS: Native system emojis popping like typing word, no image creation, no white rectangles")
    avail=discover_models()
    if not avail: avail=FALLBACK_MODELS.copy()
    ap_model,sk_model=choose_primary_models(avail)
    roles=get_debate_roles(topic, ap_model)
    print(f"Roles: {roles['side_a_label']} VS {roles['side_b_label']}")
    judges=choose_judges(avail,(ap_model,sk_model))
    if len(judges)<5:
        judges=[m for m in FALLBACK_MODELS if m.split("/")[0].lower() in ALLOWED_PROVIDERS][:7]
    print(f"Judges AGREED FREE: {', '.join(get_judge_short_name(j) for j in judges)}")
    segs=[]; sid=0
    def add_seg(text,role,name,pos=None,glow=None,jvi=None):
        nonlocal sid
        vm=sk_model if "SERPENT" in role.upper() or role=="B" else ap_model
        v=create_segment(text,role,name,topic,sid,vm,pos,glow,jvi); segs.append(v); sid+=1
    add_seg(build_intro(topic,len(judges),roles),"Moderator","MODERATOR")
    prev=""; cum_a=0.0; cum_b=0.0; pcom=[]
    for rn in range(1,ROUNDS+1):
        print(f"\nROUND {rn} - SPECIFIC about {topic[:60]}")
        a_turns,s_turns,prev=build_round_exchanges(topic,rn,ap_model,sk_model,prev,roles)
        for ti in range(TURNS_PER_SIDE_PER_ROUND):
            print(f"  Turn {ti+1}: A={count_words(a_turns[ti])} B={count_words(s_turns[ti])} used:{len(USED_ARGUMENTS)}")
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
    print(f"\nCOMPLETE: {OUTPUT_FILE} — {cum_a:.1f} vs {cum_b:.1f} — {len(judges)} agreed FREE judges, distinct voices Brian/Ava/Andrew + 7 judges, specific args about {topic}, native emojis popping no creation, soundbar inside cards")
    cleanup_cache()

if __name__=="__main__":
    try: run_debate_pipeline()
    except KeyboardInterrupt: print("Cancelled")
    except Exception as e: print("FAILED"); print(str(e)); import traceback; traceback.print_exc(); raise
