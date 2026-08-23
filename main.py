
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

MAX_VISUALS_PER_SEGMENT = 4
MIN_VISUAL_GAP = 0.8
VISUAL_W = 520
VISUAL_H = 520
VISUAL_Y = 160

# Most natural conversational voices - fixed for language audio not code
VOICES = {
    "A": "en-US-GuyNeural",
    "B": "en-US-AriaNeural",
    "Moderator": "en-US-JennyNeural",
}
JUDGE_VOICES = [
    "en-US-DavisNeural",
    "en-US-JaneNeural",
    "en-US-JasonNeural",
    "en-US-NancyNeural",
    "en-US-ChristopherNeural",
    "en-US-EmmaNeural",
    "en-US-AndrewNeural",
]

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
    # FIX: ensure language audio, not code spoken
    # Remove code artifacts, brackets, etc.
    t=re.sub(r"\([^)]*\)","",t or "")  # remove (like this)
    t=re.sub(r"\[.*?\]","",t)  # remove [brackets]
    t=re.sub(r"\{.*?\}","",t)  # remove {braces}
    t=t.replace("–",", ").replace("—",". ").replace(" - ",". ").replace(" -",". ").replace("- ",". ")
    for o,n in {"*":"", "#":"", "_":"", "`":"", "\"":"", ":":" . ", ";":" . ", "&":" and", "=":" ", ">":" ", "<":" ", "/":" ", "\\":" "}.items():
        t=t.replace(o,n)
    t=re.sub(r"\s-\s", ". ", t)
    # Remove any leftover code-like tokens
    t=re.sub(r"\b[a-z_]+\.[a-z_]+\(\)","",t)  # remove function calls like foo.bar()
    t=re.sub(r"\s+"," ",t).strip()
    t=t.replace(" . . ", ". ").replace(" , , ", ", ")
    if t and not t[-1] in ".!?":
        t+="."
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

def query_openrouter(prompt,model_id,timeout=50,max_tokens=750,temperature=0.85):
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
    excl=set(primary)
    top_providers = {"openai","anthropic","google","meta-llama","mistralai","deepseek","qwen"}
    cands=[m for m in avail if m not in excl and ":free" in m and m.split("/")[0].lower() in top_providers]
    if len(cands)<4:
        cands=[m for m in avail if m not in excl and ":free" in m]
    groups={}
    for m in cands:
        prov=provider_from_model(m)
        if prov not in groups:
            groups[prov]=m
    order=["OpenAI","Anthropic","Google","Meta","Mistral","DeepSeek","Qwen"]
    sel=[]
    for name in order:
        if name in groups:
            sel.append(groups[name])
            del groups[name]
        if len(sel)>=MAX_JUDGES: break
    for prov,m in groups.items():
        if len(sel)>=MAX_JUDGES: break
        if m not in sel:
            sel.append(m)
    print(f"Judges ONE PER COMPANY: {', '.join(provider_from_model(m) for m in sel)}")
    return sel[:MAX_JUDGES]

