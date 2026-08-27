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
MAX_EMOJIS_PER_SEGMENT = 6
EMOJI_W = 180
EMOJI_H = 180
USED_EMOJIS = set()
USED_ARGUMENTS = set()
USED_PHRASES = set()
USED_KEYWORDS = set()
USED_JUDGE_EXPLANATIONS = set()

# === UNIQUE VOICE CAST - NO DUPLICATES EVER ===
VOICE_POOL = [
    "en-US-BrianMultilingualNeural",
    "en-GB-SoniaNeural",
    "en-AU-NatashaNeural",
    "en-US-JennyNeural",
    "en-GB-RyanNeural",
    "en-US-GuyNeural",
    "en-GB-LibbyNeural",
    "en-US-DavisNeural",
    "en-AU-WilliamNeural",
    "en-CA-ClaraNeural",
    "en-US-AriaNeural",
    "en-US-JennyNeural",
]

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
CAST_VOICE_ASSIGNMENT = {}

def assign_unique_voices(roles):
    global CAST_VOICE_ASSIGNMENT, JUDGE_VOICE_MAP
    CAST_VOICE_ASSIGNMENT = {}
    CAST_VOICE_ASSIGNMENT[roles['side_a_label']] = VOICE_POOL[0]
    CAST_VOICE_ASSIGNMENT[roles['side_b_label']] = VOICE_POOL[1]
    CAST_VOICE_ASSIGNMENT["MODERATOR"] = VOICE_POOL[2]
    remaining = VOICE_POOL[3:]
    JUDGE_VOICE_MAP = {}
    print(f"Voice cast: {roles['side_a_label']}={VOICE_POOL[0]}, {roles['side_b_label']}={VOICE_POOL[1]}, MODERATOR={VOICE_POOL[2]}")
    print(f"Judge pool: {remaining[:7]}")
    return CAST_VOICE_ASSIGNMENT

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
    if "does god exist" in tl or "does a god exist" in tl or "existence of god" in tl or tl.strip()=="does god exist?" or "is there a god" in tl:
        return {"side_a_label": "GOD EXISTS","side_a_desc": "Argues God exists, uses cosmological, teleological, moral arguments","side_b_label": "GOD DOES NOT EXIST","side_b_desc": "Argues God does not exist, uses problem of evil, lack of evidence, parsimony",}
    if "god exist" in tl:
        return {"side_a_label": "GOD EXISTS","side_a_desc": "Defends existence of God","side_b_label": "NO GOD","side_b_desc": "Argues against existence of God",}
    prompt='Topic: "'+topic+'" Return ONLY JSON: {"side_a_label":"FOR label 2-4 words uppercase","side_a_desc":"one sentence for side","side_b_label":"AGAINST label 2-4 words uppercase","side_b_desc":"one sentence against"} Make labels short, opposite, clear.'
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
    if " vs " in tl or " versus " in tl:
        parts=re.split(r"\s+vs\.?\s+|\s+versus\s+", topic, flags=re.IGNORECASE)
        if len(parts)>=2:
            a=parts[0][:20].strip().upper()
            b=parts[1][:20].strip().upper()
            return {"side_a_label": a or "SIDE A","side_a_desc": f"Argues for {parts[0]}","side_b_label": b or "SIDE B","side_b_desc": f"Argues for {parts[1]}",}
    return {"side_a_label": "AFFIRMATIVE","side_a_desc": f"Argues FOR {topic} with evidence","side_b_label": "NEGATIVE","side_b_desc": f"Argues AGAINST {topic} with evidence",}

def strip_filler(text):
    for pat in [r"^(ladies and gentlemen[,.]?\s*)",r"^(my friends[,.]?\s*)",r"^(well[,.]?\s*)",r"^(thank you[,.]?\s*)"]:
        text=re.sub(pat,"",text,flags=re.IGNORECASE).strip()
    return text

