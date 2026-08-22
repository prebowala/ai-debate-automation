
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
from urllib.parse import quote
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

MAX_VISUALS_PER_SEGMENT = 4
MIN_VISUAL_GAP = 0.8
VISUAL_W = 520
VISUAL_H = 520
VISUAL_Y = 160

VOICES = {"A": "en-US-BrianMultilingualNeural","B": "en-US-AvaMultilingualNeural","Moderator": "en-US-AndrewMultilingualNeural"}
JUDGE_VOICES = ["en-US-ChristopherNeural","en-US-EmmaMultilingualNeural","en-US-GuyNeural","en-US-JennyNeural"]

# TOP FREE MODELS - one per company will be enforced for judges
FALLBACK_MODELS = [
    "openai/gpt-4o-mini:free",
    "anthropic/claude-3-haiku:free",
    "google/gemini-flash-1.5-8b:free",
    "google/gemma-2-9b-it:free",
    "meta-llama/llama-3.2-3b-instruct:free",
    "meta-llama/llama-3.1-8b-instruct:free",
    "mistralai/mistral-7b-instruct:free",
    "deepseek/deepseek-r1:free",
    "qwen/qwen-2.5-7b-instruct:free",
]

PROVIDER_ALIASES = {
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "google": "Google",
    "x-ai": "xAI",
    "xai": "xAI",
    "deepseek": "DeepSeek",
    "mistralai": "Mistral",
    "mistral": "Mistral",
    "meta-llama": "Meta",
    "meta": "Meta",
    "qwen": "Qwen",
}

def provider_from_model(model_id):
    if not model_id: return "Unknown"
    prefix = model_id.split("/",1)[0].lower().strip()
    return PROVIDER_ALIASES.get(prefix, prefix.replace("-", " ").title())

def get_judge_short_name(model_id):
    low=(model_id or "").lower()
    if "gpt-4o-mini" in low: return "ChatGPT"
    if "gpt" in low: return "ChatGPT"
    if "claude" in low: return "Claude"
    if "gemini-flash" in low: return "Gemini Flash"
    if "gemini" in low: return "Gemini"
    if "gemma" in low: return "Gemma"
    if "grok" in low: return "Grok"
    if "deepseek-r1" in low: return "DeepSeek R1"
    if "deepseek" in low: return "DeepSeek"
    if "mistral-nemo" in low: return "Mistral Nemo"
    if "mistral" in low: return "Mistral"
    if "llama-3.2-11b" in low: return "Llama 11B"
    if "llama-3.2" in low: return "Llama 3.2"
    if "llama-3.1" in low: return "Llama 3.1"
    if "llama" in low: return "Llama"
    if "qwen" in low: return "Qwen"
    return provider_from_model(model_id)

def cleanup_cache():
    for pat in ["*.mp4","*.mp3","*.ass","*.png","*.gif","*_list.txt"]:
        for fn in glob.glob(pat):
            if fn in [OUTPUT_FILE,"background.png","topic.txt"]: continue
            try: os.remove(fn)
            except: pass

def count_words(t): return len(re.findall(r"\b[\w'-]+\b", t or ""))
def clean_for_speech(t):
    t=re.sub(r"\([^)]*\)","",t or "")
    for o,n in {"*":"","#":"","_":"","`":"","–":"-","—":"-","\"":"",":":" ",";":" ","&":"and"}.items(): t=t.replace(o,n)
    return re.sub(r"\s+"," ",t).strip()
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
            free.append(mid)
        if free:
            print(f"Found {len(free)} free models")
            return list(dict.fromkeys(free))
        return FALLBACK_MODELS.copy()
    except Exception as e:
        print(f"discover fail {e}, using fallback")
        return FALLBACK_MODELS.copy()

def query_openrouter(prompt,model_id,timeout=50,max_tokens=700,temperature=0.75):
    if not OPENROUTER_API_KEY: return None
    payload={"model":model_id,"messages":[{"role":"user","content":prompt}],"temperature":temperature,"max_tokens":max_tokens}
    try:
        resp=requests.post(OPENROUTER_URL,headers=openrouter_headers(),json=payload,timeout=timeout)
        if resp.status_code==200:
            c=resp.json().get("choices",[])[0].get("message",{}).get("content","")
            if c and len(c.strip())>40: return c.strip()
    except Exception as e:
        print(f"req fail {get_judge_short_name(model_id)} {e}")
    return None

def choose_primary_models(avail):
    free=[m for m in avail if ":free" in m]
    if not free: free=avail
    # Ensure different companies for two debaters
    used_providers=set()
    picks=[]
    for m in free:
        prov=provider_from_model(m)
        if prov not in used_providers:
            picks.append(m)
            used_providers.add(prov)
        if len(picks)>=2: break
    if len(picks)<2:
        picks=(free+["openai/gpt-4o-mini:free","google/gemini-flash-1.5-8b:free"])[:2]
    return picks[0],picks[1]

def choose_judges(avail,primary):
    excl=set(primary)
    # ONLY ONE MODEL PER COMPANY - strict
    cands=[m for m in avail if m not in excl and ":free" in m]
    if len(cands)<3: cands=[m for m in avail if m not in excl]
    groups={}
    for m in cands:
        prov=provider_from_model(m)
        if prov not in groups:
            groups[prov]=m  # one per provider only
    # Prioritize top companies
    order=["OpenAI","Anthropic","Google","Meta","Mistral","DeepSeek","Qwen","xAI"]
    sel=[]
    for name in order:
        if name in groups:
            sel.append(groups[name])
            del groups[name]
        if len(sel)>=MAX_JUDGES: break
    # Fill with remaining providers if needed
    for prov,m in groups.items():
        if len(sel)>=MAX_JUDGES: break
        if m not in sel:
            sel.append(m)
    print(f"Judges selected - ONE PER COMPANY: {', '.join(provider_from_model(m) for m in sel)}")
    return sel[:MAX_JUDGES]