def get_debate_roles(topic, model):
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
        if round_num==1:
            templates=[
                "Genesis chapter 2 verse 17 is really clear when you read it carefully. God says, in the day you eat of it, you shall surely die. The Hebrew is emphatic, it literally says dying you shall die. Now look what the serpent says in chapter 3 verse 4. He says, you shall not surely die. That is a direct contradiction. What actually happens that day? Chapter 3 verse 7 says their eyes were opened and they knew they were naked. They felt shame for the first time. Verse 8 says they hid themselves from God's presence. That hiding, that separation, is what the Bible calls death.",
                "I want you to notice God's generosity in chapter 2 verse 16. He says, you may freely eat of every tree in the garden. Every tree, only one limit. That is incredibly generous. Then the serpent twists it in chapter 3 verse 1. He says, did God really say you shall not eat of every tree? He makes God sound stingy. That is classic deception. Then he promises, your eyes shall be opened and you will be as gods. And yes, chapter 3 verse 22 says they did become like God in that way. But his first promise, you will not die, was completely false.",
            ]
        elif round_num==2:
            templates=[
                "My opponent said the serpent told the truth because they did not drop dead that day. But that misses the whole point of what death means in this story. Genesis chapter 3 verse 10, Adam says, I was afraid because I was naked and I hid. Fear and hiding are not full life. Verse 19 says to dust you shall return. Mortality enters. And verses 23 and 24, they are driven out and cherubim block the way to the tree of life. So on the very day they ate, they lost access to eternal life. The process of death started that day.",
                "The argument that they did not die that day ignores how the phrase in the day is used elsewhere. In chapter 2 verse 4, it says in the day that the Lord God made the earth. It means when, not a 24 hour countdown. It is about certainty. When you eat, death is certain. And look, the serpent told a half truth. He said your eyes would be opened, and they were. But he left out the consequence. A half truth that omits crucial consequence is still a lie.",
            ]
        else:
            templates=[
                "Let me pull this together. God warned, in the day you eat you shall surely die. The serpent said, you shall not surely die, you shall be as gods. What happened? Their eyes were opened, yes, as the serpent said. But they also experienced shame, fear, hiding, toil, pain, and were cut off from the tree of life. That is death in the biblical sense, separation and mortality beginning. Romans chapter 5 verse 12 says sin entered and death through sin. The serpent promised no death, but death is now the human condition.",
                "So who told the truth? God said death would come when they ate. The serpent said no death, just enlightenment. The story shows both enlightenment and death entering. Eyes opened, but also shame, blame, cursing, and exile. If the serpent told the whole truth, where is the warning about losing Eden? Where is the warning about returning to dust? He omitted it. God did not. God told them the full cost.",
            ]
    elif "SERPENT TOLD TRUTH" in side_label.upper():
        if round_num==1:
            templates=[
                "Let us read what the text actually says, not what we think it should say. Genesis chapter 2 verse 17, God says, in the day you eat of it you shall surely die. In Hebrew, beyom, in the day. The plain sense is that same day. Yet Genesis chapter 5 verse 5 says Adam lived 930 years and then died. He did not die that day. He lived for centuries. The serpent says in chapter 3 verse 4, you shall not surely die. That is exactly what happened. They did not die that day. He also says in verse 5, your eyes shall be opened and you shall be as gods. Chapter 3 verse 7 says their eyes were opened. God Himself says in verse 22, man has become as one of us to know good and evil. God confirms the serpent was right.",
                "Think about the Hebrew word yom. In Genesis chapter 1, evening and morning were the first day, a literal 24 hour period. So when God says in the day you eat you shall die, the natural reading is that same day. Adam did not die that day. The serpent's prediction was more accurate about the immediate outcome. He said you shall not die, and they did not. He said you shall be as gods knowing good and evil. God says in chapter 3 verse 22, they have become like one of us. Two claims by the serpent, both validated by the story itself.",
            ]
        elif round_num==2:
            templates=[
                "My opponent talks about spiritual death, but the text of Genesis chapters 2 and 3 never mentions spiritual death. That is an idea imported from later theology. The text mentions nakedness, shame, cursing of the ground, pain in childbirth, hard work, and eventually dust to dust. The test is simple. Did they die that day as God said? No. Did their eyes open as the serpent said? Yes, chapter 3 verse 7 says their eyes were opened. On a straightforward reading, the serpent described what would actually happen that day more accurately.",
                "If God meant they would begin dying, why say in the day you shall surely die? Why not say you shall become mortal? And if the serpent lied, why does God confirm his second claim? Chapter 3 verse 22, behold the man is become as one of us to know good and evil. That is almost word for word what the serpent promised in verse 5. If the serpent is the liar, why is God echoing his promise? The story presents a tension that should make us ask who was more accurate.",
            ]
        else:
            templates=[
                "So let us weigh it. God said, in the day you eat you die. Serpent said, you will not die, you will be enlightened, your eyes will be opened. What does the story report? Eyes opened, yes. Enlightenment, yes. Death that day, no. Adam lives 930 years. God even acknowledges the enlightenment part in chapter 3 verse 22. No acknowledgment that they died that day. If we let the text speak for itself, without adding later ideas, the serpent's description of the immediate outcome was more accurate.",
                "The question is not who we want to be truthful, but what the text reports. It reports God threatening death in the day, serpent promising no death but knowledge, and then reports knowledge coming and death not coming that day. It reports God Himself saying they have become like us knowing good and evil. The serpent promised that. So two promises from the serpent, both happen. One threat from God, does not happen that day. On the immediate facts, the serpent was right about what would happen when they ate.",
            ]
    else:
        tl = topic_short.lower()
        if any(w in tl for w in ["ai","artificial","regulation"]):
            if round_num==1:
                templates=[
                    f"On {topic_short}, we need to be practical. {side_label} argues that unchecked AI without oversight causes real harm. We have seen bias in hiring, misinformation at scale, and concentration of power. Regulation does not mean banning. It means testing and transparency that builds trust.",
                    f"Regarding {topic_short}, who pays the cost matters. {side_label} says developers should be responsible for foreseeable misuse. We regulate cars and medicine for safety. The precautionary principle applies when systems affect millions.",
                ]
            elif round_num==2:
                templates=[
                    f"My opponent says regulation stifles innovation. But look at aviation and medicine. Safety standards made people trust flying and drugs, which helped innovation grow. {side_label} is arguing for the same kind of trust building for {topic_short}.",
                    f"On {topic_short}, {side_label} points out that without rules, the worst actors set the standard. Good companies want clear rules so they are not undercut by those who cut corners. That is why regulation can be pro innovation.",
                ]
            else:
                templates=[
                    f"To close on {topic_short}, {side_label} offers balance. Not a ban, but standards. Testing, transparency, liability. Aviation has safety checks. Medicine has trials. Why should AI, which shapes what we see, be exempt from accountability we demand elsewhere?",
                    f"So on {topic_short}, {side_label} says accountability matters. If a system affects millions, those who build it should answer for foreseeable harm. That is not anti innovation. It is how innovation earns trust.",
                ]
        else:
            if round_num==1:
                templates=[
                    f"On {topic_short}, {side_label} has the stronger case when you look at the evidence. The facts and the logic point one way. The opposing view relies on assumptions that do not hold up. We should prefer the explanation that fits what we actually see.",
                    f"Regarding {topic_short}, {side_label} argues from specific examples and follows them logically. The other side shifts definitions or ignores counterexamples. A clear and consistent explanation that matches observation should be preferred.",
                ]
            elif round_num==2:
                templates=[
                    f"My opponent raised points, but they do not address the core evidence for {side_label}. On {topic_short}, we must ask what the counterexamples show. {side_label} accounts for them. The other view struggles when tested against real cases.",
                    f"On {topic_short}, {side_label} points out a flaw in the opponent's reasoning. They assume what they need to prove, or they ignore consequences. {side_label} follows the argument step by step and shows where it breaks down.",
                ]
            else:
                templates=[
                    f"To close on {topic_short}, {side_label} offers a coherent view. It defines terms clearly, follows logic, and fits the facts. The alternative relies on vague claims or shifts ground. We should choose the view that is clear, consistent, and supported.",
                    f"So on {topic_short}, {side_label} says look at which view best explains all the evidence, not just selected pieces. {side_label} explains more with fewer assumptions. That is why it should be preferred.",
                ]
    idx=(round_num*2+turn_num)%len(templates)
    return templates[idx]