def generate_fallback_debate(side_label, topic, round_num, turn_num):
    tl=(topic or "").lower()
    topic_short = topic[:130] if len(topic)>130 else topic
    sl=side_label.upper()
    if "does god exist" in tl or "does a god" in tl or "existence of god" in tl or "is there a god" in tl or ("god exist" in tl and "serpent" not in tl):
        if "GOD EXISTS" in sl or "AFFIRMATIVE" in sl or "THEIST" in sl or "FOR" in sl or sl=="GOD" or "GOD" in sl and "NOT" not in sl and "NO" not in sl:
            if "NOT" in sl or "NO GOD" in sl or "NEGATIVE" in sl:
                pass
            else:
                theist_templates={
                    (1,1): "I want to start with something we all experience, that everything that begins to exist has a cause. The universe began to exist about 13.8 billion years ago, that's the standard big bang cosmology. If the universe began, it needs a cause beyond itself, something timeless, spaceless, enormously powerful. That sounds a lot like what people mean by God.",
                    (1,2): "Look at fine tuning. The constants of physics are balanced on a razor's edge. If gravity were slightly stronger, stars would burn out too fast for life, if the cosmological constant were different by one part in 10 to the 120, galaxies never form. Physicists call this uncanny. The best explanation is not luck, it's design.",
                    (1,3): "Think about consciousness and moral experience. We all feel that some things are truly right and wrong, not just preferences, like torturing a child for fun is really wrong. Where does that objective moral value come from if we're just atoms bumping around? And consciousness itself, the fact you are aware right now, doesn't fit neatly into pure materialism.",
                    (1,4): "There is also the contingency argument. Why does the universe exist at all? It could have not existed. Everything we see is contingent, it depends on something else. There has to be a necessary foundation, something that exists by its own nature and explains everything else. That necessary being is what classical theists call God.",
                    (2,1): "My opponent brings up the problem of evil, and it's a serious point. But the existence of evil doesn't disprove God, it assumes a standard of good that needs grounding. If there's no God, evil is just what we don't like, not objectively wrong. And free will explains a lot, God creates free creatures who can choose love, which requires possibility of choosing harm.",
                    (2,2): "They say there's no evidence, but that's not quite right. We have philosophical arguments, cosmic fine tuning, moral experience, religious experience across cultures, and historical claims. You might not find it convincing, but it's not zero evidence. And lack of evidence isn't evidence of absence, especially if God is not the kind of thing you'd detect like a planet.",
                    (2,3): "If God doesn't exist, you still have to explain why the universe is intelligible, why math works so beautifully to describe physics, why we have rational minds that can do science at all. Einstein said the most incomprehensible thing is that the universe is comprehensible. Theism says that's expected, a rational mind behind reality made minds that can understand it.",
                    (2,4): "Consider personal experience, not as proof for everyone, but as evidence for the person. Millions of people across history report encountering something transcendent, a presence, a moral transformation. You can dismiss each as psychology, but the sheer breadth and transformative effect suggests something real, not just wishful thinking.",
                    (3,1): "Let me pull it together. We have a universe that began, finely tuned for life, governed by elegant laws, containing conscious beings who perceive objective moral values and long for meaning. Each piece alone might have an alternative, but together they form a cumulative case. Theism gives one simple explanation that ties them together.",
                    (3,2): "Who has the more complete explanation? The atheist says it's all brute fact, it just is, no deeper reason. The theist says there is a necessary, intelligent, good foundation that explains why there's something rather than nothing, why it's ordered, why we are moral and conscious. If you're looking for ultimate explanation, God exists is the best explanation.",
                    (3,3): "Think about what we are as humans. We ask why, we seek purpose, we love, we feel guilt and awe when we look at stars. If God doesn't exist, those longings are accidental byproducts of evolution with no fulfillment. If God does exist, they make sense, we are made for relationship with our source.",
                    (3,4): "Final point, I'm not saying believe because it's comforting. I'm saying the evidence of a beginning, fine tuning, consciousness, morality, reason, and religious experience all point in same direction. It's an inference to best explanation. The universe looks like it has mind behind it, not just matter.",
                }
                key = (round_num, turn_num if turn_num<=4 else ((turn_num-1)%4+1))
                return theist_templates.get(key, theist_templates[(3,4)])
        # atheist / no god
        atheist_templates={
            (1,1): "I want to start with the burden of proof. The claim God exists is an extraordinary claim about an invisible, all powerful being who created everything. Extraordinary claims need extraordinary evidence, and we just don't have it. We have old books, personal feelings, and philosophical arguments that have been challenged for centuries. Science explains the universe without needing to add God.",
            (1,2): "Look at the problem of evil. Children die of cancer, earthquakes kill thousands, predators tear animals apart slowly. If there is an all powerful, all loving God, why would he design a world with so much gratuitous suffering? Evolution explains suffering as byproduct of natural selection, not design. An all loving designer wouldn't build a system where survival requires suffering.",
            (1,3): "Think about divine hiddenness. If God wants relationship with us, why is he so hidden? Why do billions of sincere seekers find nothing? Why does prayer not work any better than chance in controlled studies? Why is revelation so geographically concentrated? A loving God who wants to be known would make himself more obvious.",
            (1,4): "The God hypothesis has been shrinking. We used to think God made lightning, now we know it's electricity. We used to think God made species, now we know evolution does. Every time we learn more, we need God less to explain gaps. This is the God of the gaps problem. The universe looks exactly like you'd expect if there's no God.",
            (2,1): "My opponent says fine tuning proves design, but that misunderstands probability. We don't know if constants could be different, maybe there's a multiverse with many universes and we happen to be in one that allows life, anthropic principle. And invoking a designer to explain order is not simple, God would be infinitely more complex and tuned than the universe.",
            (2,2): "They talk about objective morality needing God, but that's backwards. We can explain morality through evolution and social contracts, empathy helped groups survive. And if morality comes from God, is something good because God says so, or does God say so because it's good? If first, morality is arbitrary. If second, good exists independent of God.",
            (2,3): "Consciousness is mysterious, yes, but mystery is not evidence for God. We didn't understand lightning once, we didn't say therefore God, we investigated. Neuroscience is making progress linking brain activity to experience. Saying God did it stops inquiry. And if God is pure mind without brain, how does that work?",
            (2,4): "Religious experience is not reliable. People of different religions have contradictory experiences, Hindu experiences Krishna, Christian experiences Jesus, they can't all be true. And experiences are heavily influenced by culture and expectation. Without independent verification, personal experience can't be trusted as evidence for a cosmic being.",
            (3,1): "Let me bring it together. We have a universe that science explains increasingly well without God, suffering that makes no sense if a loving God designed it, a God who stays hidden when he should be obvious, and arguments for God that have serious logical problems. The simplest explanation that fits all the data is that God does not exist.",
            (3,2): "Who has the better explanation for what we actually see? Theist has to add extra assumptions, God is timeless, spaceless, invisible, yet personal and caring, intervenes but not detectably, allows evil for mysterious reasons. Atheism says reality is what we see, no extra invisible layer. Occam's razor says don't multiply entities beyond necessity.",
            (3,3): "Final thought, I'm not saying I know for sure there's no God, I'm saying there's no good reason to believe there is. Belief should be proportioned to evidence, and the evidence for God is weak, contradictory, and better explained by psychology and culture. Until better evidence comes, the honest position is to withhold belief.",
            (3,4): "If God exists and wants us to believe, he could make it clear, appear, heal amputees, write in the sky. He doesn't. Instead we get ancient texts with contradictions and moral problems, and a world that looks exactly indifferent. The lack of clear evidence where we would expect it is itself evidence of absence.",
        }
        key = (round_num, turn_num if turn_num<=4 else ((turn_num-1)%4+1))
        return atheist_templates.get(key, atheist_templates[(3,4)])
    if "GOD TOLD TRUTH" in sl:
        god_templates = {
            (1,1): "I want to start with what God actually said in Genesis 2 verse 17. He said in the day you eat of it you shall surely die, and the Hebrew is moth tamuth, an emphatic form meaning dying you shall die. It is about certainty, not just timing. The serpent directly contradicts that in chapter 3 verse 4 when he says you shall not surely die. So who is telling the truth? Look at what happened that very day. They experienced shame, fear, hiding, and separation from God. That is the beginning of death.",
            (1,2): "Look at how generous God is in Genesis 2 verse 16. He says you may freely eat of every tree in the garden. Every single tree except one. That is incredibly generous. Then the serpent twists it in chapter 3 verse 1 and asks, did God really say you shall not eat of every tree? He makes God sound stingy when God was actually abundant.",
            (1,3): "Genesis 2 verse 17 is a clear warning, and Genesis 3 verses 7 through 10 shows that warning coming true relationally that same day. Their eyes were opened but what came with it? Shame, fear, and hiding from God's presence. Verse 10 says I was afraid because I was naked and I hid myself. Fear and hiding are not fullness of life.",
            (1,4): "The phrase in the day you eat appears in Genesis 2 verse 4 as well, in the day the Lord God made earth and heavens, meaning when, not a 24 hour timer. It is about when you eat, death becomes certain. And Genesis 3 verse 19 says to dust you shall return, and verses 23 to 24 say they were driven out and cherubim blocked the way to the tree of life.",
            (2,1): "My opponent says they did not drop dead that day, so God lied, but that misses the biblical meaning of death. Genesis 3 verse 10 says Adam was afraid and hid. That is relational break. Verse 19 says to dust you shall return. Mortality entered. Verses 23 and 24 say they were driven out of Eden and cherubim guarded the tree of life.",
            (2,2): "The argument that they did not die that day ignores how in the day is used elsewhere. Chapter 2 verse 4 says in the day the Lord made the heavens and earth. It means when. It is about certainty, not a countdown. When you eat, death becomes certain. And the serpent told a half truth. He said your eyes would be opened, and they were, but he left out the painful consequences.",
            (2,3): "If the serpent told the whole truth, where is the warning about pain, toil, and exile? Genesis 3 verses 16 to 19 lists real curses, pain in childbirth, thorns and thistles, sweat and hard work, and finally dust. The serpent said nothing about that. He only said you shall be as gods. Chapter 3 verse 22 says they did become like God knowing good and evil, but at what terrible cost?",
            (2,4): "Think about the tree of life in Genesis 3 verses 22 to 24. God says, lest he put forth his hand and take also of the tree of life and live forever, therefore He drove the man out and placed cherubim to keep the way. On that day they lost the chance to live forever. That is death beginning.",
            (3,1): "Let us pull it together. God warned that in the day you eat you shall surely die. The serpent said you shall not surely die, you shall be as gods. What actually happened? There was enlightenment, but also shame, blame, fear, toil, pain, and being cut off from the tree of life. That is death in the biblical sense.",
            (3,2): "Who told the more complete truth? God said death would come when they ate, and He was generous with every tree but one. The serpent said no death, just godlikeness, and made God sound restrictive. The story shows both knowledge and death entering at once, eyes opened but also shame, cursing, and exile.",
            (3,3): "Consider the character of the speakers. God creates, provides abundantly, and warns clearly to protect. The serpent questions in 3 verse 1, did God really say, that plants doubt, then denies in verse 4, you shall not surely die, and then appeals to desire in verse 5, you shall be as gods. That pattern is classic temptation.",
            (3,4): "One last point about the Hebrew. Moth tamuth in Genesis 2 verse 17 is infinitive absolute, it emphasizes certainty, you shall surely die. The serpent says lo moth temuthun, you shall not surely die, directly negating God's emphasis. What happened? They did die, not as an instant drop, but relationally that day, and they began dying physically, and eventually returned to dust.",
        }
        key = (round_num, turn_num if turn_num<=4 else ((turn_num-1)%4+1))
        return god_templates.get(key, god_templates[(3,4)])
    elif "SERPENT TOLD TRUTH" in sl:
        serpent_templates = {
            (1,1): "I want us to read what the text actually says, not what we think it should say. Genesis 2 verse 17 has God saying, in the day you eat you shall surely die, and the plain sense of in the day is that same day. Yet Genesis 5 verse 5 says Adam lived nine hundred and thirty years and then died. He did not die that day. He lived for centuries afterward. The serpent says in chapter 3 verse 4, you shall not surely die, and that is exactly what happened.",
            (1,2): "Think about the Hebrew word yom, day. In Genesis 1, evening and morning were the first day, a literal day. So when God says in the day you eat you shall surely die, the natural reading is that same day. Adam did not die that day. The serpent's prediction about the immediate outcome was more accurate. He said you shall not die, and they did not.",
            (1,3): "Genesis 2 verse 17 threatens death in the day, but Genesis 3 verse 6 says the woman saw the tree was good for food and pleasant to the eyes and desired to make one wise. She ate and gave to her husband and he ate. Verse 7 says the eyes of both were opened and they knew they were naked. That is exactly what the serpent promised in verse 5, your eyes shall be opened.",
            (1,4): "If God meant spiritual death, why did He not say spiritual death? The text of Genesis 2 and 3 never mentions spiritual death. That is later theology read back into the story. The text mentions nakedness, shame, cursing of the ground, pain, hard work, and dust to dust. The test is simple. Did they die that day as God said? No.",
            (2,1): "My opponent talks about spiritual death, but Genesis 2 and 3 never uses that phrase at all. That is an idea imported from later theology, not from this narrative. The text mentions nakedness, shame, cursing, pain in childbirth, hard work, and eventually returning to dust. The simple question is, did they die that day as God said they would? No, they did not.",
            (2,2): "If God meant they would begin dying, why did He say in the day you shall surely die? Why not say you shall become mortal? That would be clear. And if the serpent lied, why does God confirm his second claim? Chapter 3 verse 22 says, behold, the man has become as one of us to know good and evil. That is almost word for word what the serpent promised in verse 5.",
            (2,3): "Consider Genesis 3 verse 22. God says man has become as one of us to know good and evil. That is exactly what the serpent said would happen in verse 5. If the serpent is the father of lies, why is God confirming his prediction? And where is the death that day? Chapter 3 verse 20 says Adam called his wife Eve, the mother of all living. Chapter 4 verse 1 says Adam knew Eve and she conceived.",
            (2,4): "Look at chapter 3 verse 13. God asks the woman, what is this that you have done? The woman says, the serpent beguiled me and I did eat. She does not say the serpent lied about death. She says he beguiled her. Beguiled means tricked, but tricked about what? If he lied about death and they did not die, she would have evidence he lied. But the text never says she realized he lied about death.",
            (3,1): "Let us weigh the evidence carefully. God said, in the day you eat you die. The serpent said, you will not die, you will be enlightened, your eyes will be opened. What does the story actually report happened? Their eyes were opened, yes. Enlightenment came, yes. Death that day, no. Adam lives nine hundred and thirty years.",
            (3,2): "The question is not who we want to be truthful, but what the text reports. It reports God threatening death in the day, the serpent promising no death but knowledge, and then it reports knowledge coming and death not coming that day. It reports God Himself saying they have become like us knowing good and evil. The serpent promised that exact thing.",
            (3,3): "Final assessment. Genesis presents two contradictory predictions. God says in the day you eat, dying you shall die. The serpent says you shall not dying die, but your eyes opened, as gods knowing good and evil. What happens? Verse 7, eyes opened. Verse 22, God says man has become as one of us knowing good and evil. The serpent's two predictions both occur.",
            (3,4): "If we are honest about the text, Genesis 3 is not about who lied, but about who gave a more accurate description of what would happen when they ate. God said death that day. The serpent said no death, but knowledge and godlikeness. Knowledge and godlikeness happen that day, confirmed by God in chapter 3 verse 22. Death that day does not happen.",
        }
        key = (round_num, turn_num if turn_num<=4 else ((turn_num-1)%4+1))
        return serpent_templates.get(key, serpent_templates[(3,4)])
    # Versatile generic
    if "GOD" in sl and "NOT" not in sl and "NO" not in sl:
        versatile_for={
            (1,1): f"When we look at {topic_short}, I think the case for {side_label} starts with a basic observation about how we explain things. Everything we see has an explanation, either in itself or in something else, and {topic_short} fits that pattern. The idea behind {side_label} gives us a deeper why, not just a how.",
            (1,2): f"Let me put {topic_short} in everyday terms. If {side_label} is right, we would expect to see order, intelligibility, and real value in the world, and we do. We can do science, we understand math, we feel moral urgency. {side_label} says that's not an accident, there's a foundation that makes sense of it.",
            (1,3): f"Think about {topic_short} from first principles. What are we trying to explain? Not just one fact, but a cluster, existence, order, consciousness, value. {side_label} offers one unified story that covers all of them. My opponent's view needs a separate patch for each, and when you need many patches, it starts to look ad hoc.",
            (1,4): f"On {topic_short}, I think we should ask what we would predict if {side_label} were true. We would expect a world that is rational, discoverable, with creatures who can reason and care about truth. And that's exactly what we find. That predictive success matters.",
            (2,1): f"My opponent says {topic_short} can be explained without {side_label}, but I don't think their alternative really explains, it just describes. Saying it just happened or it's just natural law doesn't give a deeper reason why laws exist at all. {side_label} pushes one step further and asks what grounds the laws.",
            (2,2): f"They argue that {side_label} adds complexity, but actually {side_label} is simpler in the sense of ultimate explanation. Instead of many unexplained brute facts, you have one necessary foundation that explains the many. That's what we do in science too, we seek a unified theory.",
            (2,3): f"If {side_label} were false, you'd expect a very different world. You'd expect either nothing at all, or chaos with no laws, or minds that can't trust their reasoning because evolution only cares about survival, not truth. But we have laws, we have reliable reasoning, we have a universe that is stunningly intelligible.",
            (2,4): f"Consider personal experience and history. Across cultures, people report encountering transcendence, moral transformation, awe that changes lives. You can call each one illusion, but the pattern is vast. {side_label} makes sense of that pattern as contact with something real.",
            (3,1): f"Let me pull this together for {topic_short}. We have existence itself, order, consciousness, morality, reason, and experience. Individually each might have a naturalistic story, but together they form a cumulative case. {side_label} ties them into one coherent picture.",
            (3,2): f"Who has the more complete explanation for {topic_short}? {side_label} says there is a deeper foundation that is necessary, rational, and good, and that explains why the world is the way it is. The alternative says it's all contingent, no ultimate reason.",
            (3,3): f"Think about what it means to be human in this debate about {topic_short}. We ask why, we seek purpose, we love and reason and feel moral guilt. If {side_label} is false, those deep features are accidental side effects pointing to nothing. If {side_label} is true, they make sense as clues to our origin.",
            (3,4): f"Final point on {topic_short}, I'm not asking you to believe because it's comforting. I'm saying look at the total evidence, the beginning, the fine tuning, the intelligibility, the moral and conscious life we live. It looks like mind is fundamental, not just matter.",
        }
        key = (round_num, turn_num if turn_num<=4 else ((turn_num-1)%4+1))
        return versatile_for.get(key, versatile_for[(3,4)])
    else:
        versatile_against={
            (1,1): f"When we look at {topic_short}, I think we have to start with what we actually have evidence for. The claim behind {side_label} is a big one, it says more exists than we can see or measure. But big claims need good evidence, and when I look for independent, testable evidence for {topic_short}, I don't find it.",
            (1,2): f"Let me put {topic_short} plainly. If {side_label} were true, we would expect the world to look different than it does. We would expect clear, unmistakable signs, not ambiguity and hiddenness. But what we see looks exactly like you'd expect if {side_label} were not true, vast, mostly empty, indifferent, with suffering built into how nature works.",
            (1,3): f"Think about parsimony for {topic_short}. We have two stories. One says reality is what we observe, no extra invisible realm. The other adds an extra realm that is timeless, spaceless, undetectable, yet somehow does things. Occam's razor says don't multiply entities beyond necessity.",
            (1,4): f"On {topic_short}, the history matters. We used to invoke extra explanations for lightning, disease, planetary motion, and each time we learned the natural explanation. That's the God of the gaps pattern. {topic_short} looks like another gap that shrinks as we learn more.",
            (2,1): f"My opponent says {topic_short} explains order or value, but I think that gets it backwards. Order comes from natural laws, which we describe, not prescribe. And value comes from evolved social beings who need cooperation to survive. You don't need to add {side_label} to get those.",
            (2,2): f"They say {topic_short} gives meaning, but that is an appeal to consequences, not evidence. Wanting something to be true doesn't make it true. And the meaning it gives comes at a cost, you have to believe despite hiddenness, despite suffering that seems pointless, despite contradictory revelations across cultures.",
            (2,3): f"If {side_label} were true, you would expect convergence, everyone discovering the same thing. But for {topic_short}, we see divergence, different cultures come to opposite conclusions, and those conclusions track geography and upbringing. That pattern suggests {topic_short} is a cultural product, not a discovery.",
            (2,4): f"Consider how we know things. We trust methods that are public, repeatable, checkable. The evidence offered for {side_label} is mostly private, anecdotal, or philosophical arguments that have been debated for centuries without consensus. If {topic_short} were as solid as gravity, we wouldn't still be debating it after thousands of years.",
            (3,1): f"Let me bring it together for {topic_short}. We have a world that science explains better and better without adding {side_label}, suffering that doesn't fit a good extra purpose, hiddenness where we would expect clarity, and arguments for {side_label} that have serious logical issues. The best fit for all that data is that {side_label} is a human story.",
            (3,2): f"Who has the better explanation for {topic_short} as we actually observe it? {side_label} has to add special pleas, it is timeless but acts in time, invisible but personal, all powerful but can't make its existence clear. My view says reality is what we see, no special pleading needed.",
            (3,3): f"Final thought on {topic_short}, I'm not claiming certainty, I'm claiming proportion. Belief should match evidence, and the evidence for {side_label} is weak, conflicting, and better explained by psychology and culture. Until better evidence arrives, the honest move is to not affirm {side_label}.",
            (3,4): f"If {side_label} were true and important, you would expect it to be obvious, like the sun. Instead we get ambiguity, ancient texts with contradictions, and a world that looks indifferent. The absence of clear evidence where we would expect it is itself evidence of absence.",
        }
        key = (round_num, turn_num if turn_num<=4 else ((turn_num-1)%4+1))
        return versatile_against.get(key, versatile_against[(3,4)])