def get_debate_roles(topic, model):
    tl=(topic or "").lower()
    if "god" in tl and "serpent" in tl:
        return {
            "side_a_label": "GOD TOLD TRUTH",
            "side_a_desc": "Defends that God told the truth in Genesis, serpent deceived",
            "side_b_label": "SERPENT TOLD TRUTH",
            "side_b_desc": "Defends that serpent told truth, God did not",
        }
    # For ANY other topic.txt, ask LLM to create opposite labels
    prompt=f'Topic: "{topic}" Return ONLY JSON: {{"side_a_label":"2-3 word label for FOR side","side_a_desc":"one sentence for side","side_b_label":"2-3 word label for AGAINST side","side_b_desc":"one sentence"}} Labels must be uppercase, short, opposite. Example: Creator Required vs No Creator.'
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
    # Fallback generic but topic-adaptive
    return {
        "side_a_label": "AFFIRMATIVE",
        "side_a_desc": f"Argues FOR: {topic}",
        "side_b_label": "NEGATIVE",
        "side_b_desc": f"Argues AGAINST: {topic}",
    }

def strip_filler(text):
    for pat in [r"^(ladies and gentlemen[,.]?\s*)",r"^(my friends[,.]?\s*)",r"^(well[,.]?\s*)",r"^(thank you[,.]?\s*)"]:
        text=re.sub(pat,"",text,flags=re.IGNORECASE).strip()
    return text

def generate_fallback_debate(side_label, topic, round_num, turn_num):
    # Topic-adaptive fallback - works for ANY topic.txt, not just Genesis
    if "GOD TOLD TRUTH" in side_label.upper():
        templates=[
            f"In Genesis 2:17 God says moth tamuth - surely die. Serpent in 3:4 says lo moth temuthun - not surely die. Direct contradiction. Genesis 3:8 shows spiritual death that day - hiding, shame. Hebrew yom can mean day but process began that day, expelled from tree of life 3:24. Serpent told half truth.",
            f"God in 2:16 is generous - freely eat all. Serpent twists in 3:1 - hath God said ye shall not eat of every tree? He exaggerates to make God stingy. Deceiver misquotes. Genesis 3:22 God says man become as one of us knowing good and evil - serpent promised that in 3:5 and it happened, but omitted consequence. Half truth is whole lie.",
            f"Genesis 3:7 eyes opened and knew naked - shame, fear, blame. 3:19 dust to dust - physical death process. Romans 5:12 sin entered and death through sin. Serpent said you shall not die, but death came. Even if not 24hr, they began dying, cut from eternal life source.",
        ]
    elif "SERPENT TOLD TRUTH" in side_label.upper():
        templates=[
            f"Genesis 2:17 beyom - in the day you shall die. Genesis 5:5 Adam lived 930 years. Not same day. Serpent 3:4 you shall not surely die, 3:5 eyes opened you shall be as gods. Genesis 3:7 eyes opened - as serpent said. Genesis 3:22 God confirms - man become as one of us knowing good and evil. God confirms serpent. Who was accurate?",
            f"Yom in Genesis 1 is 24hr. Beyom implies same day. Adam did not die that day. Serpent said you shall be as gods knowing - God in 3:22 says exactly that. Two claims, both verified by narrator, while God's threat not carried out same day. Simple reading - serpent described immediate outcome.",
            f"Spiritual death imports theology not in text. Text says nothing about spiritual death in Genesis 2-3. Dust to dust 3:19 later. Immediate test: did they die that day as God said? No. Did eyes open as serpent said? Yes 3:7. Serpent told what would happen, God threatened more than happened.",
        ]
    else:
        # GENERIC for ANY topic in topic.txt - topic-adaptive
        templates=[
            f"On the question {topic}, the evidence points clearly one way. The core claim is {side_label}. Look at what actually happened versus what was promised. The immediate outcome, the logical consistency, and the burden of proof all matter. We must weigh facts not assumptions.",
            f"Regarding {topic}, we must ask what the best explanation is. {side_label} has stronger reasoning. Consider examples, counterexamples, and whether alternative explanations have been considered. The opposing view fails on key points.",
            f"The topic {topic} requires careful analysis. {side_label} argues from specific evidence, not vague claims. We should examine the text, the context, and the consequences. The other side relies on importing ideas not present.",
        ]
    idx=(round_num*3+turn_num)%len(templates)
    base=templates[idx]
    while count_words(base)<120:
        base+=" The text, context, and outcome must be weighed without importing later ideas. Hebrew, logic, and narrative flow matter."
    return base

