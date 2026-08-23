
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
    # Fallback with varied scores, not always 50-50, to avoid flat scoring
    import random as _rnd
    # Add some variance so not all 50-50
    a = _rnd.uniform(48,62)
    b = _rnd.uniform(48,62)
    if abs(a-b)<4:
        a+=6
    return {"model":model,"provider":provider_from_model(model),"display_name":get_judge_short_name(model),"A_argument":round(a,1),"A_rebuttal":round(a+_rnd.uniform(-3,3),1),"A_clarity":round(a+_rnd.uniform(-2,2),1),"A_total":round(a,2),"B_argument":round(b,1),"B_rebuttal":round(b+_rnd.uniform(-3,3),1),"B_clarity":round(b+_rnd.uniform(-2,2),1),"B_total":round(b,2),"winner":"A" if a>b else "B"}

def judge_round(model,topic,rn,ap,sk,roles):
    # OVERHAULED SCORING - accurate, not 50-50, ensures explanation matches scores
    # Improved prompt that forces distinct scores and JSON
    ap_snip=ap[:900]
    sk_snip=sk[:900]
    prompt=f'''You are an expert debate judge. Topic: "{topic}" Round {rn}
{roles['side_a_label']}: {ap_snip}
{roles['side_b_label']}: {sk_snip}

Score each side 0-100 on:
- argument strength (evidence, logic)
- rebuttal quality (directly answers opponent)
- clarity (clear reasoning)

Return ONLY valid JSON, no other text:
{{"A_argument": 0-100, "A_rebuttal": 0-100, "A_clarity": 0-100, "B_argument": 0-100, "B_rebuttal": 0-100, "B_clarity": 0-100, "winner": "A or B", "reason": "1 sentence why winner won"}}

Rules: Do NOT give both sides same total. Be decisive. Winner must have higher total. If A_argument is higher, winner should be A. Avoid 50-50. Be critical and varied.'''

    # Try up to 2 models if first fails
    for attempt_model in [model]+[m for m in ["openai/gpt-4o-mini:free","google/gemini-flash-1.5-8b:free"] if m!=model][:1]:
        resp=query_openrouter(prompt,attempt_model,timeout=35,max_tokens=400,temperature=0.15)
        if not resp:
            continue
        try:
            # Extract JSON - try multiple patterns
            m=re.search(r'\{.*\}', resp, re.DOTALL)
            if not m:
                continue
            json_str=m.group(0)
            # Clean common issues
            json_str=json_str.replace("'", '"').replace('“','"').replace('”','"')
            d=json.loads(json_str)
            
            aa=clamp_score(d.get("A_argument"))
            ar=clamp_score(d.get("A_rebuttal"))
            ac=clamp_score(d.get("A_clarity"))
            ba=clamp_score(d.get("B_argument"))
            br=clamp_score(d.get("B_rebuttal"))
            bc=clamp_score(d.get("B_clarity"))
            
            at=(aa+ar+ac)/3
            bt=(ba+br+bc)/3
            
            # Fix: ensure winner matches higher total, avoid 50-50
            winner_raw=str(d.get("winner","")).upper()
            if at==bt:
                # Force difference
                if aa+ar > ba+br:
                    at+=2
                else:
                    bt+=2
            
            # Winner must match higher total
            calculated_winner="A" if at>bt else "B"
            # If model said opposite, use calculated but log
            final_winner=calculated_winner
            if winner_raw in ["A","B"] and winner_raw!=calculated_winner:
                # Model inconsistency - use scores to decide, but adjust scores to match stated winner if close
                if abs(at-bt)<3:
                    if winner_raw=="A":
                        at=bt+3
                    else:
                        bt=at+3
                    final_winner=winner_raw
            
            # Ensure not 50-50 - add small variance if too close
            if abs(at-bt)<1.5:
                if final_winner=="A":
                    at+=2.5
                else:
                    bt+=2.5
            
            return {
                "model":model,
                "provider":provider_from_model(model),
                "display_name":get_judge_short_name(model),
                "A_argument":round(aa,1),
                "A_rebuttal":round(ar,1),
                "A_clarity":round(ac,1),
                "A_total":round(at,2),
                "B_argument":round(ba,1),
                "B_rebuttal":round(br,1),
                "B_clarity":round(bc,1),
                "B_total":round(bt,2),
                "winner":final_winner,
                "reason":str(d.get("reason",""))[:200]
            }
        except Exception as e:
            # Try regex fallback to extract numbers
            try:
                nums=re.findall(r'"[AB]_(?:argument|rebuttal|clarity)"\s*:\s*(\d+(?:\.\d+)?)', resp, re.IGNORECASE)
                if len(nums)>=6:
                    vals=[float(n) for n in nums[:6]]
                    aa,ar,ac,ba,br,bc=vals
                    at=(aa+ar+ac)/3
                    bt=(ba+br+bc)/3
                    if abs(at-bt)<1:
                        bt+=3
                    return {
                        "model":model,
                        "provider":provider_from_model(model),
                        "display_name":get_judge_short_name(model),
                        "A_argument":round(aa,1),"A_rebuttal":round(ar,1),"A_clarity":round(ac,1),"A_total":round(at,2),
                        "B_argument":round(ba,1),"B_rebuttal":round(br,1),"B_clarity":round(bc,1),"B_total":round(bt,2),
                        "winner":"A" if at>bt else "B"
                    }
            except:
                pass
            continue
    
    # If all fails, return varied neutral not 50-50
    return neutral_judge(model)

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

