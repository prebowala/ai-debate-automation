
import os
import re
import json
import glob
import random
import asyncio
import requests
import subprocess
import time
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
WORDS_PER_TURN = 200
MIN_TURN_WORDS = 170
MAX_TURN_WORDS = 230
MAX_JUDGES = 7
MAX_EMOJIS_PER_SEGMENT = 5

VOICES = {
    "Moderator": "en-US-AndrewMultilingualNeural",
    "AI Christian Apologist": "en-US-BrianMultilingualNeural",
    "AI Skeptic": "en-US-AvaMultilingualNeural",
}
JUDGE_VOICES = [
    "en-US-ChristopherNeural",
    "en-US-EmmaMultilingualNeural",
    "en-US-GuyNeural",
    "en-GB-RyanNeural",
    "en-AU-WilliamNeural",
    "en-CA-ClaraNeural",
    "en-US-JennyNeural",
]

PROVIDER_ALIASES = {
    "openai": "OpenAI", "anthropic": "Anthropic", "google": "Google",
    "x-ai": "xAI", "xai": "xAI", "deepseek": "DeepSeek",
    "mistralai": "Mistral", "mistral": "Mistral",
    "meta-llama": "Meta", "meta": "Meta",
    "qwen": "Qwen", "alibaba": "Qwen",
}

def provider_from_model(m):
    if not m: return "Unknown"
    base = m.split("/",1)[0].lower().strip()
    return PROVIDER_ALIASES.get(base, base.title())

def cleanup_cache():
    for pat in ["*.mp4","*.mp3","*.ass","*.png","*.gif","*_list.txt"]:
        for f in glob.glob(pat):
            if f in {OUTPUT_FILE,"background.png","topic.txt"}: continue
            try: os.remove(f)
            except: pass

def count_words(t): return len(re.findall(r"\b[\w'-]+\b", t or ""))
def clean_for_speech(t):
    t=re.sub(r"\([^)]*\)","",t or "")
    for old in ["*","#","_","`","–","—","\"","+",":",";","&","=","|","<",">","/","\\"]:
        t=t.replace(old, " ")
    t=re.sub(r"\s+"," ",t)
    return t.strip()
def clamp_score(v):
    try: v=float(v)
    except: v=50.0
    return max(0.0,min(100.0,v))