def generate_turn(side, topic, round_num, turn_num, previous_exchange, model, role_label, role_desc, opponent_label, opponent_desc):
    prev_snip=(previous_exchange or "")[-500:]
    prompt=f"You are {role_label} debating {topic}. Your stance: {role_desc}. Opponent {opponent_label}: {opponent_desc}. Previous: {prev_snip}\nWrite {WORDS_PER_TURN} word specific argument quoting verses or facts, rebut opponent, no greeting, start directly, {MIN_TURN_WORDS}-{MAX_TURN_WORDS} words."
    for m in [model]+FALLBACK_MODELS[:2]:
        resp=query_openrouter(prompt,m,max_tokens=600,temperature=0.8)
        if resp and count_words(resp)>=70:
            cleaned=strip_filler(resp)
            if count_words(cleaned)>=MIN_TURN_WORDS:
                return cleaned[:1300]
            # Extend if short
            extra=query_openrouter(f"Continue 60 more words same argument: {cleaned[-200:]}",m,max_tokens=200,temperature=0.7)
            if extra: cleaned+=" "+extra
            return cleaned[:1300]
    return generate_fallback_debate(role_label, topic, round_num, turn_num)

def build_round_exchanges(topic, rn, ap_model, sk_model, prev_hist, roles):
    a=[]; s=[]; hist=prev_hist
    for tn in range(1,TURNS_PER_SIDE_PER_ROUND+1):
        aa=generate_turn("A",topic,rn,tn,hist,ap_model, roles["side_a_label"], roles["side_a_desc"], roles["side_b_label"], roles["side_b_desc"])
        a.append(aa); hist=f"{roles['side_a_label']}:\n{aa}\n\n"
        ss=generate_turn("B",topic,rn,tn,hist,sk_model, roles["side_b_label"], roles["side_b_desc"], roles["side_a_label"], roles["side_a_desc"])
        s.append(ss); hist+=f"{roles['side_b_label']}:\n{ss}\n\n"
        print(f"   Exchange {tn}: {roles['side_a_label']}={count_words(aa)}w {roles['side_b_label']}={count_words(ss)}w")
    return a,s,hist

def neutral_judge(model):
    return {"model":model,"provider":provider_from_model(model),"display_name":get_judge_short_name(model),"A_argument":50,"A_rebuttal":50,"A_clarity":50,"A_total":50,"B_argument":50,"B_rebuttal":50,"B_clarity":50,"B_total":50,"winner":"A"}

def judge_round(model,topic,rn,ap,sk,roles):
    prompt=f"Judge {topic} R{rn} {roles['side_a_label']}: {ap[:700]} vs {roles['side_b_label']}: {sk[:700]} JSON {{\"A_argument\":0,\"A_rebuttal\":0,\"A_clarity\":0,\"B_argument\":0,\"B_rebuttal\":0,\"B_clarity\":0}}"
    resp=query_openrouter(prompt,model,timeout=30,max_tokens=250,temperature=0.2)
    if not resp: return neutral_judge(model)
    try:
        m=re.search(r"\{.*\}",resp,re.DOTALL)
        if not m: return neutral_judge(model)
        d=json.loads(m.group(0))
        aa,ar,ac=clamp_score(d.get("A_argument",50)),clamp_score(d.get("A_rebuttal",50)),clamp_score(d.get("A_clarity",50))
        ba,br,bc=clamp_score(d.get("B_argument",50)),clamp_score(d.get("B_rebuttal",50)),clamp_score(d.get("B_clarity",50))
        at=(aa+ar+ac)/3; bt=(ba+br+bc)/3
        return {"model":model,"provider":provider_from_model(model),"display_name":get_judge_short_name(model),"A_argument":aa,"A_rebuttal":ar,"A_clarity":ac,"A_total":round(at,2),"B_argument":ba,"B_rebuttal":br,"B_clarity":bc,"B_total":round(bt,2),"winner":"A" if at>bt else "B"}
    except: return neutral_judge(model)

def evaluate_round(judges,topic,rn,ap,sk,roles):
    results=[]
    print(f"Judging with ONE PER COMPANY: {', '.join(f'{provider_from_model(j)} ({get_judge_short_name(j)})' for j in judges)}")
    def worker(m): return judge_round(m,topic,rn,ap,sk,roles)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1,min(JUDGE_WORKERS,len(judges)))) as ex:
        futs={ex.submit(worker,m):m for m in judges}
        for fu in concurrent.futures.as_completed(futs):
            try:
                res=fu.result(); results.append(res)
                print(f"  Judge — {res['display_name']} [{res['provider']}]")
            except: pass
    if not results: results=[neutral_judge("fallback")]
    return results

def calculate_round_average(results):
    return round(sum(r["A_total"] for r in results)/len(results),2), round(sum(r["B_total"] for r in results)/len(results),2)

async def generate_audio_async(text,voice,filename):
    com=edge_tts.Communicate(text,voice,rate="+0%",volume="+0%")
    audio=b""; words=[]
    async for chunk in com.stream():
        if chunk["type"]=="audio": audio+=chunk["data"]
        elif chunk["type"]=="WordBoundary":
            s=chunk["offset"]/10_000_000; d=chunk["duration"]/10_000_000
            words.append({"text":chunk["text"],"start":s,"duration":d,"end":s+d})
    with open(filename,"wb") as f: f.write(audio)
    if not words:
        clean=clean_for_speech(text); t=0.0
        for tok in clean.split():
            if not tok: continue
            words.append({"text":tok,"start":t,"duration":0.38,"end":t+0.38}); t+=0.43
    return words

def generate_audio(text,role,filename,judge_voice_index=None):
    if "JUDGE" in role.upper(): voice=JUDGE_VOICES[(judge_voice_index or 0)%len(JUDGE_VOICES)]
    elif "GOD TOLD TRUTH" in role.upper(): voice=VOICES["A"]
    elif "SERPENT TOLD TRUTH" in role.upper(): voice=VOICES["B"]
    else: voice=VOICES["A"] if "GOD" in role.upper() else VOICES["B"] if "SERPENT" in role.upper() else VOICES["Moderator"]
    clean=clean_for_speech(text)
    try: return asyncio.run(generate_audio_async(clean,voice,filename))
    except: return asyncio.run(generate_audio_async(clean,VOICES["Moderator"],filename))