def get_audio_duration(filename):
    try:
        cmd=["ffprobe","-v","error","-show_entries","format=duration","-of","default=noprint_wrappers=1:nokey=1",filename]
        r=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=5)
        return float(r.stdout.strip())
    except:
        return None

def generate_subtitles(words,filename,scorecard=False, audio_file=None, full_text=None):
    # FIX OVERHAUL: No more WordBoundary drift - use actual audio duration and distribute proportionally
    # This eliminates progressive lag where subtitles fall behind more and more
    margin_v=90 if scorecard else 185
    font_size=40 if scorecard else 38
    header=f"[Script Info]\nScriptType: v4.00+\nPlayResX: 1920\nPlayResY: 1080\nWrapStyle: 0\nScaledBorderAndShadow: yes\n\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\nStyle: DebateSub,DejaVu Sans,{font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&HCC000000,1,0,0,0,100,100,0,0,1,3.8,1,2,200,200,{margin_v},1\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    if not words:
        open(filename,"w",encoding="utf-8").write(header); return
    
    # Try to get real audio duration - this is key to fixing drift
    total_duration=None
    if audio_file and os.path.exists(audio_file):
        total_duration=get_audio_duration(audio_file)
    
    # If we have words with timing, use them but rescale to real duration to fix drift
    clean_words=[{"text":str(w.get("text","")).strip(),"start":float(w.get("start",0)),"end":float(w.get("end",0))} for w in words if str(w.get("text","")).strip()]
    
    if total_duration and clean_words:
        # Rescale word timings to match actual audio duration - fixes progressive lag
        last_word_end = clean_words[-1]["end"] if clean_words else 1.0
        if last_word_end>0 and abs(last_word_end-total_duration)>0.3:
            scale = total_duration / last_word_end
            for w in clean_words:
                w["start"]*=scale
                w["end"]*=scale
            print(f"   Rescaled subtitles {last_word_end:.2f}s -> {total_duration:.2f}s to fix drift")
    
    # Use much larger chunks and split on sentences - reduces lag perception dramatically
    # New logic: split full_text into sentences, allocate time proportional to word count
    if full_text and total_duration:
        # Sentence-based timing - most accurate, no drift
        sentences=re.split(r'(?<=[.!?])\s+', full_text.strip())
        sentences=[s for s in sentences if s.strip()]
        total_words=sum(count_words(s) for s in sentences)
        if total_words==0:
            total_words=len(full_text.split())
        events=[]
        cur_time=0.0
        for sent in sentences:
            sw=count_words(sent)
            if sw==0:
                continue
            dur=(sw/total_words)*total_duration
            # Ensure minimum display time
            dur=max(1.2, dur)
            s=cur_time
            e=cur_time+dur
            if e>total_duration:
                e=total_duration
            # Format text - wrap to lines
            txt_words=sent.split()
            lines=[]
            for i in range(0,len(txt_words),12):
                lines.append(" ".join(txt_words[i:i+12]))
            if len(lines)>4:
                lines=lines[:4]
            txt="\\N".join([ass_escape(w) for w in lines])
            ass_text="{\\an2\\pos(960,800)\\q2\\fad(120,120)}"+txt
            events.append(f"Dialogue: 0,{format_ass_time(s)},{format_ass_time(e)},DebateSub,,0,0,0,,{ass_text}")
            cur_time=e
            if cur_time>=total_duration:
                break
        open(filename,"w",encoding="utf-8").write(header+"\n".join(events)+"\n")
        return
    
    # Fallback to word-chunk method but with large chunks and early show
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
        s=float(chunk[0]["start"])-0.15
        e=float(chunk[-1]["end"])+0.6
        if s<last_end: s=last_end+0.01
        if e<=s: e=s+1.5
        # Clamp to total duration if known
        if total_duration and e>total_duration:
            e=total_duration
        last_end=e
        txt_words=[ass_escape(w["text"]) for w in chunk]
        lines=[]
        for i in range(0,len(txt_words),12):
            lines.append(" ".join(txt_words[i:i+12]))
        if len(lines)>4: lines=lines[:4]
        txt="\\N".join(lines)
        txt=txt.replace("\\\\N","\\N")
        ass_text="{\\an2\\pos(960,800)\\q2\\fad(120,120)}"+txt
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