def generate_turn(side, topic, round_num, turn_num, previous_exchange, model, role_label, role_desc, opponent_label, opponent_desc):
    prev_snip=(previous_exchange or "")[-700:]
    if round_num==1:
        round_focus="Opening round. Establish foundation. Do not repeat, give fresh opening."
    elif round_num==2:
        round_focus="Rebuttal round. Directly address opponent's last argument and show why it fails. Bring new evidence."
    else:
        round_focus="Closing round. Summarize strongest points and show why your view best explains all evidence."
    prompt=f"You are {role_label} in live debate about {topic}. Your view: {role_desc}. Opponent {opponent_label}: {opponent_desc}. {round_focus} Previous opponent said: {prev_snip}. Write {WORDS_PER_TURN} words as human speaking naturally with contractions, varied sentences, full stops for pauses, no dashes, quote specific verses, rebut directly, start immediately, {MIN_TURN_WORDS}-{MAX_TURN_WORDS} words."
    for m in [model]+FALLBACK_MODELS[:3]:
        resp=query_openrouter(prompt,m,max_tokens=700,temperature=0.88)
        if resp and count_words(resp)>=85:
            cleaned=strip_filler(resp)
            cleaned=re.sub(r'\s*-\s*',' . ',cleaned)
            cleaned=re.sub(r'\s+',' ',cleaned).strip()
            if not cleaned.endswith(('.', '!', '?')):
                cleaned+="."
            cleaned=cleaned.replace(" - ", ". ").replace(" -",".")
            if "text, context, and outcome" in cleaned.lower():
                cleaned=re.sub(r'The text, context.*?matter\.','',cleaned,flags=re.IGNORECASE).strip()
            if count_words(cleaned)>=MIN_TURN_WORDS-10:
                return cleaned[:1500]
            extra=query_openrouter("Continue 70 more words same natural human style: "+cleaned[-250:],m,max_tokens=230,temperature=0.8)
            if extra:
                cleaned+=" "+extra
            return cleaned[:1500]
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
    prompt=f"Judge {topic} R{rn} {roles['side_a_label']}: {ap[:700]} vs {roles['side_b_label']}: {sk[:700]} JSON A_argument etc 0-100"
    resp=query_openrouter(prompt,model,timeout=30,max_tokens=250,temperature=0.25)
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
    print(f"Judging ONE PER COMPANY: {', '.join(f'{provider_from_model(j)} ({get_judge_short_name(j)})' for j in judges)}")
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
    # FIX: ensure language audio not code - use cleaned text, SSML with natural prosody
    clean_text = clean_for_speech(text)
    ssml_text = f"<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' xml:lang='en-US'><voice name='{voice}'><prosody rate='+8%' pitch='+0%'>{clean_text}</prosody></voice></speak>"
    try:
        com=edge_tts.Communicate(ssml_text,voice)
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
                words.append({"text":tok,"start":t,"duration":0.35,"end":t+0.35}); t+=0.4
        return words
    except Exception as e:
        print(f"TTS SSML failed {e}, fallback to plain")
        com=edge_tts.Communicate(clean_text,voice,rate="+8%")
        audio=b""; words=[]
        async for chunk in com.stream():
            if chunk["type"]=="audio": audio+=chunk["data"]
            elif chunk["type"]=="WordBoundary":
                s=chunk["offset"]/10_000_000; d=chunk["duration"]/10_000_000
                words.append({"text":chunk["text"],"start":s,"duration":d,"end":s+d})
        open(filename,"wb").write(audio)
        return words if words else [{"text":w,"start":i*0.4,"end":i*0.4+0.35} for i,w in enumerate(clean_text.split())]

def generate_audio(text,role,filename,judge_voice_index=None):
    if "JUDGE" in role.upper():
        voice=JUDGE_VOICES[(judge_voice_index or 0)%len(JUDGE_VOICES)]
    elif "GOD TOLD TRUTH" in role.upper(): voice=VOICES["A"]
    elif "SERPENT TOLD TRUTH" in role.upper(): voice=VOICES["B"]
    else: voice=VOICES["A"] if "GOD" in role.upper() else VOICES["B"] if "SERPENT" in role.upper() else VOICES["Moderator"]
    try: return asyncio.run(generate_audio_async(text,voice,filename))
    except: return asyncio.run(generate_audio_async(text,VOICES["Moderator"],filename))

