
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

# UNIQUE VOICES - no duplicates
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
    if t and not t[-1] in ".!?":
        t+="."
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
    primary_providers=set(provider_from_model(m) for m in primary)
    excl_ids=set(primary)
    top_providers = {"openai","anthropic","google","meta-llama","mistralai","deepseek","qwen"}
    cands=[m for m in avail if m not in excl_ids and ":free" in m and m.split("/")[0].lower() in top_providers and provider_from_model(m) not in primary_providers]
    if len(cands)<4:
        cands=[m for m in avail if m not in excl_ids and ":free" in m and provider_from_model(m) not in primary_providers]
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
    seen_display=set()
    unique_sel=[]
    for m in sel:
        dname=get_judge_short_name(m)
        if dname not in seen_display:
            unique_sel.append(m)
            seen_display.add(dname)
    print(f"Judges ONE PER COMPANY UNIQUE: {', '.join(f'{provider_from_model(m)} ({get_judge_short_name(m)})' for m in unique_sel)}")
    return unique_sel[:MAX_JUDGES]

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
            if turn_num%2==0:
                return "Genesis chapter 2 verse 17 is really clear when you read it carefully. God says, in the day you eat of it, you shall surely die. The Hebrew is emphatic. It literally says dying you shall die. Now look what the serpent says in chapter 3 verse 4. He says, you shall not surely die. That is a direct contradiction. What actually happens that day? Chapter 3 verse 7 says their eyes were opened and they knew they were naked. They felt shame for the first time. Verse 8 says they hid themselves from God's presence. That hiding is separation, and separation is what the Bible calls death."
            else:
                return "I want you to notice God's generosity in chapter 2 verse 16. He says, you may freely eat of every tree in the garden. Every tree in the garden, with only one limit. That is incredibly generous. Then the serpent twists it in chapter 3 verse 1. He says, did God really say you shall not eat of every tree? He makes God sound stingy, like God is holding out on them. That is classic deception, misrepresenting what someone said. Then he promises, your eyes shall be opened and you will be as gods. Chapter 3 verse 22 says they did become like God in that way. But his first promise, you will not die, was completely false."
        elif round_num==2:
            if turn_num%2==0:
                return "My opponent said the serpent told the truth because they did not drop dead that day. But that misses the point of what death means in this story. Genesis chapter 3 verse 10 says Adam was afraid because he was naked and he hid. Fear and hiding are not full life. Verse 19 says to dust you shall return. Mortality enters the story right there. Verses 23 and 24 say they are driven out of Eden and cherubim block the way to the tree of life. So on the very day they ate, they lost access to eternal life. The process of death started that exact day, just as God warned."
            else:
                return "The argument that they did not die that day ignores how the phrase in the day is used elsewhere in Genesis. In chapter 2 verse 4, it says in the day that the Lord God made the earth and heavens. It means when, not a 24 hour countdown. It is about certainty, not timing. When you eat, death becomes certain. And look, the serpent told a half truth. He said your eyes would be opened, and they were. But he left out the terrible consequence. A half truth that omits the crucial consequence is still a lie. That is what deception looks like."
        else:
            if turn_num%2==0:
                return "Let me pull this together. God warned, in the day you eat you shall surely die. The serpent said, you shall not surely die, you shall be as gods. What actually happened? Their eyes were opened, yes, just as the serpent said. But they also experienced shame, fear, hiding, toil, pain, and they were cut off from the tree of life. That is death in the biblical sense, separation and mortality beginning. Romans chapter 5 verse 12 says sin entered the world and death through sin. The serpent promised no death, but death is now the human condition. God told the truth about the consequence."
            else:
                return "So who told the truth? God said death would come when they ate. The serpent said no death, just enlightenment. The story shows both enlightenment and death entering at the same time. Their eyes were opened, but they also felt shame, blame, cursing, and exile. If the serpent told the whole truth, where is the warning about losing Eden? Where is the warning about returning to dust? He omitted the cost. God did not. God told them the full cost upfront. That is what truth telling looks like, even when it is hard to hear."
    elif "SERPENT TOLD TRUTH" in side_label.upper():
        if round_num==1:
            if turn_num%2==0:
                return "Let us read what the text actually says, not what we think it should say. Genesis chapter 2 verse 17 says God told Adam, in the day you eat of it you shall surely die. In Hebrew, beyom, in the day. The plain sense is that same day. Yet Genesis chapter 5 verse 5 says Adam lived 930 years and then he died. He did not die that day. He lived for centuries after that. The serpent says in chapter 3 verse 4, you shall not surely die. That is exactly what happened. They did not die that day. He also says in verse 5, your eyes shall be opened and you shall be as gods knowing good and evil. Chapter 3 verse 7 says their eyes were opened. God Himself says in verse 22, man has become as one of us to know good and evil. God confirms the serpent was right."
            else:
                return "Think about the Hebrew word yom, which means day. In Genesis chapter 1, evening and morning were the first day, a literal 24 hour period. So when God says in the day you eat you shall die, the natural reading is that same day. Adam did not die that day. The serpent's prediction was more accurate about the immediate outcome. He said you shall not die, and they did not die. He said you shall be as gods knowing good and evil. God says in chapter 3 verse 22, they have become like one of us to know good and evil. Two claims by the serpent, both validated by the story itself. God's threat simply did not happen as stated that day."
        elif round_num==2:
            if turn_num%2==0:
                return "My opponent talks about spiritual death, but the text of Genesis chapters 2 and 3 never mentions spiritual death at all. That is an idea imported from later theology, not from this story. The text mentions nakedness, shame, cursing of the ground, pain in childbirth, hard work, and eventually dust to dust. The test is simple. Did they die that day as God said they would? No, they did not. Did their eyes open as the serpent said they would? Yes, chapter 3 verse 7 says their eyes were opened. On a straightforward reading, the serpent described what would actually happen that day more accurately."
            else:
                return "If God meant they would begin dying, why did He say in the day you shall surely die? Why not say you shall become mortal? That would be clear. And if the serpent lied, why does God confirm his second claim? Chapter 3 verse 22 says, behold the man is become as one of us to know good and evil. That is almost word for word what the serpent promised in verse 5. If the serpent is the liar, why is God echoing his promise? The story presents a real tension that should make us ask who was more accurate about what would happen when they ate the fruit."
        else:
            if turn_num%2==0:
                return "So let us weigh the evidence carefully. God said, in the day you eat you die. The serpent said, you will not die, you will be enlightened, your eyes will be opened. What does the story actually report happened? Their eyes were opened, yes. Enlightenment came, yes. Death that day, no. Adam lives 930 years. God even acknowledges the enlightenment part in chapter 3 verse 22. There is no acknowledgment that they died that day. If we let the text speak for itself, without adding later ideas from other books, the serpent's description of the immediate outcome was more accurate than God's warning."
            else:
                return "The question is not who we want to be truthful, but what the text reports. It reports God threatening death in the day, the serpent promising no death but knowledge, and then it reports knowledge coming and death not coming that day. It reports God Himself saying they have become like us knowing good and evil. The serpent promised that exact thing. So two promises from the serpent, both happen in the story. One threat from God, does not happen that day. On the immediate facts of what happened that day, the serpent was right about what would occur when they ate."
    else:
        if round_num==1:
            return f"On the question of {topic_short}, {side_label} has the stronger case when you look at the evidence carefully. The facts and the logic both point in one direction. The opposing view relies on assumptions that do not hold up under scrutiny. We should prefer the explanation that fits what we actually see in the real world, not just what sounds nice in theory. That is why {side_label} should be preferred in this opening round."
        elif round_num==2:
            return f"My opponent raised some points, but they do not address the core evidence for {side_label}. On {topic_short}, we must ask what the counterexamples actually show when examined closely. {side_label} accounts for those counterexamples, while the other view struggles when tested against real cases. The logic of {side_label} holds together step by step, while the alternative breaks down at key points. That is why rebuttal favors {side_label}."
        else:
            return f"To close on {topic_short}, {side_label} offers a coherent view that fits all the evidence. It defines its terms clearly, it follows logic consistently, and it matches what we observe. The alternative relies on vague claims or it shifts ground when challenged. We should choose the view that is clear, consistent, and well supported by evidence. That is why {side_label} deserves to win this debate."