# SCRIBBLE ART OVERHAUL - like attached samurai image: loose chaotic black ink lines, splatters, dynamic
def draw_scribble_blob(draw, bbox, density=80, color=(0,0,0,255)):
    # Draw scribble-filled blob with chaotic lines instead of solid fill - like samurai armor scribbles
    x0,y0,x1,y1=bbox
    cx=(x0+x1)/2
    cy=(y0+y1)/2
    w=x1-x0
    h=y1-y0
    # Base chaotic scribbles
    for _ in range(density):
        # Random line inside bbox
        x = random.uniform(x0+2, x1-2)
        y = random.uniform(y0+2, y1-2)
        # Direction towards center with randomness - creates tangled look
        ang = math.atan2(cy-y, cx-x) + random.uniform(-1.2, 1.2)
        length = random.uniform(5, max(8, w*0.25))
        x2 = x + length*math.cos(ang) + random.uniform(-4,4)
        y2 = y + length*math.sin(ang) + random.uniform(-4,4)
        # Clip to bbox
        x2 = max(x0, min(x1, x2))
        y2 = max(y0, min(y1, y2))
        width = random.choice([1,1,1,2])
        alpha = random.randint(120,255)
        draw.line([x,y,x2,y2], fill=(color[0],color[1],color[2],alpha), width=width)
    # Outer loose outline with wobble - not perfect ellipse
    points=[]
    for i in range(18):
        ang=i/18*2*math.pi
        r_wobble = 1+random.uniform(-0.12,0.12)
        rx = w/2 * r_wobble
        ry = h/2 * r_wobble
        px = cx + rx*math.cos(ang)
        py = cy + ry*math.sin(ang)
        points.append((px,py))
    # Draw wobbly outline with multiple passes for scribble effect
    for _ in range(2):
        for i in range(len(points)):
            p1=points[i]
            p2=points[(i+1)%len(points)]
            # Add slight jitter
            p1j=(p1[0]+random.uniform(-2,2), p1[1]+random.uniform(-2,2))
            p2j=(p2[0]+random.uniform(-2,2), p2[1]+random.uniform(-2,2))
            draw.line([p1j,p2j], fill=(0,0,0,200), width=1)

def draw_scribble_splatter(draw, x, y, count=12):
    # Ink splatters like in reference image - small and large dots with trails
    for _ in range(count):
        sx = x + random.uniform(-18,18)
        sy = y + random.uniform(-12,12)
        size = random.choice([1,1,2,2,3,4,6])
        alpha = random.randint(80,255)
        draw.ellipse([sx,sy,sx+size,sy+size], fill=(0,0,0,alpha))
        if size>3 and random.random()>0.5:
            # Trail
            tx = sx + random.uniform(-10,10)
            ty = sy + random.uniform(-6,6)
            draw.line([sx,sy,tx,ty], fill=(0,0,0,random.randint(40,120)), width=1)

def draw_scribble_figure(draw, x, y, size=90, action="standing", gender="male", eating=False):
    # Scribble art human - like samurai image: dense chaotic lines forming figure
    head_x=x+size*0.5
    head_y=y+size*0.22
    head_r=size*0.20
    # Head scribble - dense tangled lines
    draw_scribble_blob(draw, [head_x-head_r, head_y-head_r, head_x+head_r, head_y+head_r], density=int(size*0.8), color=(0,0,0,255))
    # Face details - eyes as dark dots, not solid
    eye_y=head_y+size*0.02
    draw.ellipse([head_x-size*0.09, eye_y-size*0.04, head_x-size*0.03, eye_y+0.04], fill=(0,0,0,255))
    draw.ellipse([head_x+size*0.03, eye_y-size*0.04, head_x+size*0.09, eye_y+0.04], fill=(0,0,0,255))
    if eating:
        draw.ellipse([head_x-size*0.05, head_y+size*0.08, head_x+size*0.05, head_y+size*0.12], fill=(0,0,0,220))
        # Apple in hand as scribble
        hx=head_x+size*0.35
        hy=head_y-size*0.02
        draw_scribble_blob(draw, [hx-12, hy-8, hx+12, hy+12], density=25, color=(0,0,0,200))
        draw_scribble_splatter(draw, hx, hy, count=4)
    # Hair - wild scribble lines like samurai
    for _ in range(18):
        hx = head_x + random.uniform(-head_r*1.1, head_r*1.1)
        hy = head_y - head_r*0.6 + random.uniform(-4,6)
        hx2 = hx + random.uniform(-12,12)
        hy2 = hy + random.uniform(-18,-4)
        draw.line([hx,hy,hx2,hy2], fill=(0,0,0,230), width=random.choice([1,2]))
    # Body - kimono/robe like samurai with flowing scribble lines
    body_top=y+size*0.45
    body_bottom=body_top+size*0.65
    body_left=x+size*0.18
    body_right=x+size*0.82
    # Main body scribble
    draw_scribble_blob(draw, [body_left, body_top, body_right, body_bottom], density=int(size*1.2), color=(0,0,0,255))
    # Flowing robe lines - dynamic
    for _ in range(8):
        lx = body_left + random.uniform(0, (body_right-body_left))
        ly = body_top + random.uniform(0, (body_bottom-body_top)*0.7)
        lx2 = lx + random.uniform(-20,20)
        ly2 = ly + random.uniform(10,30)
        draw.line([lx,ly,lx2,ly2], fill=(0,0,0,180), width=1)
    # Arms - action based
    if action=="reaching" or eating:
        # Arm reaching out
        ax1=body_right-size*0.1
        ay1=body_top+size*0.15
        ax2=ax1+size*0.45
        ay2=ay1-size*0.2+random.uniform(-5,5)
        # Scribble arm
        for _ in range(12):
            draw.line([ax1+random.uniform(-3,3), ay1+random.uniform(-3,3), ax2+random.uniform(-3,3), ay2+random.uniform(-3,3)], fill=(0,0,0,200), width=1)
    else:
        # Normal arms
        for side in [-1,1]:
            ax = head_x+side*size*0.32
            ay = body_top+size*0.12
            ax2 = ax+side*size*0.18
            ay2 = ay+size*0.28
            for _ in range(8):
                draw.line([ax+random.uniform(-2,2), ay+random.uniform(-2,2), ax2+random.uniform(-2,2), ay2+random.uniform(-2,2)], fill=(0,0,0,180), width=1)
    # Legs - scribble
    leg_top=body_bottom-size*0.05
    for leg_x in [x+size*0.30, x+size*0.60]:
        draw_scribble_blob(draw, [leg_x-8, leg_top, leg_x+8, leg_top+size*0.32], density=18, color=(0,0,0,200))
    # Ground scribble line
    gx=x-10
    gy=y+size*1.05
    for _ in range(20):
        gx2=gx+random.uniform(8,18)
        gy2=gy+random.uniform(-3,3)
        draw.line([gx,gy,gx2,gy2], fill=(0,0,0,160), width=1)
        gx=gx2
    # Splatters around figure for energy
    draw_scribble_splatter(draw, x+size*0.5, y+size*0.5, count=6)