def format_ass_time(s):
    s=max(0.0,float(s)); h=int(s//3600); m=int((s%3600)//60); sec=s%60
    return f"{h}:{m:02d}:{sec:05.2f}"
def ass_escape(t): return str(t).replace("\\","\\\\").replace("{","\\{").replace("}","\\}")

def generate_subtitles(words,filename,scorecard=False):
    # IMPROVED: more synced, more per page so less noticeable desync
    margin_v=90 if scorecard else 200
    font_size=36 if scorecard else 34
    header=f"[Script Info]\nScriptType: v4.00+\nPlayResX: 1920\nPlayResY: 1080\nWrapStyle: 0\nScaledBorderAndShadow: yes\n\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\nStyle: DebateSub,DejaVu Sans,{font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&HCC000000,1,0,0,0,100,100,0,0,1,3.0,1,2,200,200,{margin_v},1\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    if not words:
        open(filename,"w",encoding="utf-8").write(header); return
    clean_words=[{"text":str(w.get("text","")).strip(),"start":float(w["start"]),"end":float(w["end"])} for w in words if str(w.get("text","")).strip()]
    # MORE PER PAGE - 28 words per chunk instead of 18, 4 lines max
    WORDS_PER_CHUNK=28
    chunks=[]; cur=[]
    for w in clean_words:
        cur.append(w)
        # Split on sentence end only if we have at least 12 words, to avoid too many pages
        if str(w["text"]).strip().endswith(('.', '?', '!')) and len(cur)>=14:
            chunks.append(cur); cur=[]
        elif len(cur)>=WORDS_PER_CHUNK:
            chunks.append(cur); cur=[]
    if cur: chunks.append(cur)
    events=[]; last_end=0.0
    for chunk in chunks:
        if not chunk: continue
        # More accurate timing with padding
        s=float(chunk[0]["start"])-0.08
        e=float(chunk[-1]["end"])+0.35  # longer hold to hide desync
        if s<last_end: s=last_end+0.01
        if e<=s: e=s+1.2
        last_end=e
        txt_words=[ass_escape(w["text"]) for w in chunk]
        lines=[]
        # 9 words per line, up to 4 lines = 36 words per page
        for i in range(0,len(txt_words),9):
            lines.append(" ".join(txt_words[i:i+9]))
        if len(lines)>4: lines=lines[:4]
        txt="\\N".join(lines)
        txt=txt.replace("\\\\N","\\N")
        # Slightly longer fade to smooth desync perception
        ass_text="{\\an2\\pos(960,820)\\q2\\fad(80,80)}"+txt
        events.append(f"Dialogue: 0,{format_ass_time(s)},{format_ass_time(e)},DebateSub,,0,0,0,,{ass_text}")
    open(filename,"w",encoding="utf-8").write(header+"\n".join(events)+"\n")

def fallback_visual_plan(text):
    # VERSATILE for any topic.txt - detects topic and returns relevant visuals
    tl=text.lower()
    visuals=[]
    # Genesis visuals
    genesis_kws=[
        ("apple","Apple on branch","red apple hanging from branch, watercolor"),
        ("fruit","Eating fruit","person eating fruit, watercolor"),
        ("tree","Tree in garden","tree with leaves, watercolor"),
        ("garden","Garden of Eden","garden with trees, watercolor"),
        ("serpent","Serpent on branch","snake on branch, watercolor"),
        ("snake","Snake","snake slithering, watercolor"),
        ("god","God light","sun with rays, watercolor"),
        ("eyes opened","Eyes opened","eyes opening, watercolor"),
    ]
    # AI / tech visuals
    ai_kws=[
        ("ai","AI brain","robot brain with circuits, watercolor"),
        ("artificial","Artificial intelligence","robot head, watercolor"),
        ("robot","Robot","robot head, watercolor"),
        ("regulation","Scales of justice","balanced scales, watercolor"),
        ("regulate","Regulation","scales of justice, watercolor"),
        ("bias","Bias warning","warning sign, watercolor"),
        ("algorithm","Algorithm","flowing code blocks, watercolor"),
        ("data","Data","database with data points, watercolor"),
    ]
    # Universe / creator visuals
    cosmos_kws=[
        ("universe","Universe","galaxy with stars, watercolor"),
        ("creator","Creator light","bright sun with rays, watercolor"),
        ("cosmos","Cosmos","galaxy spiral, watercolor"),
        ("big bang","Big Bang","explosion with stars, watercolor"),
        ("galaxy","Galaxy","spiral galaxy, watercolor"),
        ("star","Stars","stars and constellation, watercolor"),
        ("atom","Atom","atom with electrons, watercolor"),
        ("evolution","Evolution","DNA helix, watercolor"),
        ("dna","DNA","dna helix, watercolor"),
        ("fine tuning","Fine tuning","dial with precise tuning, watercolor"),
    ]
    # Generic debate visuals - versatile for any topic
    generic_kws=[
        ("evidence","Evidence","open book with light, watercolor"),
        ("logic","Logic","lightbulb with gears, watercolor"),
        ("truth","Truth","lightbulb glowing, watercolor"),
        ("choice","Choice","fork in road with two paths, watercolor"),
        ("free will","Free will","brain with choice, watercolor"),
        ("determin","Determinism","chain links, watercolor"),
        ("moral","Morality","scales balancing heart and brain, watercolor"),
        ("ethic","Ethics","scales of justice, watercolor"),
        ("justice","Justice","balanced scales, watercolor"),
        ("argument","Debate","two podiums facing, watercolor"),
        ("debate","Debate stage","debate stage with podiums, watercolor"),
        ("question","Question","question mark with light, watercolor"),
    ]
    all_kws = genesis_kws + ai_kws + cosmos_kws + generic_kws
    for kw,label,desc in all_kws:
        if kw in tl and len(visuals)<MAX_VISUALS_PER_SEGMENT:
            idx=tl.find(kw)
            phrase=text[max(0,idx-10):idx+len(kw)+20].strip() or kw
            visuals.append({"phrase":phrase,"label":label,"description":desc,"kind":"concept"})
    
    # If still less than 2, add topic-adaptive defaults
    if len(visuals)<2:
        if any(w in tl for w in ["ai","artificial","robot","regulation","algorithm","tech"]):
            visuals=[
                {"phrase":text[:30],"label":"AI brain","description":"robot brain with circuits, watercolor, transparent","kind":"concept"},
                {"phrase":text[:30],"label":"Scales of justice","description":"balanced scales of justice, watercolor","kind":"concept"},
            ]
        elif any(w in tl for w in ["universe","creator","cosmos","god","exist","big bang","galaxy"]):
            visuals=[
                {"phrase":text[:30],"label":"Universe","description":"galaxy with stars, watercolor","kind":"concept"},
                {"phrase":text[:30],"label":"Atom","description":"atom with orbiting electrons, watercolor","kind":"concept"},
            ]
        elif any(w in tl for w in ["free will","determin","choice","moral","ethic"]):
            visuals=[
                {"phrase":text[:30],"label":"Brain with choice","description":"brain with forked paths, watercolor","kind":"concept"},
                {"phrase":text[:30],"label":"Scales of justice","description":"scales balancing heart and brain, watercolor","kind":"concept"},
            ]
        else:
            # Generic versatile fallback for ANY topic
            visuals=[
                {"phrase":text[:30],"label":"Debate stage","description":"two podiums with watercolor figures, transparent","kind":"concept"},
                {"phrase":text[:30],"label":"Lightbulb idea","description":"lightbulb glowing with idea, watercolor","kind":"concept"},
            ]
    return visuals[:MAX_VISUALS_PER_SEGMENT]

def plan_visuals(text,model):
    prompt=f"Find up to {MAX_VISUALS_PER_SEGMENT} simple visual moments in: {text} JSON [phrase,label]"
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

# WATERCOLOR SCRIBBLE STYLE MATCHING REFERENCE IMAGE - transparent, no black triangles
def draw_watercolor_blob(draw, bbox, color, alpha=180, scribble=False):
    # Draw watercolor-like blob: soft edges with alpha
    x0,y0,x1,y1=bbox
    # Base blob
    draw.ellipse(bbox, fill=(*color, alpha))
    # Add watercolor variation with slightly offset ellipses
    draw.ellipse([x0+5,y0+5,x1-3,y1-3], fill=(*color, alpha-20))
    draw.ellipse([x0+3,y0-2,x1-5,y1+2], fill=(*color, alpha-30))

def draw_scribble_hair(draw, x, y, size):
    # Scribble hair like reference: messy black loops
    cx=x+size*0.5
    cy=y+size*0.15
    # Draw many overlapping loops
    for _ in range(12):
        rx=random.uniform(size*0.15, size*0.35)
        ry=random.uniform(size*0.08, size*0.22)
        x1=cx+random.uniform(-rx, rx)
        y1=cy+random.uniform(-ry, ry)
        x2=x1+random.uniform(-rx*0.5, rx*0.5)
        y2=y1+random.uniform(-ry*0.5, ry*0.5)
        draw.arc([x1,y1,x2,y2], random.randint(0,180), random.randint(180,360), fill=(0,0,0,255), width=2)
    # More loops on top
    for i in range(8):
        ang1=i*45+random.uniform(-10,10)
        ang2=ang1+random.uniform(60,140)
        r1=size*0.18+random.uniform(-5,10)
        r2=size*0.22+random.uniform(-5,10)
        x1=cx+r1*math.cos(math.radians(ang1))
        y1=cy+r1*math.sin(math.radians(ang1))*0.6
        x2=cx+r2*math.cos(math.radians(ang2))
        y2=cy+r2*math.sin(math.radians(ang2))*0.6
        draw.arc([min(x1,x2), min(y1,y2), max(x1,x2), max(y1,y2)], 0, 360, fill=(0,0,0,255), width=2)

def draw_stick_figure_watercolor(draw,x,y,size=80,eating=False):
    # Style matching reference: beige watercolor blobs, scribble hair, thin black outline arms
    # Head blob - beige watercolor
    head_bbox=[x+size*0.2, y, x+size*0.85, y+size*0.55]
    draw_watercolor_blob(draw, head_bbox, (210, 180, 140), alpha=190)
    # Scribble hair on head
    draw_scribble_hair(draw, x+size*0.1, y-5, size*0.6)
    # Body blob - larger beige
    body_bbox=[x+size*0.15, y+size*0.65, x+size*0.85, y+size*1.5]
    draw_watercolor_blob(draw, body_bbox, (210, 180, 140), alpha=185)
    # Arms - thin black lines like reference
    if eating:
        # Arm reaching to mouth
        draw.line([x+size*0.75, y+size*0.8, x+size*1.05, y+size*0.5], fill=(0,0,0,255), width=2)
        # Small red apple in hand
        draw.ellipse([x+size*0.95, y+size*0.4, x+size*1.15, y+size*0.6], fill=(220,20,60,255), outline=(0,0,0,255), width=1)
    else:
        draw.line([x, y+size*0.8, x+size*0.15, y+size*1.3], fill=(0,0,0,255), width=2)
        draw.line([x+size*0.85, y+size*0.8, x+size*1.0, y+size*1.3], fill=(0,0,0,255), width=2)

def create_visual_asset(visual,index):
    filename=f"visual_{index}.gif"
    label=(visual.get('label','')+" "+visual.get('description','')).lower()
    frames=[]
    for f in range(20):
        progress=f/20.0
        frame=Image.new("RGBA",(VISUAL_W,VISUAL_H),(0,0,0,0))
        draw=ImageDraw.Draw(frame)
        
        if "apple" in label or "fruit" in label or "eat" in label:
            # Watercolor apple like reference
            draw.line([VISUAL_W*0.4, 20, VISUAL_W*0.9, 40], fill=(101,67,33,255), width=2)
            for lx,ly in [(VISUAL_W*0.55,22),(VISUAL_W*0.58,28),(VISUAL_W*0.62,35),(VISUAL_W*0.78,32),(VISUAL_W*0.82,38)]:
                leaf_sway = 3*math.sin(progress*2*math.pi + lx*0.1)
                draw.ellipse([lx+leaf_sway, ly, lx+leaf_sway+8, ly+5], fill=(80,160,80,200), outline=(60,120,40,150), width=1)
            swing=10*math.sin(2*math.pi*progress*0.7)
            ax=VISUAL_W*0.65+swing
            ay=70+4*math.sin(2*math.pi*progress*1.2)
            draw.line([ax, 35, ax, ay-10], fill=(0,0,0,255), width=1)
            draw.ellipse([ax-22, ay, ax+22, ay+36], fill=(220,30,50,255), outline=(0,0,0,255), width=2)
            draw.ellipse([ax-12, ay+5, ax-2, ay+18], fill=(255,120,120,180))
            draw.ellipse([ax+5, ay-8, ax+18, ay+2], fill=(80,160,80,200), outline=(0,0,0,180), width=1)
            fig_x=VISUAL_W*0.15
            fig_y=VISUAL_H*0.35
            bob=4*math.sin(progress*2*math.pi*0.8)
            draw_stick_figure_watercolor(draw, fig_x, fig_y+bob, 160, eating=("eat" in label))
            for i in range(3):
                leaf_progress = (progress + i*0.33) % 1.0
                leaf_y = VISUAL_H*0.3 + leaf_progress*VISUAL_H*0.6
                leaf_x = VISUAL_W*0.5 + 60*math.sin(leaf_progress*3*math.pi + i)
                draw.ellipse([leaf_x, leaf_y, leaf_x+18, leaf_y+10], fill=(100,180,100,180), outline=(0,0,0,120), width=1)
                draw.line([leaf_x+2, leaf_y+5, leaf_x+16, leaf_y+5], fill=(0,0,0,100), width=1)
            for _ in range(6):
                dot_x=random.randint(20, VISUAL_W-20)
                dot_y=random.randint(VISUAL_H//2, VISUAL_H-20)
                dot_x+=3*math.sin(progress*3+dot_x*0.1)
                draw.ellipse([dot_x, dot_y, dot_x+3, dot_y+3], fill=(100,180,100,120))

        elif "tree" in label or "garden" in label:
            draw.rectangle([VISUAL_W//2-8, VISUAL_H-100, VISUAL_W//2+8, VISUAL_H-20], fill=(101,67,33,255), outline=(0,0,0,255), width=1)
            rustle=8*math.sin(2*math.pi*progress)
            draw_watercolor_blob(draw, [VISUAL_W//2-70+rustle, VISUAL_H-180, VISUAL_W//2+20+rustle, VISUAL_H-100], (80,160,80), alpha=180)
            draw_watercolor_blob(draw, [VISUAL_W//2-20-rustle, VISUAL_H-200, VISUAL_W//2+70-rustle, VISUAL_H-120], (80,160,80), alpha=170)
            draw.ellipse([VISUAL_W//2-35, VISUAL_H-150, VISUAL_W//2-12, VISUAL_H-125], fill=(220,30,50,255), outline=(0,0,0,255), width=1)
            draw.ellipse([VISUAL_W//2+18, VISUAL_H-160, VISUAL_W//2+40, VISUAL_H-135], fill=(220,30,50,255), outline=(0,0,0,255), width=1)
            fall_y=(progress*VISUAL_H*1.1)%(VISUAL_H+20)-10
            fall_x=VISUAL_W//2+50*math.sin(progress*4)
            draw.ellipse([fall_x, fall_y, fall_x+14, fall_y+8], fill=(100,180,100,180))

        elif "serpent" in label or "snake" in label:
            draw.line([20,100,VISUAL_W-20,110], fill=(101,67,33,255), width=3)
            pts=[]
            for i in range(0,VISUAL_W-40,10):
                pts.append((i+20, 100+12*math.sin((i/20)+progress*3*math.pi)))
            if len(pts)>1:
                draw.line(pts, fill=(80,160,80,255), width=8, joint="curve")
                draw.line(pts, fill=(0,0,0,180), width=1, joint="curve")
            hx,hy=pts[-1] if pts else (VISUAL_W-40,100)
            draw.ellipse([hx,hy-6,hx+16,hy+6], fill=(80,160,80,255), outline=(0,0,0,255), width=1)
            draw.ellipse([hx+10,hy-2,hx+13,hy+1], fill=(0,0,0,255))
            if f%8<4:
                draw.line([hx+16,hy,hx+24,hy-3], fill=(220,30,50,200), width=1)

        elif "ai brain" in label or "robot" in label or "artificial" in label:
            # Versatile AI animation - watercolor robot head with circuits
            cx=VISUAL_W//2
            cy=VISUAL_H//2-20
            pulse=3*math.sin(progress*2*math.pi)
            # Robot head watercolor blob
            draw_watercolor_blob(draw, [cx-60-pulse, cy-60-pulse, cx+60+pulse, cy+40+pulse], (180,180,200), alpha=180)
            draw.rectangle([cx-60, cy-60, cx+60, cy+40], fill=(200,200,220,100), outline=(0,0,0,200), width=2)
            # Eyes glowing
            glow_alpha=150+int(50*math.sin(progress*4*math.pi))
            draw.ellipse([cx-35, cy-25, cx-15, cy-5], fill=(0,200,255,glow_alpha), outline=(0,0,0,180), width=1)
            draw.ellipse([cx+15, cy-25, cx+35, cy-5], fill=(0,200,255,glow_alpha), outline=(0,0,0,180), width=1)
            # Circuit lines
            for i in range(3):
                y_off=i*15-15
                draw.line([cx-50, cy+10+y_off, cx+50, cy+10+y_off], fill=(0,150,200,100), width=1)
                draw.ellipse([cx-50+progress*100%100-20, cy+10+y_off-2, cx-50+progress*100%100-16, cy+10+y_off+2], fill=(0,255,255,200))

        elif "scales" in label or "justice" in label or "regulation" in label:
            # Scales of justice - watercolor versatile for any ethics/regulation topic
            cx=VISUAL_W//2
            # Top pivot
            draw.line([cx, 30, cx, 70], fill=(101,67,33,255), width=2)
            tilt=8*math.sin(progress*2*math.pi*0.6)
            # Beam
            draw.line([cx-80, 70+tilt, cx+80, 70-tilt], fill=(101,67,33,255), width=3)
            # Left pan
            lx=cx-70
            ly=70+tilt
            draw.line([lx, ly, lx-15, ly+40], fill=(0,0,0,150), width=1)
            draw.line([lx, ly, lx+15, ly+40], fill=(0,0,0,150), width=1)
            draw.ellipse([lx-25, ly+40, lx+25, ly+55], fill=(200,180,140,180), outline=(0,0,0,150), width=1)
            # Right pan
            rx=cx+70
            ry=70-tilt
            draw.line([rx, ry, rx-15, ry+40], fill=(0,0,0,150), width=1)
            draw.line([rx, ry, rx+15, ry+40], fill=(0,0,0,150), width=1)
            draw.ellipse([rx-25, ry+40, rx+25, ry+55], fill=(200,180,140,180), outline=(0,0,0,150), width=1)
            # Small weights bobbing
            bob=3*math.sin(progress*2*math.pi)
            draw.ellipse([lx-10, ly+35+bob, lx+10, ly+50+bob], fill=(100,100,100,150))

        elif "universe" in label or "galaxy" in label or "cosmos" in label or "big bang" in label:
            # Versatile cosmos animation - galaxy spiral watercolor
            cx=VISUAL_W//2
            cy=VISUAL_H//2
            # Stars background
            for _ in range(15):
                sx=random.randint(0,VISUAL_W)
                sy=random.randint(0,VISUAL_H)
                twinkle=random.random()
                alpha=int(100+100*math.sin(progress*4*math.pi+twinkle*10))
                draw.ellipse([sx,sy,sx+3,sy+3], fill=(255,255,255,alpha))
            # Galaxy spiral
            for i in range(0,360,20):
                ang=math.radians(i+progress*60)
                r=i*0.35
                x=cx+r*math.cos(ang)
                y=cy+r*math.sin(ang)*0.6
                size=4+r*0.05
                draw.ellipse([x-size,y-size,x+size,y+size], fill=(150,100,200,180), outline=(100,50,150,100), width=1)
                x2=cx+r*math.cos(ang+math.pi)
                y2=cy+r*math.sin(ang+math.pi)*0.6
                draw.ellipse([x2-size,y2-size,x2+size,y2+size], fill=(100,150,200,180), outline=(50,100,150,100), width=1)

        elif "atom" in label or "dna" in label or "evolution" in label:
            # Atom with orbiting electrons - versatile for science topics
            cx=VISUAL_W//2
            cy=VISUAL_H//2
            # Nucleus watercolor
            pulse=4*math.sin(progress*2*math.pi)
            draw_watercolor_blob(draw, [cx-20-pulse, cy-20-pulse, cx+20+pulse, cy+20+pulse], (200,100,100), alpha=180)
            # Electron orbits
            for orbit in range(3):
                ang_offset=orbit*120
                for e in range(2):
                    ang=math.radians(progress*360* (1+orbit*0.3) + ang_offset + e*180)
                    rx=50+orbit*15
                    ry=30+orbit*8
                    ex=cx+rx*math.cos(ang)
                    ey=cy+ry*math.sin(ang)
                    draw.ellipse([ex-6, ey-6, ex+6, ey+6], fill=(100,150,200,200), outline=(0,0,0,150), width=1)
                # Orbit path
                draw.ellipse([cx-(50+orbit*15), cy-(30+orbit*8), cx+(50+orbit*15), cy+(30+orbit*8)], outline=(0,0,0,60), width=1)

        elif "brain" in label or "choice" in label or "fork" in label:
            # Brain with choice paths - versatile for free will, decision topics
            cx=VISUAL_W//2
            cy=VISUAL_H//2-10
            # Brain watercolor
            draw_watercolor_blob(draw, [cx-60, cy-50, cx+60, cy+30], (200,150,150), alpha=180)
            # Wrinkles
            for i in range(3):
                y=cy-30+i*20
                draw.arc([cx-40, y-10, cx+40, y+10], 0, 180, fill=(0,0,0,80), width=1)
            # Forked paths below
            draw.line([cx, cy+30, cx, cy+60], fill=(0,0,0,150), width=2)
            draw.line([cx, cy+60, cx-40, cy+100], fill=(0,0,0,150), width=2)
            draw.line([cx, cy+60, cx+40, cy+100], fill=(0,0,0,150), width=2)
            # Pulsing choice lights
            pulse_left=120+60*math.sin(progress*2*math.pi)
            pulse_right=120+60*math.sin(progress*2*math.pi+math.pi)
            draw.ellipse([cx-50, cy+95, cx-30, cy+115], fill=(100,200,100,int(pulse_left)), outline=(0,0,0,150), width=1)
            draw.ellipse([cx+30, cy+95, cx+50, cy+115], fill=(200,100,100,int(pulse_right)), outline=(0,0,0,150), width=1)

        elif "lightbulb" in label or "idea" in label or "evidence" in label or "book" in label or "logic" in label or "truth" in label:
            # Lightbulb idea - versatile for any topic
            cx=VISUAL_W//2
            cy=VISUAL_H//2-20
            # Bulb watercolor glow
            glow=40+20*math.sin(progress*2*math.pi)
            draw.ellipse([cx-50-glow//2, cy-60-glow//2, cx+50+glow//2, cy+20+glow//2], fill=(255,240,100,60))
            # Bulb
            draw.ellipse([cx-35, cy-50, cx+35, cy+20], fill=(255,250,200,200), outline=(0,0,0,180), width=2)
            # Base
            draw.rectangle([cx-15, cy+20, cx+15, cy+40], fill=(150,150,150,200), outline=(0,0,0,150), width=1)
            # Filament glowing
            filament_alpha=150+80*math.sin(progress*4*math.pi)
            draw.line([cx-10, cy-20, cx, cy-10, cx+10, cy-20], fill=(255,200,0,filament_alpha), width=2)
            # Rays
            for ang in range(-60,61,20):
                rad=math.radians(ang)
                x2=cx+90*math.sin(rad)
                y2=cy-30+90*math.cos(rad)*0.3
                alpha=int(60+40*math.sin(progress*3+ang*0.1))
                draw.line([cx, cy-10, x2, y2], fill=(255,230,0,alpha), width=1)

        elif "god" in label or "creator" in label or "light" in label or "sun" in label:
            cx=VISUAL_W//2
            pulse=4*math.sin(progress*2*math.pi)
            draw.ellipse([cx-28-pulse//2,15-pulse//2,cx+28+pulse//2,71+pulse//2], fill=(255,215,0,220), outline=(0,0,0,255), width=1)
            for ang in range(-50,51,15):
                rad=math.radians(ang)
                x2=cx+200*math.sin(rad); y2=45+200*math.cos(rad)
                alpha=100+int(50*math.sin(progress*3+ang*0.1))
                draw.line([cx,45,x2,y2], fill=(255,215,0,alpha), width=2)
            draw_watercolor_blob(draw, [20,20,100,50], (255,255,255), alpha=180)
            draw_watercolor_blob(draw, [VISUAL_W-100,30,VISUAL_W-20,60], (255,255,255), alpha=180)

        elif "eyes" in label:
            eye_open=8+12*math.sin(progress*math.pi)
            draw.ellipse([VISUAL_W//2-70, VISUAL_H//2-20, VISUAL_W//2-20, VISUAL_H//2+10], fill=(255,255,255,255), outline=(0,0,0,255), width=1)
            draw.ellipse([VISUAL_W//2-55, VISUAL_H//2-8-eye_open//3, VISUAL_W//2-35, VISUAL_H//2+5-eye_open//3], fill=(101,67,33,255), outline=(0,0,0,255), width=1)
            draw.ellipse([VISUAL_W//2+20, VISUAL_H//2-20, VISUAL_W//2+70, VISUAL_H//2+10], fill=(255,255,255,255), outline=(0,0,0,255), width=1)
            draw.ellipse([VISUAL_W//2+35, VISUAL_H//2-8-eye_open//3, VISUAL_W//2+55, VISUAL_H//2+5-eye_open//3], fill=(101,67,33,255), outline=(0,0,0,255), width=1)

        else:
            # GENERIC VERSATILE - debate stage watercolor for ANY topic
            draw.rectangle([0,VISUAL_H-30,VISUAL_W,VISUAL_H], fill=(139,69,19,100))
            draw.rectangle([40,VISUAL_H//2+20,130,VISUAL_H//2+70], fill=(101,67,33,200), outline=(0,0,0,150), width=1)
            draw.rectangle([VISUAL_W-130,VISUAL_H//2+20,VISUAL_W-40,VISUAL_H//2+70], fill=(101,67,33,200), outline=(0,0,0,150), width=1)
            draw_stick_figure_watercolor(draw, 50, VISUAL_H//2-40, 80, eating=False)
            draw_stick_figure_watercolor(draw, VISUAL_W-120, VISUAL_H//2-40, 80, eating=False)
            if f%10<6:
                bx=VISUAL_W//2-40+5*math.sin(progress*4)
                by=VISUAL_H//2-80
                draw.ellipse([bx,by,bx+80,by+35], fill=(255,255,255,200), outline=(0,0,0,150), width=1)

        frames.append(frame)
    
    frames[0].save(filename,format='GIF',save_all=True,append_images=frames[1:],duration=140,loop=0,disposal=2)
    print(f"   Created versatile watercolor animation: {visual.get('label')} ({len(frames)} frames, transparent, no black border)")
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
    d.text((980,225),"INDIVIDUAL JUDGES",fill="#FFD700",font=sub)
    d.text((980,270),"AI",fill="white",font=sml); d.text((1500,270),roles['side_a_label'][:1],fill="#00FFCC",font=sml); d.text((1580,270),roles['side_b_label'][:1],fill="#FF66FF",font=sml)
    d.line([(970,300),(1680,300)],fill=(100,110,140,255),width=2)
    sy=320
    for r in results:
        display = r.get('display_name','?')
        d.text((980,sy),display,fill="white",font=sml)
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
    comp=get_company_name(model)
    pref_label = roles['side_a_label'] if side=="A" else roles['side_b_label']
    other_label = roles['side_b_label'] if side=="A" else roles['side_a_label']
    recent="\n".join(prev[-3:])
    def trim(t,mw=180):
        wl=t.split(); return t if len(wl)<=mw else " ".join(wl[-mw:])
    if side=="A":
        prompt=f"You are {prov} from {comp}. Judging round {rn} about {topic}. You thought {pref_label} stronger than {other_label}. {pref_label}: {trim(ap)} {other_label}: {trim(sk)} Explain in 2-3 sentences why {pref_label} more convincing, specific critique, different from previous judges. Previous: {recent}. Speak naturally as {prov}."
    else:
        prompt=f"You are {prov} from {comp}. Judging round {rn} about {topic}. You thought {pref_label} stronger than {other_label}. {pref_label}: {trim(ap)} {other_label}: {trim(sk)} Explain in 2-3 sentences why {pref_label} more convincing, point out weakness in {other_label}. Be different from previous judges. Previous: {recent}. Speak naturally as {prov}."
    resp=query_openrouter(prompt,model,timeout=30,max_tokens=320,temperature=0.85)
    if resp and len(resp.split())>=15:
        resp=re.sub(r'As .*? to assess,','',resp,flags=re.IGNORECASE).strip()
        resp=re.sub(r'As an? .*? judge,','',resp,flags=re.IGNORECASE).strip()
        resp=re.sub(r'^I am .*? and I.*?[.]','',resp,flags=re.IGNORECASE).strip()
        if len(resp.split())>=10:
            return resp
    if side=="A":
        return f"In round {rn}, I found {pref_label} more persuasive because they stayed close to what the text actually says. They quoted Genesis 3 verse 7 and 22 and explained immediate outcome. {other_label} relied more on ideas not in the chapter. That is why I leaned toward {pref_label}."
    else:
        return f"Looking at round {rn}, {pref_label} made stronger case to me. They pointed out Adam did not die that day, living 930 years, and that eyes opening happened exactly as described. {other_label} tried to redefine death, but plain reading favors {pref_label} on what happened that day."

def build_intro(topic,jc,roles):
    return f"Welcome to the AI Debate Arena. Today, {roles['side_a_label']} faces {roles['side_b_label']} on the question: {topic}. Three rounds, equal time. An independent panel of {jc} AI judges from leading companies will score argument strength, rebuttal quality, and clarity. Let's begin."

def build_judge_intro(judge_model, jc):
    name=get_judge_short_name(judge_model)
    comp=get_company_name(judge_model)
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
    print(f"Roles: {roles['side_a_label']} VS {roles['side_b_label']} - VERSATILE for any topic.txt")
    print(f"Debate engines: {get_judge_short_name(ap_model)} [{provider_from_model(ap_model)}] vs {get_judge_short_name(sk_model)} [{provider_from_model(sk_model)}]")
    judges=choose_judges(avail,(ap_model,sk_model))
    if not judges: judges=FALLBACK_MODELS[:MAX_JUDGES]
    print(f"Judges ({len(judges)}): ONE PER COMPANY - {', '.join(get_judge_short_name(j) for j in judges)}")
    segs=[]; sid=0
    def add_segment(text,role,name,position=None,glow=None,judge_voice_index=None):
        nonlocal sid
        vm=sk_model if "SERPENT" in role.upper() or role=="B" else ap_model
        v=create_segment(text,role,name,topic,sid,vm,position,glow,judge_voice_index); segs.append(v); sid+=1
    
    add_segment(build_intro(topic,len(judges),roles),"Moderator","MODERATOR")
    
    intro_judges = random.sample(judges, min(2, len(judges)))
    for idx,jm in enumerate(intro_judges):
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
    print(f"Versatile: YES - works for ANY topic.txt, roles {roles['side_a_label']} vs {roles['side_b_label']}")
    cleanup_cache()

if __name__=="__main__":
    try: run_debate_pipeline()
    except KeyboardInterrupt: print("Cancelled")
    except Exception as e: print("FAILED"); print(str(e)); raise