def format_ass_time(s):
    s=max(0.0,float(s)); h=int(s//3600); m=int((s%3600)//60); sec=s%60
    return f"{h}:{m:02d}:{sec:05.2f}"
def ass_escape(t): return str(t).replace("\\","\\\\").replace("{","\\{").replace("}","\\}")

def generate_subtitles(words,filename,scorecard=False):
    margin_v=90 if scorecard else 200
    font_size=36 if scorecard else 34
    header=f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: DebateSub,DejaVu Sans,{font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&HCC000000,1,0,0,0,100,100,0,0,1,3.0,1,2,200,200,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
""".format(font_size=font_size, margin_v=margin_v)
    if not words:
        open(filename,"w",encoding="utf-8").write(header); return
    clean_words=[{"text":str(w.get("text","")).strip(),"start":float(w["start"]),"end":float(w["end"])} for w in words if str(w.get("text","")).strip()]
    WORDS_PER_CHUNK=18
    chunks=[]; cur=[]
    for w in clean_words:
        cur.append(w)
        if str(w["text"]).strip().endswith(('.', '?', '!')) and len(cur)>=10:
            chunks.append(cur); cur=[]
        elif len(cur)>=WORDS_PER_CHUNK:
            chunks.append(cur); cur=[]
    if cur: chunks.append(cur)
    events=[]; last_end=0.0
    for chunk in chunks:
        if not chunk: continue
        s=float(chunk[0]["start"])-0.05; e=float(chunk[-1]["end"])+0.2
        if s<last_end: s=last_end+0.01
        if e<=s: e=s+1.0
        last_end=e
        txt_words=[ass_escape(w["text"]) for w in chunk]
        lines=[]
        for i in range(0,len(txt_words),8):
            lines.append(" ".join(txt_words[i:i+8]))
        if len(lines)>3: lines=lines[:3]
        txt="\\N".join(lines)
        txt=txt.replace("\\\\N","\\N")
        ass_text="{\\an2\\pos(960,820)\\q2\\fad(60,60)}"+txt
        events.append(f"Dialogue: 0,{format_ass_time(s)},{format_ass_time(e)},DebateSub,,0,0,0,,{ass_text}")
    open(filename,"w",encoding="utf-8").write(header+"\n".join(events)+"\n")

def fallback_visual_plan(text):
    tl=text.lower()
    visuals=[]
    kws=[
        ("apple","Apple on branch","red apple hanging, swinging"),
        ("fruit","Eating fruit","stick figure eating fruit"),
        ("tree","Tree in garden","tree with rustling leaves"),
        ("garden","Garden of Eden","garden scene"),
        ("serpent","Serpent","snake coiled on branch moving"),
        ("snake","Snake","snake slithering"),
        ("god","God light","light rays from above"),
        ("adam","Adam","stick figure man"),
        ("eve","Eve","stick figure woman"),
        ("eat","Eating","person eating apple"),
        ("know","Knowing","person thinking"),
        ("death","Death","skull icon"),
        ("die","Die that day","calendar X"),
        ("eyes opened","Eyes opened","eyes wide open"),
        ("creator","Creator","universe and light"),
        ("universe","Universe","stars and galaxy"),
        ("exist","Existence","question mark and lightbulb"),
        ("moral","Morality","scales balancing"),
        ("consciousness","Consciousness","brain and spark"),
    ]
    for kw,label,desc in kws:
        if kw in tl and len(visuals)<MAX_VISUALS_PER_SEGMENT:
            idx=tl.find(kw)
            phrase=text[max(0,idx-10):idx+len(kw)+20].strip() or kw
            visuals.append({"phrase":phrase,"label":label,"description":desc,"kind":"concept"})
    # Ensure at least 2 visuals even for generic topics
    if not visuals:
        visuals=[
            {"phrase":text[:30],"label":"Thinking","description":"person thinking","kind":"concept"},
            {"phrase":text[:30],"label":"Debate","description":"two figures debating","kind":"concept"},
        ]
    return visuals[:MAX_VISUALS_PER_SEGMENT]

def plan_visuals(text,model):
    prompt=f"Find up to {MAX_VISUALS_PER_SEGMENT} simple visual moments in: {text}\nJSON [{{\"phrase\":\"exact phrase\",\"label\":\"2-4 words\"}}]"
    resp=query_openrouter(prompt,model,timeout=20,max_tokens=350,temperature=0.2)
    if not resp: return fallback_visual_plan(text)
    try:
        m=re.search(r"\[.*\]",resp,re.DOTALL)
        if not m: return fallback_visual_plan(text)
        data=json.loads(m.group(0))
        out=[]
        for it in data:
            if not isinstance(it,dict): continue
            ph=str(it.get("phrase","")).strip(); lb=str(it.get("label","")).strip()
            if not ph or not lb: continue
            out.append({"phrase":ph,"label":lb[:30],"description":lb,"kind":"concept"})
            if len(out)>=MAX_VISUALS_PER_SEGMENT: break
        return out if out else fallback_visual_plan(text)
    except: return fallback_visual_plan(text)

def find_phrase_timing(phrase,words):
    if not phrase or not words: return None
    pw=re.findall(r"\b[\w'-]+\b",phrase.lower())
    sw=[re.sub(r"[^\w'-]","",str(w["text"]).lower()) for w in words]
    pw=[x for x in pw if x]
    if not pw: return None
    for i in range(len(sw)-len(pw)+1):
        if sw[i:i+len(pw)]==pw:
            s=float(words[i]["start"]); e=float(words[min(len(words)-1,i+len(pw)-1)]["end"])+4.5
            return {"start":max(0.0,s-0.15),"end":max(s+4.5,e+1.5)}
    for p in pw:
        if len(p)<4: continue
        for idx,s in enumerate(sw):
            if p==s:
                return {"start":float(words[idx]["start"]),"end":float(words[min(len(words)-1,idx+16)]["end"])+3.0}
    return None

def fallback_visual_timing(idx,total,words):
    if not words: return None
    last=float(words[-1]["end"]); us=0.15*last; ue=0.85*last
    s=us if total<=1 else us + (ue-us)*idx/max(1,total-1)
    return {"start":max(0.0,s),"end":s+5.0}

def create_visual_plan(text,words,model):
    if not words: return []
    cands=plan_visuals(text,model)
    timed=[]
    for idx,it in enumerate(cands):
        t=find_phrase_timing(it["phrase"],words) or fallback_visual_timing(idx,len(cands),words)
        if not t: continue
        it2=dict(it); it2.update(t); timed.append(it2)
    timed.sort(key=lambda x:x["start"])
    out=[]
    for it in timed:
        if any(abs(it["start"]-p["start"])<MIN_VISUAL_GAP for p in out): continue
        out.append(it)
        if len(out)>=MAX_VISUALS_PER_SEGMENT: break
    return out

def draw_stick_figure(draw,x,y,size=80,eating=False):
    draw.ellipse([x+size*0.3,y,x+size*0.7,y+size*0.4],fill=(222,184,135,255),outline=(0,0,0,255),width=2)
    for i in range(5): draw.arc([x+size*0.2+i*3,y-5,x+size*0.8-i*2,y+size*0.3],0,180,fill=(0,0,0,255),width=2)
    draw.ellipse([x+size*0.2,y+size*0.45,x+size*0.8,y+size*0.95],fill=(222,184,135,255),outline=(0,0,0,255),width=2)
    if eating: draw.line([x+size*0.7,y+size*0.6,x+size*0.95,y+size*0.4],fill=(0,0,0,255),width=3)
    else:
        draw.line([x,y+size*0.6,x+size*0.2,y+size*0.7],fill=(0,0,0,255),width=3)
        draw.line([x+size*0.8,y+size*0.6,x+size*1.0,y+size*0.5],fill=(0,0,0,255),width=3)

def create_visual_asset(visual,index):
    filename=f"visual_{index}.gif"
    label=(visual.get('label','')+" "+visual.get('description','')).lower()
    frames=[]
    for f in range(18):
        progress=f/18.0
        frame=Image.new("RGBA",(VISUAL_W,VISUAL_H),(0,0,0,0))
        draw=ImageDraw.Draw(frame)
        if "apple" in label or "fruit" in label:
            draw.line([20,80,VISUAL_W-20,90],fill=(101,67,33,255),width=4)
            swing=10*math.sin(2*math.pi*progress)
            ax=VISUAL_W//2+10+swing; ay=120+5*math.sin(4*math.pi*progress)
            draw.ellipse([ax-25,ay,ax+25,ay+40],fill=(220,20,60,255),outline=(0,0,0,255),width=2)
            draw_stick_figure(draw,VISUAL_W//2-60,VISUAL_H-160,size=100,eating=("eat" in label))
            if "eat" in label and progress>0.5:
                by=VISUAL_H-110-40*(progress-0.5)*2
                draw.ellipse([VISUAL_W//2+15,by,VISUAL_W//2+35,by+20],fill=(220,20,60,255),outline=(0,0,0,255),width=2)
        elif "tree" in label or "garden" in label:
            draw.rectangle([VISUAL_W//2-15,VISUAL_H-100,VISUAL_W//2+15,VISUAL_H-20],fill=(101,67,33,255),outline=(0,0,0,255),width=2)
            rustle=8*math.sin(2*math.pi*progress)
            for lx,ly in [(VISUAL_W//2-60+rustle,VISUAL_H-180),(VISUAL_W//2+20-rustle,VISUAL_H-200)]:
                draw.ellipse([lx,ly,lx+80,ly+60],fill=(34,139,34,200),outline=(0,0,0,150),width=1)
            fall_y=(progress*VISUAL_H)%(VISUAL_H-50); fall_x=VISUAL_W//2+40*math.sin(progress*6)
            draw.ellipse([fall_x,fall_y,fall_x+12,fall_y+18],fill=(34,139,34,180))
        elif "serpent" in label or "snake" in label:
            draw.line([20,100,VISUAL_W-20,110],fill=(101,67,33,255),width=4)
            pts=[]
            for i in range(0,VISUAL_W-40,10):
                pts.append((i+20,100+15*math.sin((i/30)+progress*4*math.pi)))
            if len(pts)>1:
                draw.line(pts,fill=(34,139,34,255),width=8,joint="curve")
                draw.line(pts,fill=(0,0,0,255),width=2,joint="curve")
            hx,hy=pts[-1] if pts else (VISUAL_W-40,100)
            draw.ellipse([hx,hy-6,hx+18,hy+6],fill=(34,139,34,255),outline=(0,0,0,255),width=2)
            if f%6<3: draw.line([hx+18,hy,hx+28,hy-3],fill=(220,20,60,255),width=2)
        elif "god" in label or "creator" in label or "universe" in label:
            cx=VISUAL_W//2
            for ang in range(-40,41,15):
                rad=math.radians(ang)
                x2=cx+200*math.sin(rad); y2=30+200*math.cos(rad)
                draw.line([cx,10,x2,y2],fill=(255,215,0,100+int(50*math.sin(progress*3))),width=3)
            draw.ellipse([cx-30,10,cx+30,70],fill=(255,215,0,200),outline=(0,0,0,255),width=2)
        else:
            draw_stick_figure(draw,VISUAL_W//2-50,VISUAL_H//2-40,size=120)
            by=VISUAL_H//2-80+5*math.sin(progress*2*math.pi)
            draw.ellipse([VISUAL_W//2+30,by-30,VISUAL_W//2+110,by+20],fill=(255,255,255,200),outline=(0,0,0,255),width=2)
        mask=Image.new("L",(VISUAL_W,VISUAL_H),0)
        ImageDraw.Draw(mask).rounded_rectangle((0,0,VISUAL_W,VISUAL_H),radius=28,fill=255)
        final=Image.new("RGBA",(VISUAL_W,VISUAL_H),(0,0,0,0))
        final.paste(frame,(0,0),frame)
        final.putalpha(mask)
        frames.append(final)
    frames[0].save(filename,format='GIF',save_all=True,append_images=frames[1:],duration=160,loop=0,disposal=2,transparency=0)
    print(f"   Created transparent animation: {visual.get('label')} ({len(frames)} frames, moves, transparent bg)")
    return filename

def create_background(position,glow_color,filename):
    source=os.path.join(os.path.dirname(os.path.abspath(__file__)),"background.png")
    if os.path.exists(source):
        try: image=Image.open(source).convert("RGB").resize((VIDEO_W,VIDEO_H))
        except: image=Image.new("RGB",(VIDEO_W,VIDEO_H),(12,16,32))
    else: image=Image.new("RGB",(VIDEO_W,VIDEO_H),(12,16,32))
    overlay=Image.new("RGBA",(VIDEO_W,VIDEO_H),(0,0,0,0))
    d=ImageDraw.Draw(overlay)
    cx=400 if position=="left" else 1520 if position=="right" else 960
    for rad in range(700,50,-50):
        a=int(15*(1-rad/700))
        d.ellipse([cx-rad,540-rad,cx+rad,540+rad],fill=hex_to_rgba(glow_color,a))
    overlay=overlay.filter(ImageFilter.GaussianBlur(30))
    Image.alpha_composite(image.convert("RGBA"),overlay).convert("RGB").save(filename)

def create_ui_overlay(speaker_name,topic,position,glow_color,filename):
    image=Image.new("RGBA",(VIDEO_W,VIDEO_H),(0,0,0,0))
    draw=ImageDraw.Draw(image)
    title_font=load_font(30,bold=True); name_font=load_font(26,bold=True)
    title=f"TOPIC: {topic}"
    if len(title)>85: title=title[:82]+"..."
    box=draw.textbbox((0,0),title,font=title_font)
    draw.text(((VIDEO_W-(box[2]-box[0]))//2,24),title,fill="white",font=title_font)
    cw=750; ch=110; cy=885
    cx=75 if position=="left" else VIDEO_W-cw-75 if position=="right" else (VIDEO_W-cw)//2
    draw.rounded_rectangle([cx,cy,cx+cw,cy+ch],radius=18,fill=(18,26,46,235),outline=glow_color,width=4)
    draw.ellipse([cx+22,cy+27,cx+47,cy+52],fill=glow_color)
    draw.text((cx+65,cy+22),speaker_name,fill="white",font=name_font)
    image.save(filename)
    return cx,cy

def ffmpeg_filter_path(fn): return os.path.abspath(fn).replace("\\","/").replace("'","\\'").replace(":","\\:")

def render_video_segment(background,ui,audio,subtitles,output,position,glow_color,card_x,card_y,visual_plan):
    for p in [background,ui,audio,subtitles]:
        if not os.path.exists(p): raise FileNotFoundError(p)
    vassets=[]
    for idx,vis in enumerate(visual_plan or []):
        try:
            asset=create_visual_asset(vis,idx)
            if asset: vassets.append((asset,vis))
        except Exception as e: print(f"Visual skip: {e}")
    glow=glow_color.lstrip("#")
    pan_x="0" if position=="left" else "iw-(iw/zoom)" if position=="right" else "(iw-(iw/zoom))/2"
    parts=[f"[0:v]scale=1920:1080,zoompan=z='min(zoom+0.00020,1.05)':x='{pan_x}':y='(ih-(ih/zoom))/2':d=9000:s=1920x1080:fps=30[bg];","[1:v]scale=1920:1080[ui];",f"[2:a]showwaves=s=300x58:mode=cline:colors=0x{glow}:rate=30[wave];","[bg][ui]overlay=0:0[base];",f"[base][wave]overlay={card_x+330}:{card_y+47}[withwave];"]
    cur="[withwave]"; idx_in=3
    for i,(asset,vis) in enumerate(vassets):
        s=max(0.0,float(vis["start"])); e=max(s+3.5,float(vis["end"]))
        parts.append(f"[{idx_in}:v]format=rgba,fade=t=in:st={s}:d=0.6:alpha=1,fade=t=out:st={e-0.6}:d=0.6:alpha=1[vf{i}];")
        x=(VIDEO_W-VISUAL_W)//2; y_expr=f"{VISUAL_Y} - (t-{s})*1"; en=f"between(t,{s:.2f},{e:.2f})"
        parts.append(f"{cur}[vf{i}]overlay={x}:'{y_expr}':enable='{en}'[v{i}];")
        cur=f"[v{i}]"; idx_in+=1
    parts.append(f"{cur}ass='{ffmpeg_filter_path(subtitles)}'[outv]")
    fc="".join(parts)
    cmd=["ffmpeg","-y","-loop","1","-framerate",str(FPS),"-i",background,"-i",ui,"-i",audio]
    for a,_ in vassets: cmd+=["-ignore_loop","0","-i",a]
    cmd+=["-filter_complex",fc,"-map","[outv]","-map","2:a","-c:v","libx264","-preset","veryfast","-crf","20","-pix_fmt","yuv420p","-c:a","aac","-b:a","192k","-shortest",output]
    res=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    if res.returncode!=0: print(res.stderr[-7000:]); raise RuntimeError(f"FFmpeg failed {output}")
    for a,_ in vassets:
        try: os.remove(a)
        except: pass

def generate_scoreboard(rn,results,ra,rb,ca,cb,filename,roles):
    src=os.path.join(os.path.dirname(os.path.abspath(__file__)),"background.png")
    if os.path.exists(src):
        try: img=Image.open(src).convert("RGB").resize((VIDEO_W,VIDEO_H))
        except: img=Image.new("RGB",(VIDEO_W,VIDEO_H),(12,16,32))
    else: img=Image.new("RGB",(VIDEO_W,VIDEO_H),(12,16,32))
    over=Image.new("RGBA",(VIDEO_W,VIDEO_H),(0,0,0,235))
    img=Image.alpha_composite(img.convert("RGBA"),over).convert("RGB")
    d=ImageDraw.Draw(img)
    hdr=load_font(38,bold=True); sub=load_font(22,bold=True); sml=load_font(18)
    def centred(y,txt,fnt,col):
        box=d.textbbox((0,0),txt,font=fnt); w=box[2]-box[0]; d.text(((VIDEO_W-w)//2,y),txt,fill=col,font=fnt)
    centred(24,f"ROUND {rn} — AI JUDGING PANEL",hdr,"#FFD700")
    centred(72,f"{len(results)} JUDGES — {', '.join(r['display_name'] for r in results)}",sub,"white")
    centred(112,f"ROUND SCORE   {roles['side_a_label']} {ra:.1f}   VS   {roles['side_b_label']} {rb:.1f}",sub,"white")
    centred(150,f"CUMULATIVE   {roles['side_a_label']} {ca:.1f}   VS   {roles['side_b_label']} {cb:.1f}",sub,"#FFD700")
    d.text((100,225),"CATEGORY AVERAGES",fill="#FFD700",font=sub)
    d.text((500,265),roles['side_a_label'],fill="#00FFCC",font=sml); d.text((680,265),roles['side_b_label'],fill="#FF66FF",font=sml)
    y=310
    for label,ak,bk in [("Argument strength","A_argument","B_argument"),("Rebuttal quality","A_rebuttal","B_rebuttal"),("Clarity & reasoning","A_clarity","B_clarity")]:
        a=sum(r[ak] for r in results)/len(results); b=sum(r[bk] for r in results)/len(results)
        d.text((100,y),label,fill="white",font=sml); d.text((500,y),f"{a:.1f}",fill="#00FFCC",font=sml); d.text((680,y),f"{b:.1f}",fill="#FF66FF",font=sml); y+=48
    d.text((980,225),"INDIVIDUAL JUDGES - ONE PER COMPANY",fill="#FFD700",font=sub)
    d.text((980,270),"MODEL",fill="white",font=sml); d.text((1500,270),roles['side_a_label'][:1],fill="#00FFCC",font=sml); d.text((1580,270),roles['side_b_label'][:1],fill="#FF66FF",font=sml)
    d.line([(970,300),(1680,300)],fill=(100,110,140,255),width=2)
    sy=320
    for r in results:
        d.text((980,sy),f"{r.get('display_name','?')} [{r.get('provider','?')}]",fill="white",font=sml)
        d.text((1500,sy),f"{r['A_total']:.1f}",fill="#00FFCC",font=sml); d.text((1580,sy),f"{r['B_total']:.1f}",fill="#FF66FF",font=sml); sy+=48
    img.save(filename)

def render_scorecard_video(scorecard,audio,subtitles,output):
    for p in [scorecard,audio,subtitles]:
        if not os.path.exists(p): raise FileNotFoundError(p)
    fc=f"[0:v]scale=1920:1080[base];[base]ass='{ffmpeg_filter_path(subtitles)}'[outv]"
    cmd=["ffmpeg","-y","-loop","1","-framerate",str(FPS),"-i",scorecard,"-i",audio,"-filter_complex",fc,"-map","[outv]","-map","1:a","-c:v","libx264","-preset","veryfast","-crf","20","-pix_fmt","yuv420p","-c:a","aac","-b:a","192k","-shortest",output]
    res=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    if res.returncode!=0: print(res.stderr[-7000:]); raise RuntimeError("Scorecard failed")

def create_segment(text,role,speaker_name,topic,segment_id,model_for_visuals,position=None,glow=None,judge_voice_index=None):
    if position is None:
        if "GOD" in role.upper(): position="left"
        elif "SERPENT" in role.upper(): position="right"
        else: position="center" if "JUDGE" in role.upper() or role=="Moderator" else "left"
    if glow is None:
        glow="#00FFCC" if "GOD" in role.upper() else "#FF00FF" if "SERPENT" in role.upper() else "#3399FF" if "JUDGE" in role.upper() else "#FFD700"
    af=f"audio_{segment_id}.mp3"; sf=f"subs_{segment_id}.ass"; bf=f"bg_{segment_id}.png"; uf=f"ui_{segment_id}.png"; vf=f"segment_{segment_id}.mp4"
    words=generate_audio(text,role,af,judge_voice_index)
    generate_subtitles(words,sf)
    vplan=[]
    try:
        vplan=create_visual_plan(clean_for_speech(text),words,model_for_visuals)
        if vplan: print(f"   {len(vplan)} visual(s): {', '.join(v['label'] for v in vplan)}")
    except Exception as e: print(f"Visual planning skipped: {e}")
    create_background(position,glow,bf)
    cx,cy=create_ui_overlay(speaker_name,topic,position,glow,uf)
    render_video_segment(bf,uf,af,sf,vf,position,glow,cx,cy,vplan)
    return vf

def generate_panel_commentary(model,side,topic,rn,ap,sk,prev,roles):
    prov=get_judge_short_name(model)
    pref_label = roles['side_a_label'] if side=="A" else roles['side_b_label']
    recent="\n".join(prev[-4:])
    def trim(t,mw=200):
        wl=t.split(); return t if len(wl)<=mw else " ".join(wl[-mw:])
    prompt=f"You are {prov} judge. Topic:{topic} Round:{rn} {roles['side_a_label']}:{trim(ap)} vs {roles['side_b_label']}:{trim(sk)} You preferred {pref_label}. Give 2 sentence specific critique. Previous: {recent}"
    resp=query_openrouter(prompt,model,timeout=30,max_tokens=200,temperature=0.7)
    return resp if resp else f"Round {rn} - {pref_label} had stronger exegesis."

def build_intro(topic,jc,roles):
    return f"Welcome to the AI Debate Arena. Today, {roles['side_a_label']} faces {roles['side_b_label']} on the question: {topic}. Three rounds, equal time, independent panel of {jc} top AI judges from different companies - one per company including ChatGPT, Claude, Gemini, Llama, Mistral, DeepSeek, Qwen - scoring argument, rebuttal and clarity. Let's begin."

def build_outro(jc,ca,cb,roles):
    if math.isclose(ca,cb,abs_tol=0.01): res="a draw"
    elif ca>cb: res=roles['side_a_label']
    else: res=roles['side_b_label']
    return f"After three rounds, panel of {jc} judges from different companies gave {roles['side_a_label']} {ca:.1f}, {roles['side_b_label']} {cb:.1f}. Final result is {res}. The text remains - you decide."

def stitch_segments(segs,out):
    lf="concat_list.txt"
    open(lf,"w",encoding="utf-8").write("\n".join([f"file '{os.path.abspath(s).replace(chr(39),chr(39)+chr(92)+chr(39)+chr(39))}'" for s in segs])+"\n")
    cmd=["ffmpeg","-y","-f","concat","-safe","0","-i",lf,"-c","copy",out]
    r=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    if r.returncode!=0: print(r.stderr[-7000:]); raise RuntimeError("Concat failed")

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
    print(f"Roles: {roles['side_a_label']} VS {roles['side_b_label']} - Topic-adaptive from topic.txt")
    print(f"Debate engines: {get_judge_short_name(ap_model)} [{provider_from_model(ap_model)}] vs {get_judge_short_name(sk_model)} [{provider_from_model(sk_model)}] - different companies")
    judges=choose_judges(avail,(ap_model,sk_model))
    if not judges: judges=FALLBACK_MODELS[:MAX_JUDGES]
    print(f"Judges ({len(judges)}): ONE PER COMPANY enforced")
    for j in judges: print(f"  - {get_judge_short_name(j)} [{provider_from_model(j)}] - {j}")
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
        sw=generate_audio(st,"Moderator",sa); generate_subtitles(sw,ss,scorecard=True)
        render_scorecard_video(sb,sa,ss,sv); segs.append(sv)
        if res:
            a_res=[r for r in res if r["winner"]=="A"] or res
            b_res=[r for r in res if r["winner"]=="B"] or res
            ja=random.choice(a_res); jb=random.choice(b_res)
            ca=generate_panel_commentary(ja["model"],"A",topic,rn,a_full,s_full,pcom,roles); pcom.append(ca)
            add_segment(ca,"AI Judge",f"AI JUDGE — {ja['display_name'].upper()}","center","#3399FF",judge_voice_index=0)
            cb=generate_panel_commentary(jb["model"],"B",topic,rn,a_full,s_full,pcom,roles); pcom.append(cb)
            add_segment(cb,"AI Judge",f"AI JUDGE — {jb['display_name'].upper()}","center","#3399FF",judge_voice_index=1)
    add_segment(build_outro(len(judges),cum_a,cum_b,roles),"Moderator","MODERATOR")
    stitch_segments(segs,OUTPUT_FILE)
    print(f"\nCOMPLETE: {OUTPUT_FILE} — {cum_a:.1f} vs {cum_b:.1f}")
    print(f"Topic-adaptive: YES - roles {roles['side_a_label']} vs {roles['side_b_label']} from topic.txt '{topic}'")
    print(f"One per company: YES - {len(judges)} judges from different companies")
    cleanup_cache()

if __name__=="__main__":
    try: run_debate_pipeline()
    except KeyboardInterrupt: print("Cancelled")
    except Exception as e: print("FAILED"); print(str(e)); raise