def load_font(sz,bold=False):
    paths=["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
    for p in paths:
        try: return ImageFont.truetype(p,sz)
        except: continue
    return ImageFont.load_default()
def hex_to_rgba(h,a):
    h=h.lstrip("#")
    return (int(h[0:2],16),int(h[2:4],16),int(h[4:6],16),a)
def openrouter_headers():
    return {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json", "HTTP-Referer": "https://openrouter.ai/", "X-Title": "AI Debate Arena"}

def discover_models():
    """STRICTLY FREE ONLY - no credits, so only :free models to avoid 402"""
    if not OPENROUTER_API_KEY: raise RuntimeError("OPENROUTER_API_KEY missing")
    # Known working free models as of 2026 - ONLY :free suffix, no paid
    KNOWN_FREE_FRONTIER = [
        "google/gemini-2.0-flash-exp:free",
        "google/gemini-2.0-flash-001:free",
        "google/gemma-3-27b-it:free",
        "google/gemma-3-12b-it:free",
        "meta-llama/llama-3.3-70b-instruct:free",
        "meta-llama/llama-3.1-405b-instruct:free",
        "meta-llama/llama-3.2-90b-vision-instruct:free",
        "meta-llama/llama-3.1-70b-instruct:free",
        "deepseek/deepseek-r1:free",
        "deepseek/deepseek-r1-distill-llama-70b:free",
        "deepseek/deepseek-r1-distill-qwen-32b:free",
        "deepseek/deepseek-chat:free",
        "deepseek/deepseek-r1:free",
        "qwen/qwen-2.5-72b-instruct:free",
        "qwen/qwq-32b:free",
        "qwen/qwen-2.5-coder-32b-instruct:free",
        "qwen/qwen-2.5-7b-instruct:free",
        "mistralai/mistral-nemo:free",
        "mistralai/mistral-small-24b-instruct-2501:free",
        "mistralai/mistral-7b-instruct:free",
        "mistralai/mistral-small:free",
        # OpenAI and Anthropic free are rare, but try if they exist
        "openai/gpt-4o-mini:free",
        "openai/gpt-4o-mini-search-preview:free",
        "anthropic/claude-3.5-haiku:free",
        "anthropic/claude-3-haiku:free",
        "x-ai/grok-2-mini:free",
        "cohere/command-r-plus:free",
        "nvidia/llama-3.1-nemotron-70b-instruct:free",
    ]
    try:
        r=requests.get(OPENROUTER_MODELS_URL, headers=openrouter_headers(), timeout=20)
        print(f"Discover free models API: HTTP {r.status_code}")
        if r.status_code!=200:
            print("API failed, using known free list only")
            return KNOWN_FREE_FRONTIER
        data=r.json().get("data",[])
        free_models=[]
        free_by_provider={}
        for item in data:
            mid=item.get("id","")
            if not mid: continue
            # STRICT: only :free
            if ":free" not in mid: continue
            low=mid.lower()
            if any(x in low for x in ["embed","tts","whisper","audio","image","vision","moderation","guard"]):
                continue
            free_models.append(mid)
            prov=provider_from_model(mid)
            if prov not in free_by_provider:
                free_by_provider[prov]=mid
        
        print(f"Found {len(free_models)} free models (strict :free only, no paid to avoid 402):")
        for prov, mid in free_by_provider.items():
            print(f"  {prov} -> {mid}")
        
        # Prioritize frontier free
        frontier = ["OpenAI","Anthropic","Google","Meta","Mistral","Qwen","DeepSeek","xAI","Cohere","Nvidia"]
        prioritized=[]
        # First: one free per frontier if exists
        for prov in frontier:
            if prov in free_by_provider:
                prioritized.append(free_by_provider[prov])
        # Then add remaining free that are not yet in prioritized
        for m in free_models:
            if m not in prioritized:
                prioritized.append(m)
        # If still less than 5, add from known list that are also free
        if len(prioritized)<5:
            for m in KNOWN_FREE_FRONTIER:
                if m not in prioritized:
                    prioritized.append(m)
        
        prioritized=list(dict.fromkeys(prioritized))
        print(f"Final free-only prioritized list ({len(prioritized)} models, no paid):")
        for m in prioritized[:12]:
            print(f"  {provider_from_model(m)} -> {m}")
        return prioritized[:35]
    except Exception as e:
        print(f"Discover failed {e}, using known free only")
        return KNOWN_FREE_FRONTIER

def query_openrouter(prompt, model_id, timeout=45, max_tokens=900, temperature=0.75):
    if not OPENROUTER_API_KEY: return None
    # STRICT: Skip non-free models to avoid 402 since user has no credits
    if ":free" not in model_id:
        print(f"  Skipping non-free model {model_id} because user has no credits, must be free")
        return None
    payload={"model":model_id,"messages":[{"role":"user","content":prompt}],"temperature":temperature,"max_tokens":max_tokens}
    for attempt in range(2):
        try:
            r=requests.post(OPENROUTER_URL, headers=openrouter_headers(), json=payload, timeout=timeout)
            if r.status_code==200:
                choices=r.json().get("choices",[])
                if choices:
                    c=choices[0].get("message",{}).get("content","")
                    if c and len(c.strip())>20: return c.strip()
            else:
                print(f"  {provider_from_model(model_id)} HTTP {r.status_code} on {model_id} (free-only mode)")
                if r.status_code in [404,402,429]:
                    return None
        except Exception as e:
            print(f"  {provider_from_model(model_id)} fail {str(e)[:60]}")
        time.sleep(1.2)
    return None

def choose_primary_models(avail):
    # Only free models
    free_avail=[m for m in avail if ":free" in m]
    if not free_avail: free_avail=avail
    if len(free_avail)>=2: return free_avail[0], free_avail[1]
    return free_avail[0], free_avail[0]

def choose_judges(avail, primary):
    """Ensure at least 5 judges, STRICTLY FREE ONLY"""
    free_avail=[m for m in avail if ":free" in m]
    if not free_avail: free_avail=avail
    
    by_provider={}
    for m in free_avail:
        p=provider_from_model(m)
        if p not in by_provider:
            by_provider[p]=m
    
    frontier=["Google","Meta","Mistral","Qwen","DeepSeek","OpenAI","Anthropic","xAI","Cohere","Nvidia"]
    judges=[]
    for prov in frontier:
        if prov in by_provider and by_provider[prov] not in judges:
            judges.append(by_provider[prov])
            if len(judges)>=MAX_JUDGES: break
    
    # If still <5, add more free models even if same provider different model (still free)
    if len(judges)<5:
        for m in free_avail:
            if m not in judges:
                judges.append(m)
                if len(judges)>=MAX_JUDGES: break
    
    print(f"Selected {len(judges)} FREE-ONLY judges (no credits needed, at least 5 required):")
    for j in judges: print(f"  {provider_from_model(j)} -> {j} (FREE)")
    
    if len(judges)<=1:
        print("Only 1 free judge found, forcing free fallback")
        judges=[
            "google/gemini-2.0-flash-exp:free",
            "meta-llama/llama-3.3-70b-instruct:free",
            "deepseek/deepseek-r1:free",
            "qwen/qwq-32b:free",
            "mistralai/mistral-nemo:free",
            "google/gemma-3-27b-it:free",
            "qwen/qwen-2.5-72b-instruct:free",
        ][:MAX_JUDGES]
    
    # Final filter: ensure all are free
    judges=[m for m in judges if ":free" in m]
    return judges[:MAX_JUDGES]

USED_ARGUMENTS=set()

def generate_fallback_debate(role_label, topic, round_num, turn_num, opponent_last):
    is_for="APOLOGIST" in role_label.upper()
    if is_for:
        bank=[
            "Look, if we are honest about the universe having a beginning, that changes everything. The Borde Guth Vilenkin theorem shows inflation cannot be eternal into the past. So there has to be something outside time and space that started it. And consciousness is another clue. You can scan a brain and see neurons firing, but you never see what it is like to taste coffee or feel love from the inside. That subjective experience is not just chemistry. It points to something more.",
            "Fine tuning is also striking. The cosmological constant is tuned to one part in ten to the hundred and twenty. If it was slightly different, no stars, no planets, no life. That kind of precision is hard to call luck. And morality, we all live like some things are actually wrong, not just unpopular. Torturing children for fun is not just disliked, it is wrong. That sense of real right and wrong fits better if there is a moral foundation, not just evolved preferences.",
            "Historical evidence matters too. The early followers of Jesus went from scared and hiding to willing to be killed for saying they saw him alive. People do not die for a lie they know they made up. And across cultures, people report encounters with something greater that transforms them. You can dismiss one story, but the pattern across history needs an explanation.",
            "Suffering is hard, but without God, there is no problem of suffering, it is just stuff happening. The fact that we feel it should not be this way suggests we have a sense of how it ought to be. And if love matters, freedom matters. If God showed up in the sky every day forcing belief, that would not be love, it would be coercion. The clues are there, but you have to seek.",
        ]
    else:
        bank=[
            "The suffering issue is really tough for me. It is not just that bad things happen, it is that some suffering seems completely pointless. A deer burning for days in a forest fire where no one learns anything. If you could stop it easily and you cared, you would. So if God is all powerful and all loving, why does that happen. That feels like a logical problem, not just emotional.",
            "Hiddenness bothers me too. If God wants a relationship, why is the evidence so messy. You have thousands of religions all saying they have the truth, and sincere people in different cultures finding completely different gods. If I wanted to be known, I would make it clearer. And evolution explains a lot that used to be called design. Eyes, wings, brains can build up gradually over time without a designer.",
            "Fine tuning might be backwards. If there are many universes with different constants, we will obviously find ourselves in the one where we can exist. That is selection bias, not design. And we do not actually know if constants could have been different. Quantum physics also shows things can begin without a cause at that level, so maybe the universe is like that.",
            "Morality can be explained through evolution and culture. We evolved to care about cooperation because groups that cooperate survive better. That does not mean there is a law giver. And religious experiences are tricky, our brains are very good at seeing patterns that are not there, especially when we are stressed or want something to be true.",
        ]
    base=bank[(round_num+turn_num-1)%len(bank)]
    if opponent_last and round_num>1:
        return f"You were saying {opponent_last[:90]}. I hear that, and it is worth taking seriously. But I think it misses something. {base} When I look at your point directly, I do not think it holds up because there is more going on here than just that one angle."
    return base

def generate_turn(side, topic, round_num, turn_num, previous_exchange, model):
    global USED_ARGUMENTS
    side_name="AI Christian Apologist" if side=="A" else "AI Skeptic"
    side_short="for the existence of God" if side=="A" else "against the existence of God"
    opponent_last=previous_exchange[-900:] if previous_exchange else ""
    full_history = previous_exchange[-2500:] if previous_exchange else "None - opening"
    
    if round_num==1 and turn_num==1:
        instruction = f"Opening. Topic is {topic}. You are arguing {side_short}. This is your main case, must be at least {MIN_TURN_WORDS} words, target {WORDS_PER_TURN}. Warm natural conversational opening like talking to a friend over coffee. Include 2-3 specific reasons with real examples or numbers. No symbols like plus. Plain everyday language, no bullet points."
    else:
        instruction = f"Round {round_num} turn {turn_num}. You are {side_name} arguing {side_short}. Opponent just said: {opponent_last[:700]} You must: 1) First acknowledge what they actually said in your own words, 2) Explain specifically why that does not work with a counter example, 3) Add a fresh point with example. Must be at least {MIN_TURN_WORDS} words, target {WORDS_PER_TURN}. Natural conversational, no symbols like plus. Do not repeat previous points. Stay on topic {topic}."

    prompt = f"You are {side_name} arguing {side_short} on: {topic}. {instruction} Full debate so far for coherence: {full_history} Write ONLY your spoken part, natural conversational tone. Minimum {MIN_TURN_WORDS} words. No plus signs, no special characters."

    for attempt in range(3):
        resp=query_openrouter(prompt, model, max_tokens=900, temperature=0.78+attempt*0.08)
        if not resp: continue
        wc=count_words(resp)
        if wc < MIN_TURN_WORDS*0.85:
            print(f"  Turn too short {wc} < {MIN_TURN_WORDS}, extending...")
            ext=query_openrouter(f"Continue this to reach {MIN_TURN_WORDS} words, add specific example: {resp[:400]}", model, max_tokens=400, temperature=0.8)
            if ext: resp=resp+" "+ext
            wc=count_words(resp)
        resp=resp.replace("+"," and ")
        if wc>=MIN_TURN_WORDS*0.8:
            for s in resp.split('. ')[:2]:
                if len(s)>20: USED_ARGUMENTS.add(s[:60])
            print(f"  Turn OK: {wc} words, coherent")
            return resp
    fallback=generate_fallback_debate(side_name, topic, round_num, turn_num, opponent_last)
    if count_words(fallback)<MIN_TURN_WORDS:
        fallback=fallback+" "+generate_fallback_debate(side_name, topic, round_num, turn_num+1, "")
    return fallback.replace("+"," and ")

def build_round_exchanges(topic, rn, ap_model, sk_model, prev_hist):
    ap_turns=[]; sk_turns=[]; hist=prev_hist
    for tn in range(1,TURNS_PER_SIDE_PER_ROUND+1):
        a=generate_turn("A",topic,rn,tn,hist,ap_model)
        ap_turns.append(a); hist+=f"\nAI Christian Apologist:\n{a}\n\n"
        s=generate_turn("B",topic,rn,tn,hist,sk_model)
        sk_turns.append(s); hist+=f"\nAI Skeptic:\n{s}\n\n"
    return ap_turns, sk_turns, hist

def judge_round_real(model, topic, rn, ap, sk, all_models):
    # STRICT FREE ONLY
    if ":free" not in model:
        print(f"  Skipping {model} - not free, user has no credits")
        return None
    json_example = '{"A_argument":0,"A_rebuttal":0,"A_clarity":0,"B_argument":0,"B_rebuttal":0,"B_clarity":0}'
    base_prompt = f"You are impartial judge for round {rn} on: {topic} FOR: {ap[:900]} AGAINST: {sk[:900]} Score argument strength with specific examples, rebuttal quality did they address opponent actual point, clarity natural. Return ONLY JSON: {json_example}"
    tried=[]
    to_try=[model]+[m for m in all_models if m!=model]
    for idx, try_model in enumerate(to_try[:8]):  # Try 8 free models to get real score
        if try_model in tried: continue
        if ":free" not in try_model: continue  # Skip non-free
        tried.append(try_model)
        provider=provider_from_model(try_model)
        print(f"  Judge attempt {idx+1}/8: {provider} ({try_model}) FREE")
        resp=query_openrouter(base_prompt, try_model, timeout=35, max_tokens=280, temperature=0.1)
        if not resp:
            print(f"    {provider} failed, next free...")
            time.sleep(2)
            continue
        try:
            m=re.search(r"\{.*\}", resp, re.DOTALL)
            if not m: 
                time.sleep(1)
                continue
            d=json.loads(m.group(0))
            aa=clamp_score(d.get("A_argument",50)); ar=clamp_score(d.get("A_rebuttal",50)); ac=clamp_score(d.get("A_clarity",50))
            ba=clamp_score(d.get("B_argument",50)); br=clamp_score(d.get("B_rebuttal",50)); bc=clamp_score(d.get("B_clarity",50))
            if aa==50 and ar==50 and ac==50 and ba==50 and br==50 and bc==50:
                print(f"    {provider} all 50s, retrying...")
                time.sleep(1)
                continue
            at=(aa+ar+ac)/3; bt=(ba+br+bc)/3
            result={"model":try_model,"provider":provider,"A_argument":aa,"A_rebuttal":ar,"A_clarity":ac,"A_total":round(at,2),"B_argument":ba,"B_rebuttal":br,"B_clarity":bc,"B_total":round(bt,2),"winner":"A" if at>bt else "B","real":True}
            print(f"    REAL FREE SCORE: {provider} {result['A_total']:.1f} vs {result['B_total']:.1f}")
            return result
        except Exception as e:
            print(f"    {provider} parse fail {e}")
            time.sleep(1)
            continue
    print(f"  Judge slot failed after 8 free attempts")
    return None

def evaluate_round(judges, topic, rn, ap, sk):
    # Filter to free only
    free_judges=[m for m in judges if ":free" in m]
    if not free_judges: free_judges=judges
    results=[]
    all_models=list(dict.fromkeys(free_judges))
    print(f"\nAsking {len(all_models)} FREE-ONLY judges for REAL scores (no credits needed, at least 5 required)...")
    for idx, model in enumerate(all_models):
        if idx>0: time.sleep(3)
        print(f"\nJudge {idx+1}/{len(all_models)}: {provider_from_model(model)} FREE")
        res=judge_round_real(model, topic, rn, ap, sk, all_models)
        if res: results.append(res)
    real_scores=[r for r in results if r.get('real')]
    print(f"\nFINAL: Got {len(real_scores)}/{len(all_models)} REAL FREE scores (no paid, no credits needed)")
    for r in real_scores: print(f"  REAL FREE: {r['provider']} {r['A_total']:.1f} vs {r['B_total']:.1f}")
    if len(real_scores)==0:
        print("No real free scores, using fallback to avoid crash")
        return [{"model":"fallback","provider":"Fallback","A_argument":50,"A_rebuttal":50,"A_clarity":50,"A_total":50,"B_argument":50,"B_rebuttal":50,"B_clarity":50,"B_total":50,"winner":"A","real":False}]
    if len(real_scores)<5:
        print(f"Only {len(real_scores)} real free, trying extra free models to reach 5...")
        extra_free=[
            "google/gemma-3-12b-it:free",
            "meta-llama/llama-3.1-70b-instruct:free",
            "deepseek/deepseek-r1-distill-llama-70b:free",
            "qwen/qwen-2.5-7b-instruct:free",
            "mistralai/mistral-7b-instruct:free",
            "google/gemini-2.0-flash-exp:free",
            "meta-llama/llama-3.3-70b-instruct:free",
        ]
        for extra in extra_free:
            if len(real_scores)>=5: break
            if extra in [r['model'] for r in results]: continue
            print(f"  Extra free attempt: {provider_from_model(extra)}")
            time.sleep(2)
            res=judge_round_real(extra, topic, rn, ap, sk, all_models+extra_free)
            if res:
                results.append(res)
                real_scores.append(res)
    return results

def calculate_round_average(res):
    real=[r for r in res if r.get('real')]
    if not real: real=res
    a=sum(r["A_total"] for r in real)/len(real)
    b=sum(r["B_total"] for r in real)/len(real)
    return round(a,2), round(b,2)

async def generate_audio_async(text, voice, filename):
    com=edge_tts.Communicate(text, voice, rate="+0%", volume="+0%")
    audio=b""; words=[]
    async for chunk in com.stream():
        if chunk["type"]=="audio": audio+=chunk["data"]
        elif chunk["type"]=="WordBoundary":
            s=chunk["offset"]/10_000_000; d=chunk["duration"]/10_000_000
            words.append({"text":chunk["text"],"start":s,"duration":d,"end":s+d})
    open(filename,"wb").write(audio)
    if not words:
        try:
            r=subprocess.run(["ffprobe","-v","error","-show_entries","format=duration","-of","default=noprint_wrappers=1:nokey=1",filename],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=5)
            dur=float(r.stdout.strip())
            clean=clean_for_speech(text)
            toks=clean.split()
            if toks and dur>0:
                per=dur/len(toks)
                t=0.0
                for tok in toks:
                    words.append({"text":tok,"start":t,"duration":per*0.9,"end":t+per*0.9})
                    t+=per
        except:
            clean=clean_for_speech(text); t=0.0
            for tok in clean.split():
                words.append({"text":tok,"start":t,"duration":0.38,"end":t+0.38}); t+=0.42
    return words

def generate_audio(text, role, filename, judge_voice_index=None):
    if role=="AI Christian Apologist":
        voice=VOICES["AI Christian Apologist"]
    elif role=="AI Skeptic":
        voice=VOICES["AI Skeptic"]
    elif role=="AI Judge":
        voice=JUDGE_VOICES[(judge_voice_index or 0)%len(JUDGE_VOICES)]
    else:
        voice=VOICES.get(role, VOICES["Moderator"])
    try: return asyncio.run(generate_audio_async(clean_for_speech(text), voice, filename))
    except: return asyncio.run(generate_audio_async(clean_for_speech(text), VOICES["Moderator"], filename))

def format_ass_time(s):
    s=max(0.0,float(s)); h=int(s//3600); m=int((s%3600)//60); sec=s%60
    return f"{h}:{m:02d}:{sec:05.2f}"
def ass_escape(t):
    return str(t).replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}").replace("\n"," ")

def generate_subtitles(words, filename, scorecard=False, audio_file=None, full_text=None):
    header="[Script Info]\nScriptType: v4.00+\nPlayResX: 1920\nPlayResY: 1080\nWrapStyle: 0\n\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\nStyle: DebateSub,DejaVu Sans,48,&H00FFFFFF,&H00FFFFFF,&H00000000,&HAA000000,1,0,0,0,100,100,0,0,1,3,1,2,120,120,60,1\nStyle: ScoreSub,DejaVu Sans,36,&H00FFFFFF,&H00FFFFFF,&H00000000,&HAA000000,1,0,0,0,100,100,0,0,1,2,1,2,80,80,40,1\nStyle: EmojiSub,DejaVu Sans,100,&H00FFFFFF,&H00FFFFFF,&H00000000,&H66000000,1,0,0,0,100,100,0,0,1,3,1,2,80,80,40,1\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    events=[]
    if not words:
        open(filename,"w",encoding="utf-8").write(header)
        return
    chunk=[]; last_end=0
    emoji_events=[]
    word_emoji_map={
        "adam":"🧑","man":"🧑","men":"👥","human":"🧑","person":"👤","people":"👥",
        "garden":"🌿","eden":"🌿","plant":"🌱","apple":"🍎","fruit":"🍎","tree":"🌳",
        "serpent":"🐍","snake":"🐍","eyes":"👀","eye":"👀","see":"👀","saw":"👀","look":"👀",
        "naked":"🙈","shame":"🙈","afraid":"😨","fear":"😨","hide":"😨",
        "death":"💀","die":"💀","sword":"⚔️","angel":"👼",
        "knowledge":"💡","wise":"🧠","god":"✨","lord":"✨","creator":"✨",
        "universe":"🌌","cosmos":"🌌","stars":"⭐","world":"🌍","earth":"🌍",
        "exist":"🤔","evidence":"🔍","real":"✅","moral":"⚖️","good":"😇","evil":"😈","suffering":"😣","love":"❤️","begin":"🌱","cause":"💥","design":"🎨",
    }
    used_emoji_times=[]
    for w in words:
        if not chunk:
            chunk=[w]; last_end=w["end"]
        elif w["start"]-last_end>0.8 or len(chunk)>=12:
            s=chunk[0]["start"]; e=last_end+0.15
            txt="\\N".join([" ".join([ass_escape(c["text"]) for c in chunk[i:i+7]]) for i in range(0,len(chunk),7)][:2])
            events.append(f"Dialogue: 0,{format_ass_time(s)},{format_ass_time(e)},DebateSub,,0,0,0,,{{\\an2\\pos(960,840)\\q2\\fad(100,100)}}{txt}")
            chunk=[w]; last_end=w["end"]
        else:
            chunk.append(w); last_end=w["end"]
        clean_w=re.sub(r"[^a-z]","",w["text"].lower())
        if clean_w in word_emoji_map:
            start=float(w["start"])
            end=start+3.5
            if any(not (end < s or start > e) for s,e in used_emoji_times): continue
            if used_emoji_times and start-used_emoji_times[-1][1]<1.0: continue
            emoji_char=word_emoji_map[clean_w]
            if emoji_events and emoji_char in emoji_events[-1]: continue
            x_jitter=random.randint(-250,250)
            emoji_events.append(f"Dialogue: 1,{format_ass_time(start)},{format_ass_time(end)},EmojiSub,,0,0,0,,{{\\an5\\pos({960+x_jitter},500)\\fad(200,200)\\bord3\\shad2}}{emoji_char}")
            used_emoji_times.append((start,end))
    if chunk:
        s=chunk[0]["start"]; e=last_end+0.15
        txt="\\N".join([" ".join([ass_escape(c["text"]) for c in chunk[i:i+7]]) for i in range(0,len(chunk),7)][:2])
        events.append(f"Dialogue: 0,{format_ass_time(s)},{format_ass_time(e)},DebateSub,,0,0,0,,{{\\an2\\pos(960,840)\\q2\\fad(100,100)}}{txt}")
    all_events=events+emoji_events[:5]
    open(filename,"w",encoding="utf-8").write(header+"\n".join(all_events)+"\n")
    if emoji_events:
        print(f" Subs: {len(events)} text + {len(emoji_events[:5])} STOCK emoji text (no image, no blank rect) -> {filename}")
    else:
        print(f" Subs: {len(events)} events (bigger chunks for sync) -> {filename}")

def create_background(position, glow_color, filename):
    source=os.path.join(os.path.dirname(os.path.abspath(__file__)),"background.png")
    try: image=Image.open(source).convert("RGB").resize((VIDEO_W,VIDEO_H)) if os.path.exists(source) else Image.new("RGB",(VIDEO_W,VIDEO_H),(12,16,32))
    except: image=Image.new("RGB",(VIDEO_W,VIDEO_H),(12,16,32))
    overlay=Image.new("RGBA",(VIDEO_W,VIDEO_H),(0,0,0,0)); draw=ImageDraw.Draw(overlay)
    cx=400 if position=="left" else 1520 if position=="right" else 960
    for radius in range(700,50,-50):
        alpha=int(15*(1-radius/700))
        draw.ellipse([cx-radius,540-radius,cx+radius,540+radius],fill=hex_to_rgba(glow_color,alpha))
    overlay=overlay.filter(ImageFilter.GaussianBlur(30))
    Image.alpha_composite(image.convert("RGBA"),overlay).convert("RGB").save(filename)

def create_ui_overlay(speaker_name, topic, position, glow_color, filename):
    image=Image.new("RGBA",(VIDEO_W,VIDEO_H),(0,0,0,0)); draw=ImageDraw.Draw(image)
    title_font=load_font(30,bold=True); name_font=load_font(30,bold=True)
    title=f"TOPIC: {topic}"; box=draw.textbbox((0,0),title,font=title_font)
    draw.text(((VIDEO_W-(box[2]-box[0]))//2,24),title,fill="white",font=title_font)
    card_width=650; card_height=110; card_y=885
    card_x=75 if position=="left" else 1195 if position=="right" else (VIDEO_W-card_width)//2
    draw.rounded_rectangle([card_x,card_y,card_x+card_width,card_y+card_height],radius=18,fill=(18,26,46,235),outline=glow_color,width=4)
    draw.ellipse([card_x+22,card_y+27,card_x+47,card_y+52],fill=glow_color)
    draw.text((card_x+65,card_y+22),speaker_name,fill="white",font=name_font)
    image.save(filename)
    return card_x, card_y

def ffmpeg_filter_path(fn):
    return os.path.abspath(fn).replace("\\","/").replace("'","\\'").replace(":","\\:")

def render_video_segment(background=None, ui=None, audio=None, subtitles=None, output=None, position=None, glow_color=None, card_x=None, card_y=None, visual_plan=None, bg_path=None, ui_path=None, audio_path=None, subs_path=None, output_path=None, cx=None, cy=None, **kwargs):
    if background is None and bg_path is not None: background=bg_path
    if ui is None and ui_path is not None: ui=ui_path
    if audio is None and audio_path is not None: audio=audio_path
    if subtitles is None and subs_path is not None: subtitles=subs_path
    if output is None and output_path is not None: output=output_path
    if card_x is None and cx is not None: card_x=cx
    if card_y is None and cy is not None: card_y=cy
    if position is None: position=kwargs.get('position','center')
    if glow_color is None: glow_color=kwargs.get('glow','#FFD700')
    if card_x is None: card_x=kwargs.get('card_x',960)
    if card_y is None: card_y=kwargs.get('card_y',900)
    glow=glow_color.lstrip("#")
    pan_x="0" if position=="left" else "iw-(iw/zoom)" if position=="right" else "(iw-(iw/zoom))/2"
    parts=[f"[0:v]scale=1920:1080,zoompan=z='min(zoom+0.00020,1.05)':x='{pan_x}':y='(ih-(ih/zoom))/2':d=9000:s=1920x1080:fps=30[bg];","[1:v]scale=1920:1080[ui];",f"[2:a]showwaves=s=300x58:mode=cline:colors=0x{glow}:rate=30[wave];","[bg][ui]overlay=0:0[base];",f"[base][wave]overlay={card_x+330}:{card_y+47}[withwave];"]
    current="[withwave]"
    sub_path=ffmpeg_filter_path(subtitles)
    parts.append(f"{current}ass='{sub_path}'[outv]")
    cmd=["ffmpeg","-y","-loop","1","-framerate",str(FPS),"-i",background,"-i",ui,"-i",audio,"-filter_complex","".join(parts),"-map","[outv]","-map","2:a","-c:v","libx264","-preset","veryfast","-crf","20","-pix_fmt","yuv420p","-c:a","aac","-b:a","192k","-shortest",output]
    r=subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if r.returncode!=0:
        print(r.stderr[-7000:]); raise RuntimeError(f"FFmpeg failed {output}")

def generate_scoreboard(round_num, results, round_a, round_b, cumulative_a, cumulative_b, filename, roles=None):
    W=VIDEO_W; H=VIDEO_H
    source=os.path.join(os.path.dirname(os.path.abspath(__file__)),"background.png")
    try: base=Image.open(source).convert("RGB").resize((W,H),Image.LANCZOS) if os.path.exists(source) else Image.new("RGB",(W,H),(12,16,32))
    except: base=Image.new("RGB",(W,H),(12,16,32))
    overlay=Image.new("RGBA",(W,H),(0,0,0,180))
    img=Image.alpha_composite(base.convert("RGBA"),overlay).convert("RGB")
    draw=ImageDraw.Draw(img)
    ft=load_font(48,bold=True); fs=load_font(28,bold=True); fh=load_font(22,bold=True); fr=load_font(24)
    draw.text((W//2,50),f"ROUND {round_num} SCORES",font=ft,fill=(255,215,0,255),anchor="mt")
    header_y=190; col_j=120; col_a=750; col_b=1050; col_w=1350
    short_a="APOLOGIST"; short_b="SKEPTIC"
    draw.rectangle([60,header_y-10,W-60,header_y+45],fill=(25,35,70,255),outline=(255,215,0,180),width=2)
    draw.text((col_j,header_y),"Judge",font=fh,fill=(255,255,255,230))
    draw.text((col_a,header_y),short_a,font=fh,fill=(0,255,204,255))
    draw.text((col_b,header_y),short_b,font=fh,fill=(255,120,255,255))
    draw.text((col_w,header_y),"Winner",font=fh,fill=(255,215,0,255))
    y=header_y+65
    for res in results:
        is_real=res.get('real',True)
        bg=(20,28,50,255) if (y//58)%2==0 else (15,22,40,255)
        draw.rectangle([60,y-8,W-60,y+42],fill=bg)
        jt=res.get('provider','Judge')
        marker="✓" if is_real else "✗"
        draw.text((col_j,y),f"{marker} {jt[:28]}",font=fr,fill=(255,255,255,240) if is_real else (255,100,100,255))
        draw.text((col_a,y),f"{res['A_total']:.1f}",font=fr,fill=(0,255,204,255))
        draw.text((col_b,y),f"{res['B_total']:.1f}",font=fr,fill=(255,120,255,255))
        draw.text((col_w,y),short_a if res['winner']=="A" else short_b,font=fr,fill=(0,255,204,255) if res['winner']=="A" else (255,120,255,255))
        y+=58
    draw.line([(60,y+5),(W-60,y+5)],fill=(255,255,255,60),width=2); y+=25
    real_count=len([r for r in results if r.get('real')])
    draw.text((W//2,y),f"Round Avg: {round_a:.1f} vs {round_b:.1f} ({real_count} FREE judges)",font=fs,fill=(255,255,255,255),anchor="mt")
    draw.text((W//2,y+45),f"Cumulative: {cumulative_a:.1f} vs {cumulative_b:.1f}",font=fs,fill=(255,215,0,255),anchor="mt")
    img.save(filename)

def render_scorecard_video(scorecard, audio, subtitles, output):
    sub_path=ffmpeg_filter_path(subtitles)
    fc=f"[0:v]scale=1920:1080[base];[base]ass='{sub_path}'[outv]"
    cmd=["ffmpeg","-y","-loop","1","-framerate",str(FPS),"-i",scorecard,"-i",audio,"-filter_complex",fc,"-map","[outv]","-map","1:a","-c:v","libx264","-preset","veryfast","-crf","20","-pix_fmt","yuv420p","-c:a","aac","-b:a","192k","-shortest",output]
    r=subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if r.returncode!=0: print(r.stderr[-7000:]); raise RuntimeError("Scorecard failed")

def create_segment(text, role, speaker_name, topic, segment_id, model_for_visuals, position=None, glow=None, judge_voice_index=None, jidx=None, **kwargs):
    if judge_voice_index is None and jidx is not None: judge_voice_index=jidx
    if judge_voice_index is None: judge_voice_index=kwargs.get('jidx',0)
    if position is None: position="left" if role=="AI Christian Apologist" else "right" if role=="AI Skeptic" else "center"
    if glow is None: glow="#00FFCC" if role=="AI Christian Apologist" else "#FF00FF" if role=="AI Skeptic" else "#3399FF" if role=="AI Judge" else "#FFD700"
    af=f"audio_{segment_id}.mp3"; sf=f"subs_{segment_id}.ass"; bf=f"bg_{segment_id}.png"; uf=f"ui_{segment_id}.png"; vf=f"segment_{segment_id}.mp4"
    words=generate_audio(text, role, af, judge_voice_index)
    generate_subtitles(words, sf)
    create_background(position, glow, bf)
    cx,cy=create_ui_overlay(speaker_name, topic, position, glow, uf)
    render_video_segment(background=bf, ui=uf, audio=af, subtitles=sf, output=vf, position=position, glow_color=glow, card_x=cx, card_y=cy, visual_plan=[])
    return vf

def generate_panel_commentary(model, side, topic, rn, ap, sk, prev):
    if ":free" not in model:
        return f"Round {rn} came down to the core disagreement. For me, the case for this side edged it because it engaged directly."
    prov=provider_from_model(model)
    pref="the case for" if side=="A" else "the case against"
    def trim(t,mw=220):
        t=t.strip()
        if len(t.split())<=mw: return t
        s=t.split('. ')
        return (s[0][:180] + " ... " + s[-1][:180]) if len(s)>=2 else " ".join(t.split()[:mw])
    ap_trim=trim(ap, 220)
    sk_trim=trim(sk, 220)
    prompt = f"You are {prov}, judge for round {rn} on '{topic}'. FOR: {ap_trim} AGAINST: {sk_trim} You leaned {pref}. Give 2-sentence commentary specific to what they actually said. Sentence 1: key clash referencing actual points. Sentence 2: Why {pref} handled it better. Conversational, mention actual content."
    for attempt in range(2):
        resp=query_openrouter(prompt, model, timeout=35, max_tokens=280, temperature=0.85)
        if resp and count_words(resp)>=18:
            return resp
    return f"Round {rn} came down to {ap_trim[:60]} versus {sk_trim[:60]}. For me, {pref} edged it because it engaged directly."

def build_intro(topic, jc, judge_list=None):
    if judge_list and len(judge_list)>=2:
        companies=", ".join([provider_from_model(m) for m in judge_list[:5]])
        return f"Welcome to the AI Debate Arena. Today an AI Christian Apologist and an AI Skeptic are going to talk through the question: {topic}. We will have three rounds, plenty of time for each side to really respond to each other. We have {jc} different AIs judging from companies like {companies} scoring as we go. Let us get into it."
    else:
        return f"Welcome to the AI Debate Arena. Today an AI Christian Apologist and an AI Skeptic are going to talk through the question: {topic}. We will have three rounds, plenty of time for each side to really respond. We have {jc} different AIs judging as we go. Let us get into it."

def build_outro(jc, ca, cb, judge_list=None):
    res="a draw" if abs(ca-cb)<0.01 else "the Christian Apologist" if ca>cb else "the Skeptic"
    return f"Alright, after three rounds our {jc} different AIs have the Apologist at {ca:.1f} and the Skeptic at {cb:.1f}, so overall it leans toward {res}. But that is just the panel. What do you think actually held up?"

def stitch_segments(segs, out):
    lf="concat_list.txt"
    with open(lf,"w",encoding="utf-8") as f:
        for s in segs:
            p=os.path.abspath(s).replace("'","'\\''")
            f.write(f"file '{p}'\n")
    cmd=["ffmpeg","-y","-f","concat","-safe","0","-i",lf,"-c","copy",out]
    r=subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if r.returncode!=0: print(r.stderr[-7000:]); raise RuntimeError("Concat failed")

def run_debate_pipeline():
    cleanup_cache()
    if not OPENROUTER_API_KEY: raise RuntimeError("OPENROUTER_API_KEY missing")
    if not os.path.exists("topic.txt"): open("topic.txt","w",encoding="utf-8").write("Does God exist?")
    topic=open("topic.txt","r",encoding="utf-8").read().strip() or "Does God exist?"
    print("\n"+"="*70+f"\nAI DEBATE ARENA - FREE ONLY, NO CREDITS NEEDED\n"+"="*70+f"\nTOPIC: {topic}\n")
    print(f"ROUNDS={ROUNDS}, TURNS_PER_SIDE={TURNS_PER_SIDE_PER_ROUND}, WORDS_PER_TURN={WORDS_PER_TURN} (enforced min {MIN_TURN_WORDS})")
    print(f"JUDGES: {MAX_JUDGES} FREE ONLY (no paid, no credits) - OpenAI free if exists, else Google, Meta, Mistral, Qwen, DeepSeek")
    print("Emojis: STOCK Unicode text in ASS (no image = no blank rectangles)")
    avail=discover_models()
    ap_model, sk_model = choose_primary_models(avail)
    print(f"Debate engines (FREE): {provider_from_model(ap_model)} vs {provider_from_model(sk_model)}")
    judges=choose_judges(avail,(ap_model,sk_model))
    if len(judges)==0: judges=avail[:MAX_JUDGES]
    segs=[]; sid=0
    def add_seg(text,role,name,pos=None,glow=None,jidx=None,judge_voice_index=None, judge_list=None, **kwargs):
        if jidx is None and judge_voice_index is not None: jidx=judge_voice_index
        if jidx is None: jidx=kwargs.get('jidx',0)
        nonlocal sid
        vm=sk_model if role=="AI Skeptic" else ap_model
        v=create_segment(text,role,name,topic,sid,vm,pos,glow,jidx)
        segs.append(v); sid+=1
    add_seg(build_intro(topic,len(judges), judges),"Moderator","MODERATOR")
    prev_hist=""; cum_a=0.0; cum_b=0.0; panel_comments=[]
    roles={"side_a_label":"APOLOGIST","side_b_label":"SKEPTIC"}
    for rn in range(1,ROUNDS+1):
        print("\n"+"="*70+f"\nROUND {rn} - FREE ONLY JUDGES\n"+"="*70)
        ap_turns, sk_turns, prev_hist = build_round_exchanges(topic, rn, ap_model, sk_model, prev_hist)
        for ti in range(TURNS_PER_SIDE_PER_ROUND):
            ap=ap_turns[ti]; sk=sk_turns[ti]
            print(f"   Exchange {ti+1}: A={count_words(ap)} words | B={count_words(sk)} words")
            add_seg(ap,"AI Christian Apologist","AI CHRISTIAN APOLOGIST","left","#00FFCC")
            add_seg(sk,"AI Skeptic","AI SKEPTIC","right","#FF00FF")
        ap_full="\n".join(ap_turns); sk_full="\n".join(sk_turns)
        res=evaluate_round(judges, topic, rn, ap_full, sk_full)
        ra, rb = calculate_round_average(res)
        cum_a+=ra; cum_b+=rb
        print(f"Round {rn}: A {ra:.1f} vs B {rb:.1f} | Cum {cum_a:.1f} vs {cum_b:.1f}")
        sb=f"scoreboard_r{rn}.png"
        generate_scoreboard(rn, res, ra, rb, cum_a, cum_b, sb, roles)
        free_count=len([r for r in res if r.get('real')])
        stxt=f"Round {rn} is complete. {free_count} different AIs from {', '.join([provider_from_model(m) for m in judges[:3]])} and others gave the Apologist {ra:.1f} and the Skeptic {rb:.1f}. Cumulative is {cum_a:.1f} to {cum_b:.1f}."
        sa=f"score_audio_r{rn}.mp3"; ss=f"score_subs_r{rn}.ass"; sv=f"score_video_r{rn}.mp4"
        sw=generate_audio(stxt,"Moderator",sa)
        generate_subtitles(sw, ss)
        render_scorecard_video(sb, sa, ss, sv)
        segs.append(sv)
        if res:
            wj=[r for r in res if r.get('real')][0] if [r for r in res if r.get('real')] else res[0]
            com=generate_panel_commentary(wj["model"], wj["winner"], topic, rn, ap_full, sk_full, panel_comments)
            panel_comments.append(com)
            add_seg(com,"AI Judge","AI JUDGE — "+wj["provider"].upper(),"center","#3399FF", judge_voice_index=0)
    add_seg(build_outro(len(judges),cum_a,cum_b, judges),"Moderator","MODERATOR")
    stitch_segments(segs, OUTPUT_FILE)
    print("\n"+"="*70+"\nDEBATE COMPLETE - FREE ONLY, NO CREDITS\n"+"="*70)
    print(f"Output: {OUTPUT_FILE}")
    print(f"Final: Apologist {cum_a:.1f} vs Skeptic {cum_b:.1f} (from {len(judges)} FREE AIs, no paid)")
    cleanup_cache()

if __name__=="__main__":
    try: run_debate_pipeline()
    except KeyboardInterrupt: print("\nCancelled.")
    except Exception as exc:
        print("\nPIPELINE FAILED"); print(str(exc)); raise