def generate_turn(side, topic, round_num, turn_num, previous_exchange, model, role_label, role_desc, opponent_label, opponent_desc):
    prev_snip=(previous_exchange or "")[-800:]
    if round_num==1:
        round_focus="Opening round. Establish foundation with clear evidence. Do not repeat, give fresh opening. Use full complete sentences, not phrases."
    elif round_num==2:
        round_focus="Rebuttal round. Directly address opponent's last argument and show why it fails. Bring new angle or evidence not used before. Full sentences."
    else:
        round_focus="Closing round. Pull together strongest points. Show why your view explains evidence better overall. Full complete sentences, memorable."
    prompt=f"You are {role_label} debating live on YouTube about: {topic}. Your position: {role_desc}. Opponent is {opponent_label} who argues: {opponent_desc}. {round_focus} Previous opponent said: {prev_snip}. Write {WORDS_PER_TURN} words as a REAL HUMAN would speak - natural conversational. Use contractions like I'm, don't, can't, it's, that's. Vary sentence length - some short punchy sentences. Then longer ones that explain. Each sentence must be a full sentence with subject and verb, not a fragment or phrase. Use natural pauses with periods. No dashes, no bullet points, no symbols. Quote specific evidence when relevant. Rebut directly. Start immediately with your point, no greeting. Do not repeat arguments already made in previous rounds - bring fresh points. Sound like confident human debater, not textbook. {MIN_TURN_WORDS}-{MAX_TURN_WORDS} words."
    for m in [model]+FALLBACK_MODELS[:3]:
        resp=query_openrouter(prompt,m,max_tokens=800,temperature=0.92)
        if resp and count_words(resp)>=90:
            cleaned=strip_filler(resp)
            cleaned=re.sub(r'\s*-\s*',' . ',cleaned)
            cleaned=re.sub(r'\s+',' ',cleaned).strip()
            cleaned=re.sub(r'\bIn conclusion\b','So',cleaned, flags=re.IGNORECASE)
            cleaned=re.sub(r'\bFurthermore\b','Also',cleaned, flags=re.IGNORECASE)
            cleaned=re.sub(r'\bMoreover\b','And',cleaned, flags=re.IGNORECASE)
            cleaned=re.sub(r'\bIt is important to note\b','Notice',cleaned, flags=re.IGNORECASE)
            if not cleaned.endswith(('.', '!', '?')):
                cleaned+="."
            cleaned=cleaned.replace(" - ", ". ").replace(" -",".")
            cleaned=re.sub(r"https?://\S+"," ",cleaned)
            cleaned=re.sub(r"www\.\S+"," ",cleaned)
            sentences=cleaned.split('. ')
            unique_sents=[]
            for s in sentences:
                if s.strip() and s.strip().lower() not in prev_snip.lower():
                    unique_sents.append(s)
                elif len(unique_sents)<2:
                    unique_sents.append(s)
            cleaned='. '.join(unique_sents)
            if "text, context, and outcome" in cleaned.lower():
                cleaned=re.sub(r'The text, context.*?matter\.','',cleaned,flags=re.IGNORECASE).strip()
            if count_words(cleaned)>=MIN_TURN_WORDS-10:
                return cleaned[:1600]
            extra=query_openrouter("Continue 70 more words same very natural human conversational style, full sentences, contractions, varied: "+cleaned[-250:],m,max_tokens=250,temperature=0.85)
            if extra:
                cleaned+=" "+extra
            return cleaned[:1600]
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
        if not words or len(words)<3:
            raise Exception("No word boundaries")
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
            if words:
                return words
        except:
            pass
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
    # FIX: Much larger chunks + early show to hide lag
    margin_v=90 if scorecard else 190
    font_size=38 if scorecard else 36
    header=f"[Script Info]\nScriptType: v4.00+\nPlayResX: 1920\nPlayResY: 1080\nWrapStyle: 0\nScaledBorderAndShadow: yes\n\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\nStyle: DebateSub,DejaVu Sans,{font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&HCC000000,1,0,0,0,100,100,0,0,1,3.5,1,2,200,200,{margin_v},1\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    if not words:
        open(filename,"w",encoding="utf-8").write(header); return
    clean_words=[{"text":str(w.get("text","")).strip(),"start":float(w["start"]),"end":float(w["end"])} for w in words if str(w.get("text","")).strip()]
    WORDS_PER_CHUNK=55  # Increased from 38 to 55 - much larger chunks, less distracting lag
    chunks=[]; cur=[]
    for w in clean_words:
        cur.append(w)
        # Only split on sentence end if we have substantial chunk, to keep chunks large
        if str(w["text"]).strip().endswith(('.', '?', '!')) and len(cur)>=28:
            chunks.append(cur); cur=[]
        elif len(cur)>=WORDS_PER_CHUNK:
            chunks.append(cur); cur=[]
    if cur: chunks.append(cur)
    events=[]; last_end=0.0
    for chunk in chunks:
        if not chunk: continue
        # Show early, hide late to mask lag - appears before audio, stays after
        s=float(chunk[0]["start"])-0.25  # Show 250ms early
        e=float(chunk[-1]["end"])+0.9  # Hold 900ms longer
        if s<last_end: s=last_end+0.01
        if e<=s: e=s+1.8
        last_end=e
        txt_words=[ass_escape(w["text"]) for w in chunk]
        lines=[]
        for i in range(0,len(txt_words),11):  # 11 words per line
            lines.append(" ".join(txt_words[i:i+11]))
        if len(lines)>5: lines=lines[:5]  # up to 55 words per page
        txt="\\N".join(lines)
        txt=txt.replace("\\\\N","\\N")
        ass_text="{\\an2\\pos(960,810)\\q2\\fad(150,150)}"+txt
        events.append(f"Dialogue: 0,{format_ass_time(s)},{format_ass_time(e)},DebateSub,,0,0,0,,{ass_text}")
    open(filename,"w",encoding="utf-8").write(header+"\n".join(events)+"\n")