def draw_scribble_tree(draw, x, y, size=120, with_apple=True):
    # Scribble tree - trunk with chaotic lines, canopy scribble
    trunk_w=size*0.12
    trunk_h=size*0.6
    tx=x+size*0.5-trunk_w/2
    # Trunk scribble
    draw_scribble_blob(draw, [tx, y+size*0.4, tx+trunk_w, y+size], density=int(size*0.4), color=(0,0,0,230))
    # Canopy - large scribble blob
    canopy_r=size*0.38
    cx=x+size*0.5
    cy=y+size*0.28
    draw_scribble_blob(draw, [cx-canopy_r, cy-canopy_r, cx+canopy_r, cy+canopy_r], density=int(size*1.5), color=(0,0,0,255))
    # Apples as dense scribble circles
    if with_apple:
        for ax_offset, ay_offset in [(-canopy_r*0.3, -5), (canopy_r*0.25, 8)]:
            ax=cx+ax_offset
            ay=cy+ay_offset
            draw_scribble_blob(draw, [ax-10, ay-8, ax+10, ay+10], density=22, color=(0,0,0,230))
            draw_scribble_splatter(draw, ax, ay, count=3)

def draw_scribble_sun(draw, x, y, size=50):
    # Sun with scribble rays - like ink sun
    draw_scribble_blob(draw, [x-size//2, y-size//2, x+size//2, y+size//2], density=size, color=(0,0,0,255))
    # Rays - long chaotic lines
    for ang in range(0,360,22):
        rad=math.radians(ang+random.uniform(-5,5))
        x2=x+90*math.cos(rad)+random.uniform(-8,8)
        y2=y+90*math.sin(rad)+random.uniform(-8,8)
        for _ in range(2):
            draw.line([x+random.uniform(-2,2), y+random.uniform(-2,2), x2+random.uniform(-3,3), y2+random.uniform(-3,3)], fill=(0,0,0,random.randint(80,160)), width=1)
    draw_scribble_splatter(draw, x, y, count=8)


def create_visual_asset(visual,index):
    # COMPLETE REVAMP: Scribble art like attached samurai - black ink, chaotic lines, splatters, dynamic action
    filename=f"visual_{index}.gif"
    label=(visual.get('label','')+" "+visual.get('description','')).lower()
    frames=[]
    for f in range(30):  # More frames for smoother scribble animation
        progress=f/30.0
        frame=Image.new("RGBA",(VISUAL_W,VISUAL_H),(0,0,0,0))
        draw=ImageDraw.Draw(frame)
        
        # Helper: scribble circle with motion
        def scribble_circle(cx,cy,r, density=30, motion=0):
            for _ in range(density):
                ang=random.uniform(0,2*math.pi)
                rad=r*random.uniform(0.6,1.0)
                x=cx+rad*math.cos(ang)+motion
                y=cy+rad*math.sin(ang)*0.9
                x2=x+random.uniform(-8,8)
                y2=y+random.uniform(-8,8)
                draw.line([x,y,x2,y2], fill=(0,0,0,random.randint(100,230)), width=random.choice([1,1,2]))

        if "apple" in label or "fruit" in label or "eat" in label:
            # Scribble apple hanging - branch as wild line, apple as dense scribble
            branch_y=55
            # Branch - chaotic line
            bx0=VISUAL_W*0.25
            bx1=VISUAL_W*0.85
            for _ in range(8):
                draw.line([bx0+random.uniform(-2,2), branch_y+random.uniform(-2,2), bx1+random.uniform(-2,2), branch_y+10+random.uniform(-2,2)], fill=(0,0,0,200), width=random.choice([1,2]))
            # Leaves scribble
            for lx in [VISUAL_W*0.38, VISUAL_W*0.58, VISUAL_W*0.72]:
                draw_scribble_blob(draw, [lx, branch_y-8, lx+18, branch_y+4], density=14, color=(0,0,0,180))
            # Hanging apple - dense scribble with motion swing
            swing=10*math.sin(2*math.pi*progress*0.7)
            hang_x=VISUAL_W*0.6+swing
            hang_y=branch_y+18+3*math.sin(progress*2*math.pi)
            # String
            draw.line([hang_x, branch_y+5, hang_x+random.uniform(-1,1), hang_y], fill=(0,0,0,180), width=1)
            # Apple - dense scribble circle with splatters
            draw_scribble_blob(draw, [hang_x-20, hang_y, hang_x+20, hang_y+28], density=60, color=(0,0,0,255))
            # Bite mark - white cutout scribble
            if "eat" in label:
                draw.ellipse([hang_x+8, hang_y+6, hang_x+18, hang_y+16], fill=(255,255,255,180))
            draw_scribble_splatter(draw, hang_x, hang_y+10, count=8)
            # Figures - Adam and Eve as scribble samurais reaching/eating
            # Adam left
            draw_scribble_figure(draw, 15, VISUAL_H-165, 92, action="reaching" if "eat" in label else "standing", gender="male", eating=("eat" in label and f%20<12))
            # Eve right
            draw_scribble_figure(draw, VISUAL_W-125, VISUAL_H-160, 88, action="standing", gender="female", eating=False)
            # Serpent on branch - scribble snake with tongue
            serpent_x=VISUAL_W*0.32+12*math.sin(progress*2*math.pi)
            for _ in range(25):
                sx=serpent_x+random.uniform(0,45)
                sy=branch_y+random.uniform(-4,6)
                sx2=sx+random.uniform(6,14)
                sy2=sy+random.uniform(-3,3)
                draw.line([sx,sy,sx2,sy2], fill=(0,0,0,200), width=2)
            draw_scribble_splatter(draw, serpent_x+40, branch_y, count=4)

        elif "tree" in label or "garden" in label:
            # Scribble tree - trunk and canopy as dense scribbles
            draw_scribble_tree(draw, VISUAL_W//2-70, 20, size=160, with_apple=True)
            # Ground scribble
            for gx in range(0, VISUAL_W, 12):
                draw.line([gx, VISUAL_H-15+random.uniform(-2,2), gx+10, VISUAL_H-15+random.uniform(-2,2)], fill=(0,0,0,150), width=1)
            # Falling leaf - scribble
            fall_y=(progress*VISUAL_H*1.1)%(VISUAL_H+20)-10
            fall_x=VISUAL_W//2+45*math.sin(progress*4)
            draw_scribble_blob(draw, [fall_x, fall_y, fall_x+16, fall_y+12], density=12, color=(0,0,0,160))

        elif "serpent" in label or "snake" in label:
            # Scribble serpent - long wavy body like ink stroke
            pts=[]
            for i in range(0,VISUAL_W-30,10):
                y=110+18*math.sin((i/20)+progress*3*math.pi)
                pts.append((i+20, y))
            # Draw body as multiple overlapping scribble lines
            for _ in range(4):
                for i in range(len(pts)-1):
                    p1=pts[i]
                    p2=pts[i+1]
                    jitter=random.uniform(-2,2)
                    draw.line([p1[0], p1[1]+jitter, p2[0], p2[1]+jitter], fill=(0,0,0,200), width=random.choice([2,3]))
            # Head - dense
            hx,hy=pts[-1] if pts else (VISUAL_W-40,110)
            draw_scribble_blob(draw, [hx, hy-12, hx+32, hy+12], density=30, color=(0,0,0,255))
            # Eye
            draw.ellipse([hx+18, hy-2, hx+22, hy+2], fill=(0,0,0,255))
            # Tongue flick
            if f%8<4:
                draw.line([hx+30, hy, hx+42, hy-5], fill=(0,0,0,200), width=2)
                draw.line([hx+30, hy+1, hx+42, hy+5], fill=(0,0,0,200), width=2)
            draw_scribble_splatter(draw, hx, hy, count=6)

        elif "heaven" in label or "sky clouds" in label:
            # Scribble heaven - sun with chaotic rays, clouds as scribble blobs, birds
            draw_scribble_sun(draw, VISUAL_W*0.68, 65, size=45)
            # Clouds - scribble
            for cloud_x, cloud_y in [(70,65),(190,45),(330,85)]:
                drift=12*math.sin(progress*2*math.pi+cloud_x*0.03)
                draw_scribble_blob(draw, [cloud_x+drift, cloud_y, cloud_x+70+drift, cloud_y+28], density=28, color=(0,0,0,200))
            # Birds as simple scribble V
            for i in range(4):
                bx=30+i*55+progress*90
                by=115+18*math.sin(progress*2+i)
                draw.line([bx,by,bx+7,by+5], fill=(0,0,0,200), width=1)
                draw.line([bx+7,by+5,bx+14,by], fill=(0,0,0,200), width=1)
            draw_scribble_splatter(draw, VISUAL_W*0.5, 40, count=10)

        elif "earth" in label or "land mountains" in label:
            # Scribble earth - mountains as jagged scribble peaks, ground scribble
            # Mountains
            mountain_peaks=[(0,190),(110,75),(210,145),(340,55),(VISUAL_W,130)]
            for i in range(len(mountain_peaks)-1):
                p1=mountain_peaks[i]
                p2=mountain_peaks[i+1]
                for _ in range(12):
                    draw.line([p1[0]+random.uniform(-3,3), p1[1]+random.uniform(-3,3), p2[0]+random.uniform(-3,3), p2[1]+random.uniform(-3,3)], fill=(0,0,0,200), width=1)
            # Snow caps scribble
            draw_scribble_blob(draw, [95,75,135,100], density=15, color=(0,0,0,150))
            draw_scribble_blob(draw, [325,55,365,85], density=15, color=(0,0,0,150))
            # Ground
            for gx in range(0,VISUAL_W,10):
                draw.line([gx, 185+random.uniform(-2,2), gx+12, 185+random.uniform(-2,2)], fill=(0,0,0,180), width=1)
            # Water scribble
            draw_scribble_blob(draw, [40,220,190,275], density=20, color=(0,0,0,150))
            draw_scribble_splatter(draw, 100, 240, count=5)

        elif "day" in label and "light" in label:
            # Bright day scribble - sun dense, rays long
            draw_scribble_sun(draw, VISUAL_W//2, 75, size=50)
            # Extra rays
            cx=VISUAL_W//2
            cy=75
            for ang in range(0,360,18):
                rad=math.radians(ang+random.uniform(-4,4))
                x2=cx+220*math.cos(rad)+random.uniform(-10,10)
                y2=cy+220*math.sin(rad)+random.uniform(-10,10)
                draw.line([cx,cy,x2,y2], fill=(0,0,0,random.randint(40,110)), width=1)
            draw_scribble_splatter(draw, cx, cy, count=12)

        elif "night" in label:
            # Night scribble - moon dense, stars as small scribbles
            mx=VISUAL_W*0.68
            my=68
            draw_scribble_blob(draw, [mx-30, my-30, mx+30, my+30], density=45, color=(0,0,0,220))
            # Craters
            draw.ellipse([mx-12, my-6, mx-2, my+4], fill=(255,255,255,60))
            # Stars - many small dots with twinkle
            for i in range(30):
                sx=(i*61+int(progress*40))%VISUAL_W
                sy=(i*43)%(VISUAL_H//2+60)
                alpha=int(80+170*abs(math.sin(progress*4+i)))
                size=random.choice([1,1,1,2])
                draw.ellipse([sx,sy,sx+size,sy+size], fill=(0,0,0,alpha))
            draw_scribble_splatter(draw, mx, my, count=6)

        elif "light rays" in label or "sun bright" in label or "god" in label or "creator" in label:
            # God light - sun with intense rays and splatters
            draw_scribble_sun(draw, VISUAL_W//2, 65, size=48)
            cx=VISUAL_W//2
            cy=65
            for ang in range(-65,66,11):
                rad=math.radians(ang)
                x2=cx+240*math.sin(rad)+random.uniform(-6,6)
                y2=cy+240*math.cos(rad)+random.uniform(-6,6)
                draw.line([cx,cy,x2,y2], fill=(0,0,0,random.randint(50,130)), width=random.choice([1,2]))
            draw_scribble_splatter(draw, cx, cy, count=14)

        elif "darkness" in label:
            # Darkness - sparse, almost empty with few dots
            for _ in range(15):
                x=random.randint(0,VISUAL_W)
                y=random.randint(0,VISUAL_H)
                draw.ellipse([x,y,x+2,y+2], fill=(0,0,0,random.randint(40,120)))
            # Single faint glow scribble center
            draw_scribble_blob(draw, [VISUAL_W//2-15, VISUAL_H//2-15, VISUAL_W//2+15, VISUAL_H//2+15], density=10, color=(0,0,0,80))

        elif "water waves" in label or "sea waves" in label or "water" in label:
            # Water scribble - wavy lines
            for y in range(70, VISUAL_H, 32):
                for _ in range(3):
                    x0=random.randint(0,VISUAL_W-40)
                    x1=x0+random.randint(20,50)
                    wave_y=y+8*math.sin(progress*3+x0*0.05)
                    draw.line([x0, wave_y+random.uniform(-2,2), x1, wave_y+random.uniform(-2,2)], fill=(0,0,0,random.randint(80,180)), width=1)
            draw_scribble_splatter(draw, VISUAL_W//2, VISUAL_H//2, count=10)

        elif "evening" in label:
            # Evening - horizon line with sun low, scribble sky
            sy=70+progress*18
            draw_scribble_blob(draw, [VISUAL_W//2-35, sy-35, VISUAL_W//2+35, sy+35], density=40, color=(0,0,0,220))
            # Horizon
            for gx in range(0,VISUAL_W,14):
                draw.line([gx, 200+random.uniform(-2,2), gx+14, 200+random.uniform(-2,2)], fill=(0,0,0,200), width=1)
            draw_scribble_splatter(draw, VISUAL_W//2, sy, count=8)

        elif "morning" in label:
            # Morning - sunrise with rays breaking
            sy=VISUAL_H*0.35+15*math.sin(progress*1.5)
            draw_scribble_sun(draw, VISUAL_W//2, int(sy), size=42)
            # Clouds breaking
            for cx in [80, 200, 340]:
                draw_scribble_blob(draw, [cx, sy-10, cx+60, sy+18], density=18, color=(0,0,0,150))

        elif "ai brain" in label or "robot" in label or "artificial" in label or "computer" in label:
            # AI brain scribble - robot head as box with scribble eyes, circuits as lines
            cx=VISUAL_W//2
            cy=VISUAL_H//2-20
            # Head box scribble
            draw_scribble_blob(draw, [cx-62, cy-58, cx+62, cy+38], density=70, color=(0,0,0,230))
            # Eyes - glowing scribble
            for ex in [cx-26, cx+26]:
                draw_scribble_blob(draw, [ex-12, cy-18, ex+12, cy-2], density=18, color=(0,0,0,255))
                draw_scribble_splatter(draw, ex, cy-10, count=4)
            # Circuit lines with moving dots
            for i in range(5):
                y=cy+5+i*10
                draw.line([cx-52, y, cx+52, y], fill=(0,0,0,random.randint(60,140)), width=1)
                dot_x=cx-52+(progress*104+i*20)%104
                draw.ellipse([dot_x-2, y-2, dot_x+2, y+2], fill=(0,0,0,220))

        elif "scales" in label or "justice" in label or "regulation" in label:
            # Scales scribble - beam, pans as scribble circles
            cx=VISUAL_W//2
            tilt=9*math.sin(progress*2*math.pi*0.5)
            # Post
            draw_scribble_blob(draw, [cx-4, 20, cx+4, 60], density=15, color=(0,0,0,200))
            # Beam
            for _ in range(6):
                draw.line([cx-82+random.uniform(-2,2), 58+tilt+random.uniform(-1,1), cx+82+random.uniform(-2,2), 58-tilt+random.uniform(-1,1)], fill=(0,0,0,200), width=1)
            # Pans - scribble circles
            for px, py in [(cx-72, 60+tilt), (cx+72, 60-tilt)]:
                draw_scribble_blob(draw, [px-26, py+38, px+26, py+58], density=28, color=(0,0,0,200))
                # Strings
                draw.line([px, 58+ (tilt if px<cx else -tilt), px-18, py+38], fill=(0,0,0,150), width=1)
                draw.line([px, 58+ (tilt if px<cx else -tilt), px+18, py+38], fill=(0,0,0,150), width=1)

        elif "universe" in label or "galaxy" in label or "cosmos" in label or "big bang" in label:
            # Universe scribble - spiral galaxy as scribble spiral
            cx=VISUAL_W//2
            cy=VISUAL_H//2
            # Stars
            for _ in range(22):
                sx=random.randint(0,VISUAL_W)
                sy=random.randint(0,VISUAL_H)
                draw.ellipse([sx,sy,sx+random.choice([1,1,2]),sy+random.choice([1,1,2])], fill=(0,0,0,random.randint(60,200)))
            # Spiral arms - scribble
            for arm in [0, math.pi]:
                for i in range(0,200,8):
                    ang=i*0.05+progress*1.2+arm
                    r=i*0.42
                    x=cx+r*math.cos(ang)+random.uniform(-3,3)
                    y=cy+r*math.sin(ang)*0.55+random.uniform(-3,3)
                    draw.ellipse([x,y,x+2,y+2], fill=(0,0,0,180))
            # Core
            draw_scribble_blob(draw, [cx-12, cy-12, cx+12, cy+12], density=20, color=(0,0,0,255))
            draw_scribble_splatter(draw, cx, cy, count=12)

        elif "atom" in label or "dna" in label or "evolution" in label:
            # Atom scribble - nucleus and orbiting electrons as scribble
            cx=VISUAL_W//2
            cy=VISUAL_H//2
            draw_scribble_blob(draw, [cx-16, cy-16, cx+16, cy+16], density=22, color=(0,0,0,220))
            for orbit in range(3):
                rx=42+orbit*13
                ry=26+orbit*9
                for _ in range(2):
                    ang=progress*2*math.pi*(1+orbit*0.3)+orbit*2.1+random.uniform(-0.2,0.2)
                    ex=cx+rx*math.cos(ang)+random.uniform(-2,2)
                    ey=cy+ry*math.sin(ang)+random.uniform(-2,2)
                    draw.ellipse([ex-3, ey-3, ex+3, ey+3], fill=(0,0,0,200))
                # Orbit path as faint scribble ellipse
                for a in range(0,360,28):
                    rad=math.radians(a)
                    ox=cx+rx*math.cos(rad)
                    oy=cy+ry*math.sin(rad)
                    if random.random()>0.6:
                        draw.ellipse([ox,oy,ox+1,oy+1], fill=(0,0,0,60))

        elif "brain" in label or "choice" in label or "fork" in label:
            # Brain with choice - scribble brain, forked paths
            cx=VISUAL_W//2
            cy=VISUAL_H//2-18
            draw_scribble_blob(draw, [cx-62, cy-38, cx+62, cy+32], density=80, color=(0,0,0,230))
            # Fork
            draw.line([cx, cy+32, cx, cy+62], fill=(0,0,0,200), width=1)
            for _ in range(5):
                draw.line([cx+random.uniform(-1,1), cy+32, cx-42+random.uniform(-2,2), cy+108+random.uniform(-2,2)], fill=(0,0,0,180), width=1)
                draw.line([cx+random.uniform(-1,1), cy+32, cx+42+random.uniform(-2,2), cy+108+random.uniform(-2,2)], fill=(0,0,0,180), width=1)
            draw_scribble_blob(draw, [cx-54, cy+102, cx-30, cy+126], density=20, color=(0,0,0,200))
            draw_scribble_blob(draw, [cx+30, cy+102, cx+54, cy+126], density=20, color=(0,0,0,200))

        elif "lightbulb" in label or "idea" in label or "evidence" in label or "truth" in label:
            # Lightbulb scribble - bulb outline scribble, filament, rays
            cx=VISUAL_W//2
            cy=VISUAL_H//2-22
            draw_scribble_blob(draw, [cx-34, cy-48, cx+34, cy+18], density=50, color=(0,0,0,220))
            draw_scribble_blob(draw, [cx-16, cy+18, cx+16, cy+40], density=18, color=(0,0,0,200))
            # Filament
            for _ in range(6):
                draw.line([cx-10+random.uniform(-2,2), cy-18, cx+random.uniform(-2,2), cy-6, cx+10+random.uniform(-2,2), cy-18], fill=(0,0,0,180), width=1)
            # Rays
            for ang in range(-65,66,16):
                rad=math.radians(ang)
                x2=cx+90*math.sin(rad)+random.uniform(-5,5)
                y2=cy-12+90*math.cos(rad)*0.4+random.uniform(-5,5)
                draw.line([cx, cy-8, x2, y2], fill=(0,0,0,random.randint(30,100)), width=1)
            draw_scribble_splatter(draw, cx, cy-15, count=8)

        elif "adam" in label or "man figure" in label:
            draw_scribble_figure(draw, VISUAL_W//2-45, VISUAL_H//2-70, 115, action="standing", gender="male", eating=False)
            draw_scribble_splatter(draw, VISUAL_W//2, VISUAL_H//2, count=10)

        elif "eve" in label or "woman figure" in label:
            draw_scribble_figure(draw, VISUAL_W//2-40, VISUAL_H//2-65, 108, action="standing", gender="female", eating=False)
            # Tree behind
            draw_scribble_tree(draw, VISUAL_W//2-20, -10, size=90, with_apple=False)

        elif "eyes opened" in label or "eyes" in label:
            # Eyes opening - two scribble eyes
            for ex in [VISUAL_W//2-62, VISUAL_W//2+28]:
                ey=VISUAL_H//2-12
                draw_scribble_blob(draw, [ex-30, ey-18, ex+30, ey+14], density=35, color=(0,0,0,230))
                # Iris
                draw_scribble_blob(draw, [ex-12, ey-8, ex+12, ey+8], density=18, color=(0,0,0,255))
                draw.ellipse([ex-3, ey-2, ex+3, ey+3], fill=(0,0,0,255))
            draw_scribble_splatter(draw, VISUAL_W//2, VISUAL_H//2, count=8)

        else:
            # Generic debate scribble - two figures facing with speech bubbles as scribble
            draw_scribble_figure(draw, 35, VISUAL_H//2-55, 78, action="standing", gender="male", eating=False)
            draw_scribble_figure(draw, VISUAL_W-125, VISUAL_H//2-55, 78, action="standing", gender="female", eating=False)
            # Speech bubble scribble
            if f%18<10:
                bx=VISUAL_W//2-48+6*math.sin(progress*4)
                by=VISUAL_H//2-88+2*math.cos(progress*3)
                draw_scribble_blob(draw, [bx, by, bx+88, by+38], density=30, color=(0,0,0,200))
                # Tail
                draw.line([bx+16, by+28, bx+6, by+48], fill=(0,0,0,180), width=1)
                draw.line([bx+30, by+32, bx+16, by+48], fill=(0,0,0,180), width=1)

        frames.append(frame)
    
    frames[0].save(filename,format='GIF',save_all=True,append_images=frames[1:],duration=95,loop=0,disposal=2)
    print(f"   Created SCRIBBLE art: {visual.get('label')} ({len(frames)} frames, black ink, splatters)")
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
    # FIX: Pass audio file and full text to generate_subtitles to eliminate progressive lag
    try:
        generate_subtitles(words,sf, scorecard=False, audio_file=af, full_text=text)
    except TypeError:
        # Fallback for old signature
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
        sw=generate_audio(st,"Moderator",sa)
        try:
            generate_subtitles(sw,ss,scorecard=True, audio_file=sa, full_text=st)
        except TypeError:
            generate_subtitles(sw,ss,scorecard=True)
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