USED_JUDGE_EXPLANATIONS = set()
def generate_panel_commentary(model,side,topic,rn,ap,sk,prev,roles):
    prov=get_judge_short_name(model); comp=get_company_name(model)
    pref_label = roles['side_a_label'] if side=="A" else roles['side_b_label']
    other_label = roles['side_b_label'] if side=="A" else roles['side_a_label']
    recent="\n".join(prev[-4:]); used_expl = "\n".join(list(USED_JUDGE_EXPLANATIONS)[-8:])
    def trim(t,mw=220): 
        # Keep more context and preserve specific phrases
        wl=t.split()
        if len(wl)<=mw:
            return t
        # Keep first 80 and last 140 to preserve hook and conclusion
        return " ".join(wl[:80]) + " ... " + " ".join(wl[-140:])
    tl_topic = (topic or "").lower()
    is_genesis_topic = "god" in tl_topic and "serpent" in tl_topic
    # Extract specific claims from this round for judge to reference
    ap_claim = ap[:400]
    sk_claim = sk[:400]
    # Varied openers - avoid Look/Honestly
    openers = [
        f"What decided round {rn} for me was",
        f"The reason I went with {pref_label} in round {rn} comes down to",
        f"Round {rn} came down to a specific exchange,",
        f"When I look at what actually happened in round {rn},",
        f"For me, round {rn} hinged on",
        f"Two things made me score round {rn} for {pref_label},",
        f"In round {rn}, {pref_label} did something {other_label} didn't,",
        f"I'm scoring round {rn} for {pref_label} because",
    ]
    import random as _rop
    opener = _rop.choice(openers)
    # Force specificity by requiring quotes
    if side=="A":
        prompt=f"You are {prov} from {comp}, YouTube debate judge for '{topic}' round {rn}. You just scored {pref_label} HIGHER than {other_label}. CRITICAL: Be ultra-specific to THIS round's actual arguments, not generic debate praise. You MUST quote or paraphrase a specific claim from each side in round {rn}. Start with: {opener}. Structure: Sentence 1: Quote what {pref_label} argued in round {rn}: '{trim(ap)[:200]}'. Sentence 2: Quote what {other_label} argued: '{trim(sk)[:200]}' and why that was weaker in THIS round. Sentence 3: Give 2nd specific reason {pref_label} won round {rn} referencing evidence. Sentence 4: Conclude why round {rn} goes to {pref_label}. Use contractions, conversational. Do NOT use Look or Honestly. Do NOT be generic like 'better evidence' or 'clearer argument' without saying what evidence. Mention actual points: for Genesis mention verses, Hebrew, tree of life, 930 years, etc. For God existence mention cosmological, evil, hiddenness, fine-tuning specifically. Avoid: {used_expl}. 3-4 sentences, specific to round {rn}."
    else:
        prompt=f"You are {prov} from {comp}, YouTube debate judge for '{topic}' round {rn}. You just scored {pref_label} HIGHER than {other_label}. CRITICAL: Be ultra-specific to THIS round's actual arguments. You MUST quote or paraphrase specific claims from round {rn}. Start with: {opener}. Structure: Sentence 1: What {pref_label} said in round {rn}: '{trim(ap)[:200]}' that was strong. Sentence 2: What {other_label} said: '{trim(sk)[:200]}' that failed in THIS round. Sentence 3: Second specific reason {pref_label} won - reference actual evidence from round {rn}. Sentence 4: Conclude round {rn} winner. Use contractions, conversational. No Look/Honestly. No generic praise. Must reference actual round content, not general debate theory. Avoid: {used_expl}. 3-4 sentences, specific to round {rn}."
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
            f"What stood out to me in round {rn} was how {pref_label} handled Genesis 3 verse 10, Adam was afraid and hid. That's not just an emotion, that's a break in fellowship, and in biblical language that counts as death. {other_label} kept pointing to no physical drop that day, but didn't address that relational rupture which happened immediately. That specificity is why I scored {pref_label} higher this round.",
            f"The reason I leaned toward {pref_label} in round {rn} is the Hebrew phrase in the day. They showed Genesis 2 verse 4 uses the same construction to mean when, not a 24-hour timer, so moth tamuth is about certainty. Adam living 930 years actually fits that reading, while {other_label}'s literal deadline reading doesn't hold together with chapter 5 verse 5.",
            f"I kept coming back to one detail in round {rn}, the omitted consequences. {pref_label} pointed out the serpent promised you'd be as gods in verse 5 but said nothing about pain, thorns, sweat, or losing the tree of life in verses 22 to 24. {other_label} didn't deal with that omission, and leaving out that cost matters when we're judging who told the fuller truth.",
            f"For round {rn}, I found myself siding with {pref_label} because they tracked immortality loss that very day. Verse 22 says lest he take also of the tree of life and live forever, verse 24 says cherubim blocked the way. That's access to everlasting life lost on that day. {other_label} focused only on whether someone fell over dead, which missed this key point.",
            f"Round {rn} was interesting because {pref_label} brought character into it, not just word studies. God in Genesis 2 verse 16 says you may freely eat of every tree, abundantly generous, while the serpent in 3 verse 1 says did God really say you shall not eat of every tree, making God sound stingy. That twist is relevant to who is being truthful, and {other_label} didn't address it.",
            f"When I compare what was actually said in round {rn}, {pref_label} made the Hebrew contrast clear, moth tamuth surely die versus lo moth temuthun not surely die, a direct negation. What follows is to dust you shall return in verse 19, so the certainty God stressed does come true, even if not instant. {other_label} didn't engage that linguistic point.",
            f"What tipped it for me in this round was Genesis 3 verse 7, they knew they were naked and sewed fig leaves. {pref_label} noted that's shame, not just neutral knowledge, and shame wasn't advertised as a positive by the serpent. {other_label} treated eyes opening as purely good, but the text pairs it with shame and hiding.",
            f"Looking at round {rn} specifically, {pref_label} followed the narrative to exile, verses 23 and 24 says the Lord drove them out and placed cherubim to guard the way. That's losing home that day, not elevation. The serpent promised elevation in verse 5 but never mentioned losing Eden, and {other_label} didn't answer that gap.",
            f"In round {rn}, I scored {pref_label} higher because they defined death as separation, not just breathing stopping, and Genesis 3 shows separation from garden, ease, and tree of life starting that day. That fits verses 23 and 24 and verse 10's hiding. {other_label}'s definition was narrower and didn't fit the whole chapter.",
            f"The detail that stuck with me in round {rn} was the absolute nature of the serpent's claim, you shall not surely die in 3 verse 4, versus the story showing death entering through toil, pain, and return to dust in 3 verse 19. An absolute denial fails if any form of death begins, and {pref_label} showed that it did, while {other_label} defended the absolute.",
        ]
        fallbacks_b=[
            f"What stood out to me in round {rn} was the plain sense of yom. Genesis 1 defines a day as evening and morning, and chapter 2 verse 17 says in the day you eat you shall die. Adam did not die that day, he lived 930 years per chapter 5 verse 5, while his eyes did open exactly as the serpent said in chapter 3 verse 7. That direct match to the serpent's prediction is why I gave this round to {pref_label}.",
            f"The reason I leaned toward {pref_label} in round {rn} is God's own confirmation in chapter 3 verse 22, man has become like one of us knowing good and evil, word for word what the serpent promised in verse 5. If the serpent was entirely false, why does God affirm that part? {other_label} didn't address that confirmation.",
            f"I kept coming back to the immediate outcome in round {rn}. God said you'd die that day, the serpent said you won't die but your eyes will be opened, and verse 7 says their eyes were opened. There's no verse that day saying they died. The reported outcome fits the serpent's description better, which is why {pref_label} was stronger here.",
            f"For round {rn}, I found myself siding with {pref_label} because they centered the woman's experience. Chapter 3 verse 6 says she saw the tree was good for food, pleasant, desired to make wise, and ate, verse 13 says the serpent beguiled me. She got wisdom as promised, not death that day. {other_label} didn't engage that lived experience.",
            f"Round {rn} was interesting because {pref_label} asked why God would need to block the tree of life in verses 22 to 24 if they were already dead that day. Cherubim placed to prevent living forever only makes sense if they're still alive. {other_label} didn't answer why that guard would be needed.",
            f"When I compare the report in round {rn}, death that day is never actually reported. Chapter 3 verse 20 says Adam called his wife Eve mother of all living, chapter 4 verse 1 says they conceived Cain. They're building family, not lying dead. That narrative outcome aligns with the serpent's you shall not die claim for that day, which tipped it to {pref_label}.",
            f"What tipped it for me in this round was threat versus report. Threat was in the day you die, report says eyes opened, knew naked, sewed leaves, heard God walking. No report of death that day. {pref_label} stuck to what the text reports happened, while {other_label} imported a later theological category not in Genesis 2-3.",
            f"Looking at round {rn} specifically, {pref_label} highlighted that God affirms the serpent's second claim. Verse 5 says as gods knowing good and evil, verse 22 has God saying man has become as one of us to know good and evil. If that part was a lie, why would God echo it? {other_label} didn't resolve that.",
            f"The detail that stuck with me in round {rn} was definitional consistency. {pref_label} noted if death means separation, the text should say spiritual death, but it never does. It says dust to dust in verse 19 as future, not that day. {other_label} redefined death mid-argument, which weakened their case this round.",
            f"In round {rn}, I scored {pref_label} higher because there is an unresolved tension between warning and outcome. God warned death that day, serpent promised no death but enlightenment, enlightenment happens in verse 7 while death does not. {pref_label} stayed with the reported outcome, {other_label} didn't close that gap.",
        ]
    else:
        # Versatile fallbacks - specific, relevant, varied openers, no Look/Honestly overuse
        fallbacks_a=[
            f"What stood out to me in round {rn} was how {pref_label} brought checkable evidence that ties directly to this round's exchange. They laid out a mechanism you can test, while {other_label} relied on a general assumption that sounded plausible but didn't explain the specific cases raised in round {rn}.",
            f"The reason I leaned toward {pref_label} in round {rn} is definition consistency. They defined their key terms early in round {rn} and stuck to them, while I noticed {other_label} shifted meaning when pressed on a counter-example in this round, which made their argument less clear.",
            f"I kept coming back to one exchange in round {rn} where {pref_label} answered {other_label}'s strongest point head on with a concrete reply. {other_label} skipped that counter and repeated an earlier claim instead of engaging with what was just said in round {rn}. That direct engagement mattered to me.",
            f"For round {rn}, I found myself siding with {pref_label} because they were honest about costs and trade-offs specific to this topic in this round. They said here's what you gain and here's what it costs, while {other_label} only talked about benefits and didn't address the downside raised in round {rn}.",
            f"Round {rn} was interesting because {pref_label} used a concrete, lived example that made their principle click in the context of this round's debate. You could picture it happening, while {other_label} stayed abstract and I couldn't see how their view would work for the example at hand in round {rn}.",
            f"When I compare the reasoning in round {rn}, {pref_label} flowed cleanly from premise to conclusion without a jump, while {other_label} had a moment where the conclusion didn't follow from the evidence they cited in this round. That logical gap weakened them for me.",
            f"What tipped it for me in this round was explanatory scope in round {rn}. {pref_label} showed why the alternative fails to explain a common case that came up in this round, while their view handles it naturally. {other_label} didn't address that common case in round {rn}.",
            f"Looking at round {rn} specifically, {pref_label} distinguished correlation from causation clearly in their reply to {other_label} in this exchange. {other_label} treated them as the same thing in round {rn}, which made their inference weaker this round.",
            f"The detail that stuck with me in round {rn} was falsifiability. {pref_label} said what would count against them in this round and then showed evidence still supported them. {other_label} didn't offer that kind of testable standard in round {rn}, which made {pref_label} feel more rigorous.",
            f"In round {rn}, I scored {pref_label} higher because they balanced breadth and depth for this specific round, covering the big picture and the crucial detail that decides this round's question. {other_label} either stayed high level or got lost in minutiae and missed that balance in round {rn}.",
        ]
        fallbacks_b=[
            f"What stood out to me in round {rn} was how {pref_label} stuck to the plain meaning of what was said in this round's exchange. Their prediction matched what happened right away in round {rn}, while {other_label} had to add extra interpretation to make theirs fit in this specific round.",
            f"The reason I leaned toward {pref_label} in round {rn} is they pointed to a direct quote from this round that {other_label} had to reinterpret to make fit. When you have to twist the quote to make it work in round {rn}, it feels like stretching, and {pref_label} didn't need to do that here.",
            f"I kept coming back to predictive track record in round {rn}. {pref_label} had two independent claims that both lined up with what came up in this round, while {other_label} had one prediction that didn't occur as stated in round {rn}. That count matters for this round.",
            f"For round {rn}, I found myself siding with {pref_label} because they exposed a contradiction in {other_label}'s position in this round that never got resolved. I kept waiting for an answer to that contradiction in round {rn} and it didn't come, while {pref_label} stayed consistent.",
            f"Round {rn} was interesting because {pref_label} asked a pointed question specific to this round's logic: if {other_label} were already correct in this round, why would that extra step be needed? That question stuck with me and {other_label} didn't answer it in round {rn}.",
            f"When I compare what actually happened in round {rn}, {pref_label} showed lived outcome from this round's examples, people actually doing what they predicted, while {other_label} predicted an outcome that wasn't reported in this round. That fit with observed outcome in round {rn} mattered.",
            f"What tipped it for me in this round was comparing stated claim versus reported result in round {rn}. I looked at what was claimed in this round and what was reported, and the report matched {pref_label} better for this round. It's not about preference, it's about what round {rn} showed.",
            f"Looking at round {rn} specifically, even the opposing material in this round affirmed a key part of {pref_label}'s claim. When the other side's own source in round {rn} supports you, that's telling, and I thought {pref_label} had that here.",
            f"The detail that stuck with me in round {rn} was definitional stability. {pref_label} kept the same definition from start to finish in this round, while {other_label} changed what they meant halfway through round {rn}, which made it hard to trust their case for this round.",
            f"In round {rn}, I scored {pref_label} higher because they highlighted what {other_label} omitted in this round, a hidden cost or consequence that the full exchange in round {rn} includes. Leaving out that cost gave an incomplete picture of this round, and {pref_label} gave the fuller one.",
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
    return f"After three rounds, our panel of {jc} judges gave {roles['side_a_label']} {ca:.1f}, {roles['side_b_label']} {cb:.1f}. Final result is {res}. Thank you for watching, and you decide who is right."

def stitch_segments(segs,out):
    lf="concat_list.txt"
    open(lf,"w",encoding="utf-8").write("\n".join([f"file '{os.path.abspath(s).replace(chr(39),chr(39)+chr(92)+chr(39)+chr(39))}'" for s in segs])+"\n")
    cmd=["ffmpeg","-y","-f","concat","-safe","0","-i",lf,"-c","copy",out]
    r=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    if r.returncode!=0: print(r.stderr[-7000:]); raise RuntimeError("Concat failed")

# === ROBUST EMOJIS - NEVER WHITE BOX - TWEMOJI + SAFE FALLBACK ===
def emoji_to_codepoint(emoji_char):
    codes=[]
    for ch in emoji_char:
        cp=ord(ch)
        if cp==0xfe0f: continue
        codes.append(f"{cp:x}")
    return "-".join(codes)

def get_visual_story_flow(topic):
    tl=(topic or "").lower()
    if "god" in tl and "serpent" in tl:
        return ["🧑", "👤", "🧑‍🦱", "👥","🧑","👤","🌿","🌱","🍎","🍏","🌳","🌲","🐍","👀","👁️","🙈","😨","😣","😓","🪨","🚪","⚔️","👼","💀","💡","🧠",]
    elif "god" in tl and "exist" in tl:
        return ["🌌","⭐","🌍","🧠","💡","🤔","⚖️","💭","🙏","👤","👥","🌱","🔬","📖","💀","✨"]
    else:
        return ["💡","🔍","📖","⚖️","🧠","🌍","🌌","⭐","🔥","💧","🌳","🤖","💻","⚠️","✅","🤔","💭","👤","👥","🌱","✨"]

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
        "god":"✨","universe":"🌌","cosmos":"🌌","stars":"⭐","world":"🌍","earth":"🌍",
        "exist":"🤔","exists":"🤔","evidence":"🔍","proof":"🔍",
        "moral":"⚖️","good":"😇","evil":"😈","suffering":"😣","pain":"😣",
        "science":"🔬","faith":"🙏","pray":"🙏","believe":"🤔","think":"🤔","reason":"🧠",
        "ai":"🤖","robot":"🤖","computer":"💻",
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

EMOJI_CACHE_DIR="emoji_cache"
os.makedirs(EMOJI_CACHE_DIR, exist_ok=True)

# Safe emoji list - only emojis with reliable Twemoji assets, no problematic variation selectors
SAFE_EMOJIS = {"🧑","👤","👥","🧑‍🦱","🌿","🌱","🍎","🍏","🌳","🌲","🐍","👀","🙈","😨","😣","😓","🪨","🚪","⚔️","👼","💀","💡","🧠","🌌","⭐","🌍","🤔","⚖️","💭","🙏","🔬","📖","😇","✨","❤️","😈","🔍","🤖","💻","✅","⚠️","🔥","💧"}

def create_emoji_asset(emoji_char, index):
    filename=f"emoji_{index}.png"
    size=500
    # Only allow safe emojis - if not safe, replace with similar safe one
    if emoji_char not in SAFE_EMOJIS:
        # Map unsafe to safe
        fallback_map={"❓":"🤔","❤️":"❤️","😈":"😈","🔍":"🔍","✨":"✨","🌌":"🌌","⭐":"⭐"}
        emoji_char = fallback_map.get(emoji_char, "💡")
    try:
        code=emoji_to_codepoint(emoji_char)
        urls=[
            f"https://cdn.jsdelivr.net/gh/twitter/twemoji@14.0.2/assets/72x72/{code}.png",
            f"https://cdn.jsdelivr.net/gh/twitter/twemoji@latest/assets/72x72/{code}.png",
            f"https://raw.githubusercontent.com/twitter/twemoji/master/assets/72x72/{code}.png",
        ]
        if "-" in code:
            first=code.split("-")[0]
            urls.append(f"https://cdn.jsdelivr.net/gh/twitter/twemoji@14.0.2/assets/72x72/{first}.png")
            urls.append(f"https://cdn.jsdelivr.net/gh/twitter/twemoji@14.0.2/assets/72x72/{first.split('-')[0]}.png")
        cached_path=os.path.join(EMOJI_CACHE_DIR, f"{code}.png")
        emoji_img=None
        if os.path.exists(cached_path):
            try:
                emoji_img=Image.open(cached_path).convert("RGBA")
                if emoji_img.size[0]<10: emoji_img=None
            except:
                pass
        if emoji_img is None:
            for url in urls:
                try:
                    resp=requests.get(url, timeout=10)
                    if resp.status_code==200 and len(resp.content)>500:
                        emoji_img=Image.open(BytesIO(resp.content)).convert("RGBA")
                        try:
                            emoji_img.save(cached_path)
                        except:
                            pass
                        break
                except:
                    continue
        if emoji_img is not None:
            img=Image.new("RGBA",(size,size),(0,0,0,0))
            emoji_resized=emoji_img.resize((380,380), Image.LANCZOS)
            x=(size-380)//2
            y=(size-380)//2
            shadow=Image.new("RGBA",(size,size),(0,0,0,0))
            shadow_draw=ImageDraw.Draw(shadow)
            shadow_draw.ellipse([x+6,y+6,x+380+6,y+380+6], fill=(0,0,0,60))
            shadow=shadow.filter(ImageFilter.GaussianBlur(radius=6))
            img=Image.alpha_composite(img, shadow)
            img.paste(emoji_resized, (x,y), emoji_resized)
            img.save(filename)
            return filename
    except Exception as e:
        print(f"Twemoji download failed for {emoji_char} {code}: {e}, using safe fallback")
    # SAFE FALLBACK - NEVER WHITE BOX - colorful circle with icon, not tofu
    try:
        img=Image.new("RGBA",(size,size),(0,0,0,0))
        draw=ImageDraw.Draw(img)
        # Color based on emoji type
        color_map={
            "🧑":(255,213,128,255), "👤":(180,180,180,255), "👥":(120,180,255,255),
            "🌿":(80,200,120,255), "🌱":(100,220,100,255), "🍎":(255,80,80,255),
            "🌳":(60,180,75,255), "🐍":(100,200,100,255), "👀":(255,220,100,255),
            "💀":(200,200,200,255), "💡":(255,230,100,255), "🧠":(255,180,200,255),
            "🌌":(80,80,200,255), "⭐":(255,230,80,255), "🌍":(80,180,220,255),
            "🤔":(255,220,150,255), "⚖️":(200,200,100,255), "🙏":(255,213,128,255),
            "🔬":(150,200,255,255), "✨":(255,240,150,255), "😇":(255,240,180,255),
        }
        bg_color=color_map.get(emoji_char, (100,180,255,255))
        # Draw shadow
        draw.ellipse([60,60,440,440], fill=(0,0,0,70))
        # Draw main circle
        draw.ellipse([50,50,430,430], fill=bg_color, outline=(255,255,255,220), width=6)
        # Draw inner highlight
        draw.ellipse([80,80,200,200], fill=(255,255,255,90))
        # Draw emoji as text using safe font - if fails, draw first letter
        try:
            # Try to find emoji font, but if not, draw letter
            font=load_font(180,bold=True)
            # Use a simple representation - first letter of emoji meaning or emoji itself with safe font that won't produce tofu
            # Instead of tofu, draw a symbol
            symbol_map={
                "🧑":"person", "👤":"person", "👥":"people", "🌿":"leaf", "🍎":"apple",
                "🌳":"tree", "🐍":"snake", "👀":"eyes", "💀":"death", "💡":"idea",
                "🧠":"mind", "🌌":"space", "⭐":"star", "🌍":"world", "🤔":"think",
                "⚖️":"justice", "🙏":"pray", "🔬":"science", "✨":"god", "😇":"good",
            }
            label=symbol_map.get(emoji_char, emoji_char)
            # Draw text in centre - small, not tofu
            font_small=load_font(48,bold=True)
            draw.text((250,250), label[:6], font=font_small, fill=(0,0,0,200), anchor="mm")
        except:
            pass
        img.save(filename)
        return filename
    except Exception as e:
        print(f"Fallback also failed {e}, creating minimal colored dot")
        img=Image.new("RGBA",(size,size),(0,0,0,0))
        draw=ImageDraw.Draw(img)
        draw.ellipse([100,100,400,400], fill=(100,180,255,255), outline=(255,255,255,200), width=4)
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
        judge_pool = VOICE_POOL[3:]
        voice=judge_pool[idx % len(judge_pool)]
    elif role in CAST_VOICE_ASSIGNMENT:
        voice=CAST_VOICE_ASSIGNMENT[role]
    elif role.upper() in CAST_VOICE_ASSIGNMENT:
        voice=CAST_VOICE_ASSIGNMENT[role.upper()]
    elif "GOD TOLD TRUTH" in role.upper(): voice=VOICE_POOL[0]
    elif "SERPENT TOLD TRUTH" in role.upper(): voice=VOICE_POOL[1]
    elif "GOD EXISTS" in role.upper() and "NOT" not in role.upper(): voice=VOICE_POOL[0]
    elif "GOD DOES NOT EXIST" in role.upper() or "NO GOD" in role.upper(): voice=VOICE_POOL[1]
    elif "MODERATOR" in role.upper(): voice=VOICE_POOL[2]
    else: voice=CAST_VOICE_ASSIGNMENT.get(role, VOICE_POOL[0])
    try: return asyncio.run(generate_audio_async(text,voice,filename))
    except Exception as e:
        print(f"TTS primary failed {voice}: {e}, trying fallback same category")
        try:
            if "GOD" in role.upper() and "NOT" not in role.upper(): fb_voice="en-US-GuyNeural"
            elif "SERPENT" in role.upper() or "NOT" in role.upper() or "NO GOD" in role.upper(): fb_voice="en-GB-LibbyNeural"
            else: fb_voice="en-US-JennyNeural"
            # Ensure fallback also unique - not same as other cast
            if fb_voice in [VOICE_POOL[0], VOICE_POOL[1], VOICE_POOL[2]]:
                fb_voice = VOICE_POOL[5]
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
        vx=(VIDEO_W-EMOJI_W)//2
        vy=(VIDEO_H-EMOJI_H)//2 - 50
        next_label=f"[tmp{idx}]"
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
    # Extract opponent's last actual turn for mandatory rebuttal
    opponent_last = ""
    if prev_history:
        # Find last opponent block
        parts = prev_history.split(f"{opponent_label}:")
        if len(parts) > 1:
            opponent_last = parts[-1].strip()[-600:]
        else:
            opponent_last = prev_history[-1000:]
    else:
        opponent_last = "No opponent yet - this is opening"

    if round_num==1 and turn_num==1:
        round_focus="OPENING ROUND TURN 1: Set up your case naturally. Hook + strongest evidence. No opponent to rebut yet."
        rebuttal_instruction="This is first turn, no rebuttal needed, just make strong opening case."
    elif round_num==1:
        round_focus="OPENING ROUND: You MUST rebut opponent's last claim FIRST, then add new evidence."
        rebuttal_instruction=f"MANDATORY REBUTTAL STRUCTURE - DO THIS: 1) Start with: 'My opponent just said {opponent_last[:120]}...' and explain why that misses context. 2) Then say 'Here's why...' and bring your new point. You MUST address opponent before new fact. Don't just list facts."
    elif round_num==2:
        round_focus="REBUTTAL ROUND: This is core rebuttal round. You MUST dismantle opponent's last argument before adding anything."
        rebuttal_instruction=f"MANDATORY: Quote opponent's last point: '{opponent_last[:150]}' and directly rebut it with counter-evidence. Show where they are wrong. THEN add new evidence you haven't used. Structure: 1) You claimed X, but 2) Actually Y because... 3) And here's another reason..."
    else:
        round_focus="CLOSING ROUND: Summarize but STILL rebut opponent's last round point first."
        rebuttal_instruction=f"MANDATORY: Start by addressing opponent's last claim: '{opponent_last[:150]}'. Say why it fails, then bring it together. Don't ignore opponent. Debate is conversation, not two monologues."

    prev_snip=prev_history[-1200:] if prev_history else "No previous"
    used_str="; ".join(list(USED_ARGUMENTS)[-10:])[:500]
    used_kw="; ".join(list(USED_KEYWORDS)[-10:])
    tl = (topic or "").lower()
    is_genesis = "god" in tl and "serpent" in tl
    is_god_exist = "god" in tl and "exist" in tl
    if is_genesis:
        evidence_line = "Reference Genesis naturally: 2:17, 3:4, 3:7, 3:22, 5:5 - but speak like a person, not a reference list"
        fresh_line = "CRITICAL: Fresh angle not used before. If you said eyes opened, now try tree of life, cherubim, dust, shame, or Hebrew moth tamuth"
    elif is_god_exist:
        if "DOES NOT EXIST" in role_label.upper() or "NO GOD" in role_label.upper() or "NEGATIVE" in role_label.upper():
            evidence_line = "Use real arguments: problem of evil, divine hiddenness, lack of evidence, parsimony, God of gaps, Euthyphro dilemma. Give concrete examples, not just no."
            fresh_line = "CRITICAL: Fresh angle, if you used evil before, now try hiddenness or parsimony or incoherence. Real points."
        else:
            evidence_line = "Use real arguments: cosmological (cause), teleological (fine-tuning), moral argument, consciousness, contingency, personal experience. Give concrete examples, not just yes."
            fresh_line = "CRITICAL: Fresh angle, if you used cosmological before, now try fine-tuning or moral or consciousness. Real points."
    else:
        evidence_line = f"Use real examples, studies, lived experience, mechanisms, consequences about {topic} - make it concrete and human, not just yes/no"
        fresh_line = "CRITICAL: Fresh angle not used before. New mechanism, consequence, or example that makes sense for this topic"

    prompt=f"""You are {role_label} debating LIVE on YouTube about: {topic}
Your view: {role_desc}
Opponent: {opponent_label} = {opponent_desc}
{round_focus}
Opponent's LAST argument you MUST rebut first: {opponent_last[:400]}

Full recent history: {prev_snip[-800:]}

DO NOT REPEAT: {used_str}
Keywords already used: {used_kw}

CRITICAL DEBATE RULES - MUST FOLLOW:
- NEVER just say facts without addressing opponent. Real debate = conversation.
- {rebuttal_instruction}
- For Does God exist, if GOD EXISTS: argue cosmological, fine-tuning, moral, consciousness BUT always respond to opponent's evil/hiddenness claim first.
- If GOD DOES NOT EXIST: argue evil, hiddenness, lack of evidence, parsimony BUT always respond to opponent's cosmological/fine-tuning claim first.
- You MUST start with rebuttal if not first turn. Structure: Opponent said X -> Actually Y because -> And here's my new point Z.
- NEVER start with generic fact, always start by engaging opponent when there is opponent history.
- Make sense, be substantive, be a real conversation.

Speak like a REAL HUMAN on stage:
- Use contractions: I'm, don't, can't, it's, we're, that's, you've
- Speak in full natural sentences, not choppy phrases
- Vary rhythm: short punchy, then longer thoughtful
- Use transitions: "You said..., but...", "That's interesting, but when you actually look...", "I hear what you're saying about X, however..."
- {evidence_line}
- {fresh_line}
- Be conversational, passionate, like talking to a friend who disagrees
- {MIN_TURN_WORDS}-{MAX_TURN_WORDS} words, spoken English
- Must be versatile for topic: {topic}
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
            if len(lower_cleaned.split())<20 and ("yes" in lower_cleaned or "no" in lower_cleaned):
                continue
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
                        for kw in ["eyes opened","tree of life","cherubim","dust","shame","moth tamuth","beyom","pain","toil","exile","930 years","3:22","3:7","knowledge","wisdom","fine tuning","evil","hiddenness","cosmological","moral","consciousness"]:
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
    print(f"Asking {len(judges)} independent AI judges for round {rn}...")
    def worker(model): return judge_round(model,topic,rn,ap,sk,roles)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(JUDGE_WORKERS, len(judges)))) as executor:
        futures={executor.submit(worker, model): model for model in judges}
        completed=0
        for future in concurrent.futures.as_completed(futures):
            model=futures[future]
            try:
                result=future.result()
                results.append(result); completed+=1
                print(f"   Judge {completed}/{len(judges)} - {result['provider']} ({result['display_name']}) {result['A_total']:.1f} vs {result['B_total']:.1f} -> {result['winner']}")
            except Exception as exc:
                print(f"   Judge failed {provider_from_model(model)}: {str(exc)[:100]}")
    if not results: results=[neutral_judge("fallback")]
    return results

def calculate_round_average(results):
    return round(sum(r["A_total"] for r in results)/len(results),2), round(sum(r["B_total"] for r in results)/len(results),2)

def create_emoji_plan(text, words):
    if not words:
        return []
    word_emoji_map={
        "adam":"🧑","man":"🧑","men":"👥","human":"🧑","person":"👤","people":"👥",
        "garden":"🌿","eden":"🌿","plant":"🌱",
        "apple":"🍎","fruit":"🍎","eat":"🍎","eating":"🍎","trees":"🌳","tree":"🌳",
        "serpent":"🐍","snake":"🐍",
        "eyes":"👀","eye":"👀","see":"👀","naked":"🙈","shame":"🙈",
        "afraid":"😨","fear":"😨","hide":"😨","hid":"😨",
        "death":"💀","die":"💀","died":"💀","dying":"💀","dust":"💀",
        "sword":"⚔️","cherubim":"👼","angel":"👼",
        "knowledge":"💡","wise":"🧠","wisdom":"💡",
        "god":"✨","lord":"✨","creator":"✨",
        "universe":"🌌","cosmos":"🌌","space":"🌌","stars":"⭐","star":"⭐","world":"🌍","earth":"🌍",
        "exist":"🤔","exists":"🤔","evidence":"🔍","proof":"🔍","real":"✅",
        "moral":"⚖️","good":"😇","evil":"😈","suffering":"😣","pain":"😣",
        "science":"🔬","faith":"🙏","pray":"🙏","believe":"🤔","think":"🤔","reason":"🧠",
        "love":"❤️","heart":"❤️","soul":"✨",
        "begin":"🌱","began":"🌱","beginning":"🌱","cause":"💥","caused":"💥",
        "design":"🎨","designed":"🎨",
    }
    plan=[]
    used_times=[]
    for w_idx, w in enumerate(words):
        clean_w = re.sub(r"[^a-z]", "", w["text"].lower())
        if clean_w in word_emoji_map:
            start=float(w["start"])
            end=float(w["end"]) + 3.5
            overlaps=False
            for s,e in used_times:
                if not (end < s or start > e):
                    overlaps=True
                    break
            if overlaps:
                continue
            if used_times and start - used_times[-1][1] < 1.0:
                continue
            emoji_char=word_emoji_map[clean_w]
            if emoji_char not in SAFE_EMOJIS:
                continue
            if emoji_char in [p["emoji"] for p in plan[-2:]]:
                continue
            plan.append({"emoji":emoji_char, "start":max(0.0,start), "end":end, "label":clean_w, "word":w["text"]})
            used_times.append((start,end))
            if len(plan)>=6:
                break
    return plan

def create_segment(text,role,speaker_name,topic,segment_id,model_for_visuals,position=None,glow=None,judge_voice_index=None):
    if position is None:
        if "GOD TOLD TRUTH" in role.upper() or "GOD EXISTS" in role.upper(): position="left"
        elif "SERPENT" in role.upper() or "NOT EXIST" in role.upper() or "NO GOD" in role.upper(): position="right"
        else: position="center" if "JUDGE" in role.upper() or role=="Moderator" else "left"
    if glow is None:
        if "GOD TOLD TRUTH" in role.upper() or "GOD EXISTS" in role.upper(): glow="#00FFCC"
        elif "SERPENT" in role.upper() or "NOT EXIST" in role.upper() or "NO GOD" in role.upper(): glow="#FF00FF"
        else: glow="#3399FF" if "JUDGE" in role.upper() else "#FFD700"
    af=f"audio_{segment_id}.mp3"; sf=f"subs_{segment_id}.ass"; bf=f"bg_{segment_id}.png"; uf=f"ui_{segment_id}.png"; vf=f"segment_{segment_id}.mp4"
    words=generate_audio(text,role,af,judge_voice_index)
    try:
        generate_subtitles(words,sf, scorecard=False, audio_file=af, full_text=text)
    except TypeError:
        generate_subtitles(words,sf)
    eplan=[]
    try:
        eplan=create_emoji_plan(clean_for_speech(text),words)
        if eplan: print(f"   {len(eplan)} emoji(s) 3.5s each: {', '.join(v['emoji']+'('+v['word']+')' for v in eplan)}")
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
    assign_unique_voices(roles)
    print(f"Roles: {roles['side_a_label']} VS {roles['side_b_label']} - VERSATILE")
    print(f"UNIQUE VOICES: {CAST_VOICE_ASSIGNMENT}")
    print(f"Debate engines: {get_judge_short_name(ap_model)} [{provider_from_model(ap_model)}] vs {get_judge_short_name(sk_model)} [{provider_from_model(sk_model)}]")
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
        vm=sk_model if "SERPENT" in role.upper() or "NOT" in role.upper() or "NO GOD" in role.upper() or role=="B" else ap_model
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