def fallback_visual_plan(text):
    # OVERHAULED: Animate many more words including heaven, earth, day, etc. - much less repetitive
    tl=text.lower()
    visuals=[]
    # Genesis expanded - many more words
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
        ("death","Death","skull with time detailed"),
        ("knowledge","Knowledge","book with light detailed"),
        ("wisdom","Wisdom","owl with book detailed"),
    ]
    all_kws = genesis_kws + ai_kws + cosmos_kws + generic_kws
    # Use all occurrences, not just first, for more variety
    for kw,label,desc in all_kws:
        # Find all occurrences for more animations
        start=0
        while len(visuals)<MAX_VISUALS_PER_SEGMENT:
            idx=tl.find(kw, start)
            if idx==-1:
                break
            phrase=text[max(0,idx-15):idx+len(kw)+25].strip() or kw
            # Avoid exact duplicate phrases
            if not any(v["phrase"]==phrase for v in visuals):
                visuals.append({"phrase":phrase,"label":label,"description":desc,"kind":"concept"})
            start=idx+len(kw)
            if len(visuals)>=MAX_VISUALS_PER_SEGMENT:
                break
        if len(visuals)>=MAX_VISUALS_PER_SEGMENT:
            break
    
    # If still less than 3, add diverse defaults based on topic
    if len(visuals)<3:
        if any(w in tl for w in ["ai","artificial","robot","regulation","algorithm","tech"]):
            visuals.extend([
                {"phrase":text[:30],"label":"AI brain","description":"robot brain with circuits, detailed","kind":"concept"},
                {"phrase":text[:30],"label":"Scales of justice","description":"balanced scales detailed","kind":"concept"},
                {"phrase":text[:30],"label":"Computer data","description":"computer with data flowing detailed","kind":"concept"},
            ])
        elif any(w in tl for w in ["heaven","earth","god","creator","universe"]):
            visuals.extend([
                {"phrase":text[:30],"label":"Heaven","description":"heaven with clouds and sun detailed","kind":"concept"},
                {"phrase":text[:30],"label":"Earth","description":"earth with land and water detailed","kind":"concept"},
                {"phrase":text[:30],"label":"Day light","description":"day with bright sun detailed","kind":"concept"},
                {"phrase":text[:30],"label":"Tree in garden","description":"tree with apples detailed","kind":"concept"},
            ])
        else:
            visuals.extend([
                {"phrase":text[:30],"label":"Debate stage","description":"two podiums with figures detailed","kind":"concept"},
                {"phrase":text[:30],"label":"Lightbulb idea","description":"lightbulb glowing detailed","kind":"concept"},
                {"phrase":text[:30],"label":"Evidence book","description":"open book with light detailed","kind":"concept"},
            ])
    # Deduplicate labels to avoid repetition - ensure varied animations
    seen_labels=set()
    unique=[]
    for v in visuals:
        if v["label"] not in seen_labels:
            unique.append(v)
            seen_labels.add(v["label"])
        if len(unique)>=MAX_VISUALS_PER_SEGMENT:
            break
    # If deduplication removed too many, fill with varied
    if len(unique)<MAX_VISUALS_PER_SEGMENT:
        for v in visuals:
            if v not in unique:
                unique.append(v)
            if len(unique)>=MAX_VISUALS_PER_SEGMENT:
                break
    return unique[:MAX_VISUALS_PER_SEGMENT]

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

def draw_watercolor_blob(draw, bbox, color, alpha=180):
    x0,y0,x1,y1=bbox
    draw.ellipse(bbox, fill=(*color, alpha))
    draw.ellipse([x0+4,y0+4,x1-4,y1-4], fill=(*color, alpha-15))
    draw.ellipse([x0+8,y0+2,x1-2,y1-6], fill=(*color, alpha-30))
    draw.ellipse([x0-2,y0+6,x1-8,y1-2], fill=(*color, alpha-40))
    draw.ellipse([x0+10,y0+8,x0+30,y0+25], fill=(255,255,255,60))

def draw_scribble_hair(draw, x, y, size):
    cx=x+size*0.5
    cy=y+size*0.15
    for _ in range(14):
        rx=random.uniform(size*0.15, size*0.35)
        ry=random.uniform(size*0.08, size*0.22)
        x1=cx+random.uniform(-rx, rx)
        y1=cy+random.uniform(-ry, ry)
        x2=x1+random.uniform(-rx*0.5, rx*0.5)
        y2=y1+random.uniform(-ry*0.5, ry*0.5)
        draw.arc([x1,y1,x2,y2], random.randint(0,180), random.randint(180,360), fill=(0,0,0,255), width=2)
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
    head_bbox=[x+size*0.2, y, x+size*0.85, y+size*0.55]
    draw_watercolor_blob(draw, head_bbox, (210, 180, 140), alpha=190)
    draw_scribble_hair(draw, x+size*0.1, y-5, size*0.6)
    body_bbox=[x+size*0.15, y+size*0.65, x+size*0.85, y+size*1.5]
    draw_watercolor_blob(draw, body_bbox, (210, 180, 140), alpha=185)
    if eating:
        draw.line([x+size*0.75, y+size*0.8, x+size*1.05, y+size*0.5], fill=(0,0,0,255), width=2)
        draw.ellipse([x+size*0.95, y+size*0.4, x+size*1.15, y+size*0.6], fill=(220,20,60,255), outline=(0,0,0,255), width=1)
    else:
        draw.line([x, y+size*0.8, x+size*0.15, y+size*1.3], fill=(0,0,0,255), width=2)
        draw.line([x+size*0.85, y+size*0.8, x+size*1.0, y+size*1.3], fill=(0,0,0,255), width=2)

def draw_detailed_human(draw, x, y, size, eating=False, gender="male"):
    head_x=x+size*0.5
    head_y=y+size*0.25
    draw.ellipse([head_x-size*0.22, head_y-size*0.22, head_x+size*0.22, head_y+size*0.22], fill=(255,224,189,255), outline=(0,0,0,255), width=2)
    draw.ellipse([head_x-size*0.12, head_y-size*0.05, head_x-size*0.05, head_y+0.02], fill=(0,0,0,255))
    draw.ellipse([head_x+size*0.05, head_y-size*0.05, head_x+size*0.12, head_y+0.02], fill=(0,0,0,255))
    draw.ellipse([head_x-size*0.11, head_y-0.04, head_x-size*0.07, head_y-0.01], fill=(255,255,255,180))
    draw.ellipse([head_x+size*0.06, head_y-0.04, head_x+size*0.10, head_y-0.01], fill=(255,255,255,180))
    if eating:
        draw.ellipse([head_x-size*0.06, head_y+size*0.08, head_x+size*0.06, head_y+size*0.14], fill=(0,0,0,255))
    else:
        draw.arc([head_x-size*0.08, head_y+size*0.05, head_x+size*0.08, head_y+size*0.12], 20, 160, fill=(0,0,0,255), width=2)
    if gender=="male":
        draw.ellipse([head_x-size*0.25, head_y-size*0.28, head_x+size*0.25, head_y-size*0.05], fill=(101,67,33,255), outline=(0,0,0,200), width=1)
    else:
        draw.ellipse([head_x-size*0.28, head_y-size*0.30, head_x+size*0.28, head_y-0.02], fill=(80,50,20,255), outline=(0,0,0,200), width=1)
        draw.ellipse([head_x-size*0.30, head_y-0.05, head_x-size*0.15, head_y+size*0.15], fill=(80,50,20,255))
        draw.ellipse([head_x+size*0.15, head_y-0.05, head_x+size*0.30, head_y+size*0.15], fill=(80,50,20,255))
    body_top=y+size*0.5
    draw.rectangle([x+size*0.2, body_top, x+size*0.8, body_top+size*0.6], fill=(100,149,237,255), outline=(0,0,0,255), width=2)
    draw.rectangle([x+size*0.22, body_top+size*0.25, x+size*0.78, body_top+size*0.30], fill=(139,69,19,255), outline=(0,0,0,255), width=1)
    if eating:
        draw.line([x+size*0.7, body_top+size*0.1, x+size*1.0, body_top-size*0.05], fill=(0,0,0,255), width=3)
        draw.ellipse([x+size*0.92, body_top-size*0.12, x+size*1.12, body_top+size*0.08], fill=(220,20,60,255), outline=(0,0,0,255), width=2)
        draw.ellipse([x+size*0.98, body_top-size*0.06, x+size*1.06, body_top+0.01], fill=(255,150,150,180))
    else:
        draw.line([x+size*0.05, body_top+size*0.1, x+size*0.20, body_top+size*0.35], fill=(0,0,0,255), width=3)
        draw.line([x+size*0.80, body_top+size*0.1, x+size*0.95, body_top+size*0.05], fill=(0,0,0,255), width=3)
    draw.rectangle([x+size*0.25, body_top+size*0.6, x+size*0.42, body_top+size*0.95], fill=(101,67,33,255), outline=(0,0,0,255), width=1)
    draw.rectangle([x+size*0.58, body_top+size*0.6, x+size*0.75, body_top+size*0.95], fill=(101,67,33,255), outline=(0,0,0,255), width=1)

def create_visual_asset(visual,index):
    filename=f"visual_{index}.gif"
    label=(visual.get('label','')+" "+visual.get('description','')).lower()
    frames=[]
    for f in range(24):
        progress=f/24.0
        frame=Image.new("RGBA",(VISUAL_W,VISUAL_H),(0,0,0,0))
        draw=ImageDraw.Draw(frame)
        if "apple" in label or "fruit" in label or "eat" in label:
            draw.rectangle([0,0,VISUAL_W,80], fill=(135,206,235,30))
            draw.rectangle([VISUAL_W//2-14, VISUAL_H-140, VISUAL_W//2+14, VISUAL_H-20], fill=(101,67,33,255), outline=(0,0,0,255), width=2)
            draw.line([VISUAL_W//2-6, VISUAL_H-120, VISUAL_W//2-6, VISUAL_H-30], fill=(80,50,20,100), width=1)
            draw.line([VISUAL_W//2+6, VISUAL_H-120, VISUAL_W//2+6, VISUAL_H-30], fill=(80,50,20,100), width=1)
            rustle=6*math.sin(2*math.pi*progress*0.8)
            for offset in [(-50, -140, 40), (10, -160, 35), (-30, -180, 30)]:
                ox, oy, sz = offset
                draw.ellipse([VISUAL_W//2+ox+rustle, VISUAL_H+oy, VISUAL_W//2+ox+sz+rustle, VISUAL_H+oy+sz], fill=(34,139,34,255), outline=(0,0,0,200), width=1)
                draw.ellipse([VISUAL_W//2+ox+5+rustle, VISUAL_H+oy+5, VISUAL_W//2+ox+sz-5+rustle, VISUAL_H+oy+sz-5], fill=(60,179,60,180))
            swing=8*math.sin(2*math.pi*progress*0.6)
            for ax_offset, ay_offset in [(-25, -125), (20, -135)]:
                ax=VISUAL_W//2+ax_offset+swing
                ay=VISUAL_H+ay_offset
                draw.line([ax, ay-12, ax, ay], fill=(101,67,33,255), width=2)
                draw.ellipse([ax-14, ay, ax+14, ay+18], fill=(220,20,60,255), outline=(0,0,0,255), width=2)
                draw.ellipse([ax-8, ay+3, ax-2, ay+9], fill=(255,150,150,200))
                draw.ellipse([ax+4, ay-6, ax+10, ay-1], fill=(34,139,34,255), outline=(0,0,0,150), width=1)
            branch_y=50
            draw.line([VISUAL_W*0.3, branch_y, VISUAL_W*0.85, branch_y+10], fill=(101,67,33,255), width=4)
            for lx in [VISUAL_W*0.4, VISUAL_W*0.55, VISUAL_W*0.70]:
                ly=branch_y+random.randint(-5,5)
                draw.ellipse([lx+3*math.sin(progress*3+lx*0.1), ly, lx+10+3*math.sin(progress*3+lx*0.1), ly+6], fill=(60,160,60,200), outline=(0,0,0,120), width=1)
            hang_x=VISUAL_W*0.62+swing
            hang_y=branch_y+15+3*math.sin(progress*2*math.pi)
            draw.line([hang_x, branch_y+5, hang_x, hang_y], fill=(0,0,0,200), width=1)
            draw.ellipse([hang_x-18, hang_y, hang_x+18, hang_y+26], fill=(220,20,60,255), outline=(0,0,0,255), width=2)
            draw.ellipse([hang_x-10, hang_y+4, hang_x-3, hang_y+12], fill=(255,180,180,200))
            serpent_x=VISUAL_W*0.35+10*math.sin(progress*2*math.pi)
            draw.ellipse([serpent_x, branch_y-3, serpent_x+40, branch_y+8], fill=(34,139,34,255), outline=(0,0,0,200), width=1)
            draw.ellipse([serpent_x+30, branch_y-2, serpent_x+45, branch_y+6], fill=(34,139,34,255), outline=(0,0,0,200), width=1)
            draw.ellipse([serpent_x+38, branch_y-1, serpent_x+42, branch_y+2], fill=(0,0,0,255))
            if f%8<4:
                draw.line([serpent_x+45, branch_y+2, serpent_x+52, branch_y], fill=(220,20,60,255), width=1)
            draw_detailed_human(draw, 30, VISUAL_H-160, 90, eating=("eat" in label), gender="male")
            draw_detailed_human(draw, VISUAL_W-130, VISUAL_H-155, 85, eating=False, gender="female")
            draw.rectangle([0, VISUAL_H-20, VISUAL_W, VISUAL_H], fill=(34,139,34,150))
            for gx in range(0, VISUAL_W, 20):
                draw.line([gx, VISUAL_H-20, gx+5, VISUAL_H-28], fill=(20,100,20,120), width=1)
        elif "tree" in label or "garden" in label:
            draw.rectangle([VISUAL_W//2-12, VISUAL_H-110, VISUAL_W//2+12, VISUAL_H-20], fill=(101,67,33,255), outline=(0,0,0,255), width=2)
            rustle=6*math.sin(2*math.pi*progress)
            draw.ellipse([VISUAL_W//2-60+rustle, VISUAL_H-170, VISUAL_W//2+10+rustle, VISUAL_H-100], fill=(34,139,34,255), outline=(0,0,0,255), width=2)
            draw.ellipse([VISUAL_W//2-10-rustle, VISUAL_H-190, VISUAL_W//2+60-rustle, VISUAL_H-120], fill=(34,139,34,255), outline=(0,0,0,255), width=2)
            draw.ellipse([VISUAL_W//2-18, VISUAL_H-155, VISUAL_W//2-2, VISUAL_H-135], fill=(220,20,60,255), outline=(0,0,0,255), width=2)
            draw.ellipse([VISUAL_W//2+15, VISUAL_H-165, VISUAL_W//2+32, VISUAL_H-145], fill=(220,20,60,255), outline=(0,0,0,255), width=2)
            fall_y=(progress*VISUAL_H*1.2)%(VISUAL_H+20)-10
            fall_x=VISUAL_W//2+40*math.sin(progress*4)
            draw.ellipse([fall_x, fall_y, fall_x+12, fall_y+18], fill=(60,180,60,200), outline=(0,0,0,150), width=1)
            draw.rectangle([0, VISUAL_H-15, VISUAL_W, VISUAL_H], fill=(34,139,34,150))
        elif "serpent" in label or "snake" in label:
            draw.line([20,110,VISUAL_W-20,120], fill=(101,67,33,255), width=5)
            pts=[]
            for i in range(0,VISUAL_W-50,8):
                pts.append((i+25,110+16*math.sin((i/22)+progress*3*math.pi)))
            if len(pts)>1:
                draw.line(pts, fill=(34,139,34,255), width=14, joint="curve")
                draw.line(pts, fill=(0,0,0,200), width=2, joint="curve")
                for j in range(0,len(pts),4):
                    x,y=pts[j]
                    draw.ellipse([x-2,y-2,x+2,y+2], fill=(50,180,50,200))
            hx,hy=pts[-1] if pts else (VISUAL_W-40,110)
            draw.ellipse([hx,hy-10,hx+28,hy+10], fill=(34,139,34,255), outline=(0,0,0,255), width=2)
            draw.ellipse([hx+16,hy-3,hx+20,hy+1], fill=(0,0,0,255))
            draw.ellipse([hx+18,hy-5,hx+22,hy-1], fill=(255,255,255,150))
            if f%6<3:
                draw.line([hx+28,hy,hx+38,hy-4], fill=(220,20,60,255), width=2)
                draw.line([hx+28,hy+1,hx+38,hy+4], fill=(220,20,60,255), width=2)
        elif "ai brain" in label or "robot" in label or "artificial" in label:
            cx=VISUAL_W//2
            cy=VISUAL_H//2-20
            pulse=4*math.sin(progress*2*math.pi)
            draw.rectangle([cx-65-pulse, cy-65-pulse, cx+65+pulse, cy+45+pulse], fill=(220,220,230,200), outline=(0,0,0,255), width=2)
            draw.rectangle([cx-55, cy-55, cx+55, cy+35], fill=(200,200,220,150), outline=(0,0,0,150), width=1)
            glow=150+80*math.sin(progress*4*math.pi)
            draw.ellipse([cx-35, cy-25, cx-15, cy-5], fill=(0,200,255,glow), outline=(0,0,0,200), width=2)
            draw.ellipse([cx+15, cy-25, cx+35, cy-5], fill=(0,200,255,glow), outline=(0,0,0,200), width=2)
            draw.rectangle([cx-20, cy+5, cx+20, cy+15], fill=(0,0,0,200))
            for i in range(4):
                y_off=i*10-10
                draw.line([cx-50, cy+8+y_off, cx+50, cy+8+y_off], fill=(0,150,200,100), width=1)
                dot_x=cx-50+(progress*100+i*25)%100
                draw.ellipse([dot_x-3, cy+8+y_off-2, dot_x+3, cy+8+y_off+2], fill=(0,255,255,220))
        elif "scales" in label or "justice" in label or "regulation" in label:
            cx=VISUAL_W//2
            draw.rectangle([cx-5, 20, cx+5, 60], fill=(101,67,33,255), outline=(0,0,0,200), width=1)
            tilt=10*math.sin(progress*2*math.pi*0.5)
            draw.line([cx-85, 60+tilt, cx+85, 60-tilt], fill=(101,67,33,255), width=4)
            draw.ellipse([cx, 55, cx+10, 65], fill=(101,67,33,255), outline=(0,0,0,200), width=1)
            lx=cx-75
            ly=60+tilt
            draw.line([lx, ly, lx-20, ly+45], fill=(0,0,0,150), width=2)
            draw.line([lx, ly, lx+20, ly+45], fill=(0,0,0,150), width=2)
            draw.ellipse([lx-28, ly+45, lx+28, ly+62], fill=(218,165,32,200), outline=(0,0,0,200), width=2)
            draw.ellipse([lx-15, ly+38, lx+15, ly+52], fill=(100,100,100,180), outline=(0,0,0,150), width=1)
            rx=cx+75
            ry=60-tilt
            draw.line([rx, ry, rx-20, ry+45], fill=(0,0,0,150), width=2)
            draw.line([rx, ry, rx+20, ry+45], fill=(0,0,0,150), width=2)
            draw.ellipse([rx-28, ry+45, rx+28, ry+62], fill=(218,165,32,200), outline=(0,0,0,200), width=2)
            draw.ellipse([rx-12, ry+38, rx+12, ry+52], fill=(200,50,50,180), outline=(0,0,0,150), width=1)
        elif "universe" in label or "galaxy" in label or "cosmos" in label or "big bang" in label:
            cx=VISUAL_W//2
            cy=VISUAL_H//2
            for _ in range(20):
                sx=random.randint(0,VISUAL_W)
                sy=random.randint(0,VISUAL_H)
                alpha=int(80+120*math.sin(progress*5+_))
                size=1+random.randint(0,2)
                draw.ellipse([sx,sy,sx+size,sy+size], fill=(255,255,255,alpha))
            for i in range(0,360,15):
                ang=math.radians(i+progress*80)
                r=i*0.38
                x=cx+r*math.cos(ang)
                y=cy+r*math.sin(ang)*0.55
                sz=3+r*0.06
                draw.ellipse([x-sz,y-sz,x+sz,y+sz], fill=(138,43,226,180), outline=(75,0,130,100), width=1)
                x2=cx+r*math.cos(ang+math.pi)
                y2=cy+r*math.sin(ang+math.pi)*0.55
                draw.ellipse([x2-sz,y2-sz,x2+sz,y2+sz], fill=(30,144,255,180), outline=(0,0,139,100), width=1)
            draw.ellipse([cx-8, cy-8, cx+8, cy+8], fill=(255,255,0,220), outline=(255,165,0,200), width=2)
        elif "atom" in label or "dna" in label or "evolution" in label:
            cx=VISUAL_W//2
            cy=VISUAL_H//2
            pulse=3*math.sin(progress*2*math.pi)
            draw.ellipse([cx-18-pulse, cy-18-pulse, cx+18+pulse, cy+18+pulse], fill=(255,100,100,200), outline=(0,0,0,200), width=2)
            draw.ellipse([cx-8, cy-8, cx+8, cy+8], fill=(200,50,50,255))
            for orbit in range(3):
                ang_offset=orbit*120
                rx=45+orbit*14
                ry=28+orbit*8
                for e in range(2):
                    ang=math.radians(progress*360*(1+orbit*0.4)+ang_offset+e*180)
                    ex=cx+rx*math.cos(ang)
                    ey=cy+ry*math.sin(ang)
                    draw.ellipse([ex-5, ey-5, ex+5, ey+5], fill=(100,150,255,220), outline=(0,0,0,150), width=1)
                draw.ellipse([cx-rx, cy-ry, cx+rx, cy+ry], outline=(0,0,0,70), width=1)
        elif "brain" in label or "choice" in label or "fork" in label:
            cx=VISUAL_W//2
            cy=VISUAL_H//2-15
            draw.ellipse([cx-65, cy-45, cx+65, cy+35], fill=(255,182,193,200), outline=(0,0,0,200), width=2)
            draw.ellipse([cx-55, cy-35, cx+55, cy+25], fill=(255,192,203,150))
            for i in range(3):
                y=cy-25+i*18
                draw.arc([cx-40, y-8, cx+40, y+8], 0, 180, fill=(0,0,0,90), width=2)
            draw.line([cx, cy+35, cx, cy+65], fill=(0,0,0,200), width=3)
            draw.line([cx, cy+65, cx-45, cy+110], fill=(0,0,0,200), width=3)
            draw.line([cx, cy+65, cx+45, cy+110], fill=(0,0,0,200), width=3)
            pulse_l=100+100*math.sin(progress*2*math.pi)
            pulse_r=100+100*math.sin(progress*2*math.pi+math.pi)
            draw.ellipse([cx-55, cy+105, cx-30, cy+130], fill=(100,200,100,int(pulse_l)), outline=(0,0,0,200), width=2)
            draw.ellipse([cx+30, cy+105, cx+55, cy+130], fill=(200,100,100,int(pulse_r)), outline=(0,0,0,200), width=2)
            draw.text((cx-48, cy+110), "A", fill="white", font=load_font(14,bold=True))
            draw.text((cx+38, cy+110), "B", fill="white", font=load_font(14,bold=True))
        elif "lightbulb" in label or "idea" in label or "evidence" in label or "book" in label or "logic" in label or "truth" in label:
            cx=VISUAL_W//2
            cy=VISUAL_H//2-25
            glow=35+25*math.sin(progress*2*math.pi)
            draw.ellipse([cx-60-glow, cy-70-glow, cx+60+glow, cy+30+glow], fill=(255,240,100,50))
            draw.ellipse([cx-38, cy-55, cx+38, cy+20], fill=(255,255,200,230), outline=(0,0,0,200), width=2)
            draw.rectangle([cx-18, cy+20, cx+18, cy+42], fill=(150,150,150,230), outline=(0,0,0,200), width=2)
            draw.rectangle([cx-15, cy+28, cx+15, cy+32], fill=(100,100,100,150))
            filament_alpha=120+100*math.sin(progress*5*math.pi)
            draw.line([cx-12, cy-20, cx-4, cy-8, cx+4, cy-8, cx+12, cy-20], fill=(255,200,0,filament_alpha), width=3)
            for ang in range(-70,71,18):
                rad=math.radians(ang)
                x2=cx+85*math.sin(rad)
                y2=cy-15+85*math.cos(rad)*0.35
                alpha=int(50+40*math.sin(progress*4+ang*0.15))
                draw.line([cx, cy-10, x2, y2], fill=(255,230,0,alpha), width=2)
        elif "god" in label or "creator" in label or "light" in label or "sun" in label:
            cx=VISUAL_W//2
            pulse=5*math.sin(progress*2*math.pi)
            draw.ellipse([cx-32-pulse, 12-pulse, cx+32+pulse, 76+pulse], fill=(255,215,0,230), outline=(0,0,0,255), width=2)
            draw.ellipse([cx-12, 25, cx-2, 38], fill=(255,255,180,180))
            for ang in range(-60,61,12):
                rad=math.radians(ang)
                x2=cx+220*math.sin(rad); y2=44+220*math.cos(rad)
                alpha=90+60*math.sin(progress*3+ang*0.12)
                draw.line([cx,44,x2,y2], fill=(255,215,0,alpha), width=3)
            draw.ellipse([25,18,95,48], fill=(255,255,255,220), outline=(0,0,0,150), width=1)
            draw.ellipse([VISUAL_W-95,28,VISUAL_W-25,58], fill=(255,255,255,220), outline=(0,0,0,150), width=1)
            draw.rectangle([0,VISUAL_H-25,VISUAL_W,VISUAL_H], fill=(34,139,34,200))
        elif "eyes" in label:
            eye_open=10+14*math.sin(progress*math.pi)
            draw.ellipse([VISUAL_W//2-75, VISUAL_H//2-22, VISUAL_W//2-15, VISUAL_H//2+12], fill=(255,255,255,255), outline=(0,0,0,255), width=2)
            draw.ellipse([VISUAL_W//2-60, VISUAL_H//2-10-eye_open//3, VISUAL_W//2-30, VISUAL_H//2+6-eye_open//3], fill=(101,67,33,255), outline=(0,0,0,255), width=1)
            draw.ellipse([VISUAL_W//2-50, VISUAL_H//2-2, VISUAL_W//2-40, VISUAL_H//2+4], fill=(0,0,0,255))
            draw.ellipse([VISUAL_W//2+15, VISUAL_H//2-22, VISUAL_W//2+75, VISUAL_H//2+12], fill=(255,255,255,255), outline=(0,0,0,255), width=2)
            draw.ellipse([VISUAL_W//2+30, VISUAL_H//2-10-eye_open//3, VISUAL_W//2+60, VISUAL_H//2+6-eye_open//3], fill=(101,67,33,255), outline=(0,0,0,255), width=1)
            draw.ellipse([VISUAL_W//2+40, VISUAL_H//2-2, VISUAL_W//2+50, VISUAL_H//2+4], fill=(0,0,0,255))
        elif "heaven" in label or "sky clouds" in label:
            # Heaven with detailed clouds and sun rays
            draw.rectangle([0,0,VISUAL_W,VISUAL_H*0.6], fill=(135,206,235,255))
            # Sun with rays
            cx=VISUAL_W*0.7
            cy=60
            pulse=4*math.sin(progress*2*math.pi)
            draw.ellipse([cx-30-pulse, cy-30-pulse, cx+30+pulse, cy+30+pulse], fill=(255,255,0,255), outline=(255,165,0,255), width=3)
            for ang in range(0,360,25):
                rad=math.radians(ang+progress*40)
                x2=cx+180*math.cos(rad)
                y2=cy+180*math.sin(rad)
                alpha=70+40*math.sin(progress*3+ang*0.1)
                draw.line([cx,cy,x2,y2], fill=(255,255,0,alpha), width=3)
            # Detailed clouds with shadows
            for cloud_x, cloud_y in [(80,70),(200,50),(350,90)]:
                drift=10*math.sin(progress*2*math.pi+cloud_x*0.02)
                draw.ellipse([cloud_x+drift, cloud_y, cloud_x+70+drift, cloud_y+30], fill=(255,255,255,255), outline=(200,200,200,150), width=1)
                draw.ellipse([cloud_x+15+drift, cloud_y-10, cloud_x+55+drift, cloud_y+15], fill=(255,255,255,255))
                draw.ellipse([cloud_x+10+drift, cloud_y+5, cloud_x+60+drift, cloud_y+25], fill=(240,240,240,200))
            # Birds
            for i in range(3):
                bx=50+i*60+progress*80
                by=120+20*math.sin(progress*2+ i)
                draw.line([bx,by,bx+8,by+4], fill=(0,0,0,200), width=1)
                draw.line([bx+8,by+4,bx+16,by], fill=(0,0,0,200), width=1)

        elif "earth" in label or "land mountains" in label:
            # Earth with mountains, land, water
            draw.rectangle([0,0,VISUAL_W,VISUAL_H*0.5], fill=(135,206,235,255))
            # Mountains detailed
            draw.polygon([(0,200),(120,80),(240,160),(360,60),(VISUAL_W,140),(VISUAL_W,VISUAL_H),(0,VISUAL_H)], fill=(100,100,100,255), outline=(0,0,0,200), width=2)
            draw.polygon([(120,80),(140,100),(100,110)], fill=(255,255,255,200))  # snow cap
            draw.polygon([(360,60),(380,80),(340,90)], fill=(255,255,255,200))
            # Land with grass texture
            draw.rectangle([0,180,VISUAL_W,VISUAL_H], fill=(34,139,34,255))
            for gx in range(0,VISUAL_W,15):
                gh=10+5*math.sin(gx*0.1+progress*2)
                draw.line([gx,180,gx+3,180-gh], fill=(20,100,20,150), width=2)
            # Water
            draw.ellipse([50,220,200,280], fill=(0,100,200,200), outline=(0,0,0,150), width=1)
            for wx in range(60,190,20):
                wave=3*math.sin(progress*4+wx*0.1)
                draw.line([wx,240+wave,wx+15,240+wave], fill=(255,255,255,100), width=1)

        elif "day" in label and "light" in label:
            # Bright day
            draw.rectangle([0,0,VISUAL_W,VISUAL_H], fill=(135,206,250,255))
            cx=VISUAL_W//2
            cy=80
            pulse=6*math.sin(progress*2*math.pi)
            draw.ellipse([cx-40-pulse, cy-40-pulse, cx+40+pulse, cy+40+pulse], fill=(255,255,0,255), outline=(255,200,0,255), width=3)
            # Radiating light
            for ang in range(0,360,20):
                rad=math.radians(ang+progress*30)
                x2=cx+250*math.cos(rad)
                y2=cy+250*math.sin(rad)
                draw.line([cx,cy,x2,y2], fill=(255,255,150,60), width=4)
            # Lens flare
            draw.ellipse([cx-15, cy-15, cx+15, cy+15], fill=(255,255,200,200))
            draw.rectangle([0,VISUAL_H-30,VISUAL_W,VISUAL_H], fill=(34,139,34,255))

        elif "night" in label:
            # Night sky with moon and stars
            draw.rectangle([0,0,VISUAL_W,VISUAL_H], fill=(10,10,40,255))
            # Moon
            mx=VISUAL_W*0.7
            my=70
            draw.ellipse([mx-28, my-28, mx+28, my+28], fill=(230,230,200,255), outline=(200,200,180,200), width=2)
            draw.ellipse([mx-10, my-5, mx, my+5], fill=(200,200,180,150))  # crater
            draw.ellipse([mx+8, my+8, mx+15, my+15], fill=(200,200,180,120))
            # Stars twinkling varied sizes
            for i in range(25):
                sx=(i*73+int(progress*30))%VISUAL_W
                sy=(i*37)%(VISUAL_H//2+80)
                twinkle=0.5+0.5*math.sin(progress*5+i)
                size=int(1+2*twinkle)
                brightness=int(150+105*twinkle)
                draw.ellipse([sx,sy,sx+size,sy+size], fill=(brightness,brightness,brightness,255))
                if size>2:
                    draw.line([sx+size//2-4, sy+size//2, sx+size//2+4, sy+size//2], fill=(brightness,brightness,brightness,100), width=1)
                    draw.line([sx+size//2, sy+size//2-4, sx+size//2, sy+size//2+4], fill=(brightness,brightness,brightness,100), width=1)

        elif "light rays" in label or "sun bright" in label:
            # Detailed sun rays like God light but more
            cx=VISUAL_W//2
            cy=70
            pulse=5*math.sin(progress*2*math.pi)
            draw.ellipse([cx-35-pulse, cy-35-pulse, cx+35+pulse, cy+35+pulse], fill=(255,255,0,255), outline=(255,165,0,255), width=3)
            draw.ellipse([cx-15, cy-15, cx+15, cy+15], fill=(255,255,200,200))
            for ang in range(-70,71,10):
                rad=math.radians(ang)
                x2=cx+240*math.sin(rad)
                y2=cy+240*math.cos(rad)
                alpha=80+50*math.sin(progress*3+ang*0.15)
                width=2+int(2*math.sin(ang*0.2))
                draw.line([cx,cy,x2,y2], fill=(255,215,0,alpha), width=width)
            # Ground glow
            draw.rectangle([0,VISUAL_H-20,VISUAL_W,VISUAL_H], fill=(255,255,200,100))

        elif "darkness" in label:
            draw.rectangle([0,0,VISUAL_W,VISUAL_H], fill=(5,5,15,255))
            # Single faint light source
            cx=VISUAL_W//2
            cy=VISUAL_H//2
            glow=20+10*math.sin(progress*2*math.pi)
            draw.ellipse([cx-glow, cy-glow, cx+glow, cy+glow], fill=(50,50,80,100))
            # Stars sparse
            for i in range(10):
                sx=random.randint(0,VISUAL_W)
                sy=random.randint(0,VISUAL_H)
                alpha=int(60+60*math.sin(progress*3+i))
                draw.ellipse([sx,sy,sx+2,sy+2], fill=(255,255,255,alpha))

        elif "water waves" in label or "sea waves" in label:
            draw.rectangle([0,0,VISUAL_W,VISUAL_H], fill=(0,100,200,255))
            # Waves with motion
            for y in range(80, VISUAL_H, 35):
                for x in range(0, VISUAL_W, 20):
                    wave_x=x+15*math.sin(progress*3+y*0.05+x*0.02)
                    wave_y=y+8*math.sin(progress*2+x*0.05)
                    draw.ellipse([wave_x, wave_y, wave_x+25, wave_y+8], fill=(0,150,255,150), outline=(255,255,255,80), width=1)
            # Foam
            for i in range(5):
                fx=(i*100+int(progress*50))%VISUAL_W
                fy=100+i*40+int(5*math.sin(progress*3+i))
                draw.ellipse([fx,fy,fx+30,fy+12], fill=(255,255,255,120))

        elif "evening" in label:
            # Orange sunset
            for y in range(0, VISUAL_H):
                ratio=y/VISUAL_H
                r=int(255*(1-ratio*0.3))
                g=int(140+60*(1-ratio))
                b=int(30+20*ratio)
                draw.line([0,y,VISUAL_W,y], fill=(r,g,b,255))
            # Sun setting
            sx=VISUAL_W*0.5+20*math.sin(progress*0.5)
            sy=60+progress*20
            draw.ellipse([sx-35, sy-35, sx+35, sy+35], fill=(255,100,0,255), outline=(255,50,0,200), width=2)
            draw.rectangle([0,VISUAL_H-25,VISUAL_W,VISUAL_H], fill=(50,20,0,200))

        elif "morning" in label:
            # Sunrise
            for y in range(0, VISUAL_H):
                ratio=y/VISUAL_H
                r=int(135+120*(1-ratio))
                g=int(206-50*ratio)
                b=int(235-100*ratio)
                draw.line([0,y,VISUAL_W,y], fill=(r,g,b,255))
            sx=VISUAL_W//2
            sy=VISUAL_H*0.3+20*math.sin(progress*1.5)
            pulse=4*math.sin(progress*2*math.pi)
            draw.ellipse([sx-30-pulse, sy-30-pulse, sx+30+pulse, sy+30+pulse], fill=(255,255,100,255), outline=(255,200,0,200), width=2)
            # Rays through clouds
            for ang in range(-50,51,15):
                rad=math.radians(ang)
                x2=sx+200*math.sin(rad)
                y2=sy+200*math.cos(rad)
                draw.line([sx,sy,x2,y2], fill=(255,255,150,70), width=3)

        elif "adam" in label or "man figure" in label:
            draw.rectangle([0,VISUAL_H-20,VISUAL_W,VISUAL_H], fill=(34,139,34,150))
            draw_detailed_human(draw, VISUAL_W//2-45, VISUAL_H//2-60, 110, eating=("eat" in label), gender="male")
            # Thought bubble?
            if f%16<10:
                bx=VISUAL_W//2+50
                by=VISUAL_H//2-90
                draw.ellipse([bx,by,bx+70,by+35], fill=(255,255,255,230), outline=(0,0,0,200), width=1)
                draw.ellipse([bx+10,by+30,bx+20,by+40], fill=(255,255,255,230), outline=(0,0,0,150), width=1)

        elif "eve" in label or "woman figure" in label:
            draw.rectangle([0,VISUAL_H-20,VISUAL_W,VISUAL_H], fill=(34,139,34,150))
            # Garden background
            draw.rectangle([VISUAL_W//2-10, VISUAL_H-100, VISUAL_W//2+10, VISUAL_H-20], fill=(101,67,33,255), width=2)
            draw.ellipse([VISUAL_W//2-50, VISUAL_H-170, VISUAL_W//2+50, VISUAL_H-100], fill=(34,139,34,255), outline=(0,0,0,200), width=2)
            draw_detailed_human(draw, VISUAL_W//2-40, VISUAL_H//2-55, 105, eating=False, gender="female")

        elif "naked" in label or "shame" in label:
            draw.rectangle([0,VISUAL_H-15,VISUAL_W,VISUAL_H], fill=(34,139,34,150))
            draw_detailed_human(draw, 60, VISUAL_H//2-60, 85, eating=False, gender="male")
            draw_detailed_human(draw, VISUAL_W-150, VISUAL_H//2-60, 85, eating=False, gender="female")
            # Leaves covering
            draw.ellipse([70, VISUAL_H//2+10, 120, VISUAL_H//2+45], fill=(34,139,34,255), outline=(0,0,0,200), width=2)
            draw.ellipse([VISUAL_W-120, VISUAL_H//2+10, VISUAL_W-70, VISUAL_H//2+45], fill=(34,139,34,255), outline=(0,0,0,200), width=2)
            # Blush
            draw.ellipse([75, VISUAL_H//2-35, 90, VISUAL_H//2-25], fill=(255,100,100,100))
            draw.ellipse([VISUAL_W-95, VISUAL_H//2-35, VISUAL_W-80, VISUAL_H//2-25], fill=(255,100,100,100))

        else:
            draw.rectangle([0,VISUAL_H-28,VISUAL_W,VISUAL_H], fill=(139,69,19,120))
            draw.rectangle([35,VISUAL_H//2+15,135,VISUAL_H//2+75], fill=(101,67,33,220), outline=(0,0,0,200), width=2)
            draw.rectangle([VISUAL_W-135,VISUAL_H//2+15,VISUAL_W-35,VISUAL_H//2+75], fill=(101,67,33,220), outline=(0,0,0,200), width=2)
            draw.rectangle([55,VISUAL_H//2+15,75,VISUAL_H//2+35], fill=(0,0,0,150))
            draw.rectangle([VISUAL_W-75,VISUAL_H//2+15,VISUAL_W-55,VISUAL_H//2+35], fill=(0,0,0,150))
            draw_detailed_human(draw, 45, VISUAL_H//2-50, 75, eating=False, gender="male")
            draw_detailed_human(draw, VISUAL_W-125, VISUAL_H//2-50, 75, eating=False, gender="female")
            if f%12<8:
                bx=VISUAL_W//2-50+6*math.sin(progress*4)
                by=VISUAL_H//2-85+2*math.cos(progress*3)
                draw.ellipse([bx,by,bx+90,by+40], fill=(255,255,255,230), outline=(0,0,0,200), width=2)
                draw.polygon([(bx+18,by+30),(bx+8,by+50),(bx+32,by+34)], fill=(255,255,255,230), outline=(0,0,0,200))

        frames.append(frame)
    
    frames[0].save(filename,format='GIF',save_all=True,append_images=frames[1:],duration=110,loop=0,disposal=2)
    print(f"   Created ENGAGING animation: {visual.get('label')} ({len(frames)} frames, detailed, transparent)")
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
        prompt=f"You are {prov} from {comp}. Judging round {rn} about {topic}. You thought {pref_label} stronger than {other_label}. {pref_label}: {trim(ap)} {other_label}: {trim(sk)} Explain in 2-3 full sentences why {pref_label} more convincing, specific, different from previous judges. Previous: {recent}. Speak naturally as {prov}, full sentences."
    else:
        prompt=f"You are {prov} from {comp}. Judging round {rn} about {topic}. You thought {pref_label} stronger than {other_label}. {pref_label}: {trim(ap)} {other_label}: {trim(sk)} Explain in 2-3 full sentences why {pref_label} more convincing, point out weakness in {other_label}. Be different from previous judges. Previous: {recent}. Speak naturally as {prov}, full sentences."
    resp=query_openrouter(prompt,model,timeout=30,max_tokens=320,temperature=0.85)
    if resp and len(resp.split())>=15:
        resp=re.sub(r'As .*? to assess,','',resp,flags=re.IGNORECASE).strip()
        resp=re.sub(r'As an? .*? judge,','',resp,flags=re.IGNORECASE).strip()
        resp=re.sub(r'^I am .*? and I.*?[.]','',resp,flags=re.IGNORECASE).strip()
        if len(resp.split())>=10:
            return resp
    if side=="A":
        return f"In round {rn}, I found {pref_label} more persuasive because they stayed close to what the text actually says. They quoted Genesis 3 verse 7 and 22 and explained the immediate outcome clearly. {other_label} relied more on ideas not in the chapter itself. That is why I leaned toward {pref_label} in this round."
    else:
        return f"Looking at round {rn}, {pref_label} made the stronger case to me. They pointed out that Adam did not die that day, living 930 years, and that eyes opening happened exactly as described in the text. {other_label} tried to redefine death, but the plain reading of what happened that day favors {pref_label}."

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
    print(f"Roles: {roles['side_a_label']} VS {roles['side_b_label']} - VERSATILE")
    print(f"Debate engines: {get_judge_short_name(ap_model)} [{provider_from_model(ap_model)}] vs {get_judge_short_name(sk_model)} [{provider_from_model(sk_model)}]")
    print(f"Voices UNIQUE: GOD={VOICES['A']}, SERPENT={VOICES['B']}, MOD={VOICES['Moderator']}, JUDGES={', '.join(JUDGE_VOICES[:len(avail)])}")
    judges=choose_judges(avail,(ap_model,sk_model))
    if not judges:
        seen_prov=set()
        seen_name=set()
        dedup=[]
        for m in FALLBACK_MODELS:
            prov=provider_from_model(m)
            dname=get_judge_short_name(m)
            if prov not in seen_prov and dname not in seen_name:
                dedup.append(m)
                seen_prov.add(prov)
                seen_name.add(dname)
            if len(dedup)>=MAX_JUDGES:
                break
        judges=dedup
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
            # FIX: Ensure 2 commentaries are from DIFFERENT models/companies, not both ChatGPT
            ja=random.choice(a_res)
            # Filter B to exclude same model and same provider as A
            b_filtered=[r for r in b_res if r["model"]!=ja["model"] and r["provider"]!=ja["provider"]]
            if b_filtered:
                jb=random.choice(b_filtered)
            else:
                # If no different provider in B, pick from B excluding same model
                b_filtered2=[r for r in b_res if r["model"]!=ja["model"]]
                jb=random.choice(b_filtered2) if b_filtered2 else random.choice(b_res)
                # If still same provider, try pick from opposite side A with different provider
                if jb["provider"]==ja["provider"]:
                    alt=[r for r in res if r["provider"]!=ja["provider"] and r["model"]!=ja["model"]]
                    if alt:
                        jb=random.choice(alt)
            ca=generate_panel_commentary(ja["model"],"A",topic,rn,a_full,s_full,pcom,roles); pcom.append(ca)
            # Use actual judge index for voice to match model
            ja_voice_idx = next((i for i,m in enumerate(judges) if m==ja["model"]), 0)
            add_segment(ca,"AI Judge",f"AI JUDGE — {ja['display_name'].upper()} ({ja['provider'].upper()})","center","#3399FF",judge_voice_index=ja_voice_idx)
            cb=generate_panel_commentary(jb["model"],"B",topic,rn,a_full,s_full,pcom,roles); pcom.append(cb)
            jb_voice_idx = next((i for i,m in enumerate(judges) if m==jb["model"]), 1)
            # Ensure different voice index
            if jb_voice_idx==ja_voice_idx:
                jb_voice_idx=(ja_voice_idx+1)%len(JUDGE_VOICES)
            add_segment(cb,"AI Judge",f"AI JUDGE — {jb['display_name'].upper()} ({jb['provider'].upper()})","center","#3399FF",judge_voice_index=jb_voice_idx)
    add_segment(build_outro(len(judges),cum_a,cum_b,roles),"Moderator","MODERATOR")
    stitch_segments(segs,OUTPUT_FILE)
    print(f"\nCOMPLETE: {OUTPUT_FILE} — {cum_a:.1f} vs {cum_b:.1f}")
    cleanup_cache()

if __name__=="__main__":
    try: run_debate_pipeline()
    except KeyboardInterrupt: print("Cancelled")
    except Exception as e: print("FAILED"); print(str(e)); raise
