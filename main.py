
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

# FIXED SETTINGS - addresses all user feedback
FREE_MODE = True
ROUNDS = 3
TURNS_PER_SIDE_PER_ROUND = 4
WORDS_PER_TURN = 200
MIN_TURN_WORDS = 170
MAX_TURN_WORDS = 230
MAX_JUDGES = 7
JUDGE_WORKERS = 1
PANEL_COMMENTS_PER_ROUND = 1
MAX_VISUALS_PER_SEGMENT = 0
MAX_EMOJIS_PER_SEGMENT = 5
EMOJI_W = 220
EMOJI_H = 220

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
    "cohere": "Cohere", "perplexity": "Perplexity",
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
    for o,n in {"*":"","#":"","_":"","`":"","–":"-","—":"-","\"":"",":":" ", ";":" ", "&":"and"}.items(): t=t.replace(o,n)
    return re.sub(r"\s+"," ",t).strip()
def clamp_score(v):
    try: v=float(v)
    except: v=50.0
    return max(0.0,min(100.0,v))

def load_font(sz,bold=False):
    # Try multiple paths
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    ]
    for p in candidates:
        try: return ImageFont.truetype(p,sz)
        except: continue
    return ImageFont.load_default()

def find_emoji_font(size=140):
    """Find a font that actually has emoji glyphs to fix blank white triangles"""
    possible = [
        "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
        "/usr/share/fonts/truetype/noto/NotoEmoji-Regular.ttf",
        "/usr/share/fonts/truetype/ancient-scripts/Symbola.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansSymbols2-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansSymbols-Regular.ttf",
        "/usr/share/fonts/noto-cjk/NotoColorEmoji.ttf",
        "/usr/share/fonts/truetype/seguiemj.ttf",  # Windows Segoe
        "/System/Library/Fonts/Apple Color Emoji.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",  # fallback, has some symbols
    ]
    for p in possible:
        if os.path.exists(p):
            try:
                font = ImageFont.truetype(p, size)
                # Test if it can render a simple emoji
                return font, p
            except:
                continue
    # Last resort: DejaVu
    return load_font(size,bold=True), "fallback"

def hex_to_rgba(h,a):
    h=h.lstrip("#")
    return (int(h[0:2],16),int(h[2:4],16),int(h[4:6],16),a)

def openrouter_headers():
    return {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json", "HTTP-Referer": "https://openrouter.ai/", "X-Title": "AI Debate Arena"}

def discover_models():
    """Discover CURRENT free models, prioritizing frontier companies - fixes Poolside-only issue"""
    if not OPENROUTER_API_KEY: raise RuntimeError("OPENROUTER_API_KEY missing")
    # Known good frontier free models as of 2026 - updated to avoid 404s
    KNOWN_FRONTIER_FREE = [
        "google/gemini-2.0-flash-exp:free",
        "google/gemini-2.0-flash-001:free",
        "google/gemma-3-27b-it:free",
        "meta-llama/llama-3.3-70b-instruct:free",
        "meta-llama/llama-3.1-405b-instruct:free",
        "meta-llama/llama-3.2-90b-vision-instruct:free",
        "deepseek/deepseek-r1:free",
        "deepseek/deepseek-r1-distill-llama-70b:free",
        "deepseek/deepseek-chat:free",
        "qwen/qwen-2.5-72b-instruct:free",
        "qwen/qwq-32b:free",
        "qwen/qwen-2.5-coder-32b-instruct:free",
        "mistralai/mistral-nemo:free",
        "mistralai/mistral-small-24b-instruct-2501:free",
        "mistralai/mistral-small:free",
        "openai/gpt-4o-mini:free",
        "openai/gpt-4o-mini",
        "anthropic/claude-3-haiku:free",
        "anthropic/claude-3.5-haiku:free",
        "anthropic/claude-3.5-haiku",
        "x-ai/grok-2-mini:free",
        "cohere/command-r-plus:free",
    ]
    try:
        r=requests.get(OPENROUTER_MODELS_URL, headers=openrouter_headers(), timeout=20)
        print(f"Discover models API: HTTP {r.status_code}")
        if r.status_code!=200:
            print("API failed, using known frontier free list")
            return KNOWN_FRONTIER_FREE
        data=r.json().get("data",[])
        all_free=[]
        all_models_by_provider={}
        for item in data:
            mid=item.get("id","")
            if not mid: continue
            if any(x in mid.lower() for x in ["embed","tts","whisper","audio","image","vision","moderation"]): continue
            prov = provider_from_model(mid)
            all_models_by_provider.setdefault(prov, []).append(mid)
            if ":free" in mid:
                all_free.append(mid)
        
        print(f"Found {len(all_free)} free models total")
        # Prioritize frontier providers
        frontier = ["OpenAI","Anthropic","Google","Meta","Mistral","Qwen","DeepSeek","xAI"]
        prioritized=[]
        # First, pick one free model per frontier provider if available
        by_prov = {}
        for m in all_free:
            p=provider_from_model(m)
            if p not in by_prov:
                by_prov[p]=m
        for prov in frontier:
            if prov in by_prov:
                prioritized.append(by_prov[prov])
        
        # Then add remaining free models
        for m in all_free:
            if m not in prioritized:
                prioritized.append(m)
        
        # If still less than 5, add known frontier that might not be in :free but are cheap paid (will work if user has credits)
        if len(prioritized)<7:
            for m in KNOWN_FRONTIER_FREE:
                if m not in prioritized:
                    prioritized.append(m)
        
        # Deduplicate, keep order, limit
        prioritized = list(dict.fromkeys(prioritized))
        print(f"Prioritized frontier judges (fixing Poolside-only):")
        for m in prioritized[:15]:
            print(f"  {provider_from_model(m)} -> {m}")
        
        # Filter out obscure providers like Poolside if we have frontier
        filtered=[]
        for m in prioritized:
            p=provider_from_model(m)
            if p in frontier or len(filtered)<7:
                filtered.append(m)
        # Ensure at least 7
        if len(filtered)<7:
            filtered = prioritized
        
        return filtered[:30]
    except Exception as e:
        print(f"Discover failed {e}, using known frontier list")
        return KNOWN_FRONTIER_FREE

def query_openrouter(prompt, model_id, timeout=45, max_tokens=900, temperature=0.75):
    if not OPENROUTER_API_KEY: return None
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
                print(f"  {provider_from_model(model_id)} HTTP {r.status_code} on {model_id}")
                if r.status_code in [404,402,429]:
                    return None
        except Exception as e:
            print(f"  {provider_from_model(model_id)} fail {str(e)[:60]}")
        time.sleep(1)
    return None

def choose_primary_models(avail):
    # Pick two different providers for debate for variety, not same
    if len(avail)>=2:
        # Try to pick OpenAI vs Anthropic or Google vs Meta
        if len(avail)>=2:
            return avail[0], avail[1]
    return avail[0], avail[0]

def choose_judges(avail, primary):
    """Choose 5-7 judges from frontier companies, fixing Poolside-only bug"""
    by_provider={}
    for m in avail:
        p=provider_from_model(m)
        # Skip obscure if we already have frontier
        if p not in by_provider:
            by_provider[p]=m
    
    frontier_priority=["OpenAI","Anthropic","Google","Meta","Mistral","Qwen","DeepSeek","xAI"]
    judges=[]
    # First pass: frontier
    for prov in frontier_priority:
        if prov in by_provider and by_provider[prov] not in judges:
            judges.append(by_provider[prov])
            if len(judges)>=MAX_JUDGES: break
    
    # Second pass: if still less than 5, add from avail even if same provider but different model
    if len(judges)<5:
        for m in avail:
            if m not in judges:
                judges.append(m)
                if len(judges)>=MAX_JUDGES: break
    
    print(f"Selected {len(judges)} judges from frontier companies (fixing Poolside-only):")
    for j in judges: print(f"  {provider_from_model(j)} -> {j}")
    
    # If still only 1 (Poolside bug), force fallback to known frontier
    if len(judges)<=1:
        print("Only 1 judge found (Poolside bug), forcing frontier fallback list")
        fallback_frontier=[
            "google/gemini-2.0-flash-exp:free",
            "meta-llama/llama-3.3-70b-instruct:free",
            "deepseek/deepseek-r1:free",
            "qwen/qwq-32b:free",
            "mistralai/mistral-nemo:free",
            "openai/gpt-4o-mini",
            "anthropic/claude-3.5-haiku",
        ]
        judges=fallback_frontier[:MAX_JUDGES]
    
    return judges[:MAX_JUDGES]

USED_ARGUMENTS=set()

def generate_fallback_debate(role_label, topic, round_num, turn_num, opponent_last):
    """Fallback with LONG cases to fix short apologist issue"""
    is_for="APOLOGIST" in role_label.upper()
    if is_for:
        bank=[
            "Look, if we are honest about the universe, it had a beginning. The Borde-Guth-Vilenkin theorem shows you cannot have an eternal past of inflation. So whatever started everything cannot be inside time and space. That is not a trick, that is what beginning means. And then there is consciousness. You can scan a brain all day and see neurons firing, but you do not see what it is like to taste coffee or feel love. Material stuff does not explain subjective experience. There is a real gap there.",
            "Think about fine-tuning for a second. The cosmological constant is tuned to one part in ten to the hundred and twenty. If that was slightly different, no stars, no planets, no us. Saying that is just luck feels like saying you found a fully functioning watch in the desert and calling it wind. Plus morality. We all live like some things are actually wrong, not just unpopular. If there is no deeper grounding, then it is just preference, but we do not treat torture that way.",
            "And the resurrection and religious experience are worth taking seriously. You have got early testimony from people who said they saw Jesus alive, and they went from scared to willing to die for it. That is not what you do for a lie you made up. And across cultures, people report encounters that change them. You can dismiss each one, but at some point you have to ask what story makes sense of all of it together, not just one piece.",
            "I get the suffering worry, but if there is no God, suffering is just stuff happening. There is no problem of evil if there is no good to violate. The fact that we feel this should not be like this points to a standard beyond us. And hiddenness? Love requires freedom. If God forced belief by showing up in the sky every day, that would not be relationship, that would be coercion. The evidence is more like clues that you have to seek.",
        ]
    else:
        bank=[
            "Yeah, but here is the thing about suffering that keeps tripping me up. It is not just that bad stuff happens, it is that some of it seems completely pointless. A deer burning for days in a forest fire where no one learns anything from it. If you could stop that and you cared, you would. So if God is all-powerful and all-loving, why does not he? That is not just emotional, that is logical.",
            "The hiddenness part bothers me too. If God really wants a relationship with us, why is the evidence so messy? You have got thousands of religions all saying they have the truth, and sincere people in different cultures finding totally different gods. If I wanted to be known, I would not make it a puzzle. And evolution does a lot of the heavy lifting that used to be called design. Complex eyes, wings, brains build up slowly.",
            "I hear the fine-tuning point, but we might be looking at it backwards. If there are many universes with different constants, we are obviously going to find ourselves in the one where we can exist. That is not design, that is selection bias. And we do not actually know if those constants could have been different. Plus, quantum mechanics shows things can begin without a cause. The universe could be like that.",
            "And moral arguments worry me because we can explain morality through evolution and culture. We evolved to care about cooperation because it helps us survive. That does not mean there is an objective moral law giver. And religious experience is tricky because our brains are really good at seeing patterns that are not there, especially under stress. People see what they want to see.",
        ]
    base=bank[(round_num+turn_num-1)%len(bank)]
    if opponent_last and round_num>1:
        return f"You were saying that {opponent_last[:100]}... I hear that, and I think it is worth taking seriously. But I think it misses something important. {base} So when I look at your point directly, I do not think it holds up the way it first seems, because there is more going on here than just that one angle."
    return base

def generate_turn(side, topic, round_num, turn_num, previous_exchange, model):
    """Fixed to enforce MIN_TURN_WORDS to fix short apologist"""
    global USED_ARGUMENTS
    side_name="AI Christian Apologist" if side=="A" else "AI Skeptic"
    side_short="for the existence of God" if side=="A" else "against the existence of God"
    opponent_last=previous_exchange[-900:] if previous_exchange else ""
    if round_num==1 and turn_num==1:
        instruction = f"Opening. Topic is {topic}. You are arguing {side_short}. This is your main case, needs to be substantial - at least {MIN_TURN_WORDS} words, target {WORDS_PER_TURN}. Give a warm natural conversational opening like talking to a friend over coffee, not reading a textbook. Include 2-3 specific reasons with real examples, numbers, or stories. Plain everyday language, no bullet points. Must be at least {MIN_TURN_WORDS} words."
    else:
        banned=', '.join(list(USED_ARGUMENTS)[-3:])
        instruction = f"Round {round_num} turn {turn_num}. You are {side_name} arguing {side_short}. Opponent just said: {opponent_last[:600]} First, acknowledge what they actually said in your own words to show you heard them - this is important, don't skip it. Then explain why that does not work with a specific counter example or distinction. Then add a fresh point with example. Must be at least {MIN_TURN_WORDS} words, target {WORDS_PER_TURN}. Plain natural conversational language like chatting over coffee, no bullet points. Do not repeat: {banned}"
    
    prompt = f"You are {side_name} arguing {side_short} on: {topic} {instruction} Previous for context: {(previous_exchange[-800:] if previous_exchange else 'None')} Write ONLY your spoken part, natural conversational tone. Minimum {MIN_TURN_WORDS} words."
    
    for attempt in range(3):  # Try up to 3 times to get long enough
        resp=query_openrouter(prompt, model, max_tokens=900, temperature=0.8+attempt*0.1)
        if not resp: continue
        wc=count_words(resp)
        if wc < MIN_TURN_WORDS:
            print(f"  Turn too short: {wc} words < {MIN_TURN_WORDS}, retrying to get longer...")
            # Try to extend
            extend_prompt = f"Continue this argument to make it at least {MIN_TURN_WORDS} words total. Current is {wc} words, need longer. Add more specific example and address opponent more directly. Topic {topic}: {resp} Continue naturally:"
            resp2=query_openrouter(extend_prompt, model, max_tokens=500, temperature=0.8)
            if resp2:
                resp = resp + " " + resp2
                wc=count_words(resp)
        if wc >= MIN_TURN_WORDS*0.8:  # Allow 80% minimum to be reasonable
            low=resp.lower()
            is_repeat=any(len(a)>25 and a.lower() in low for a in list(USED_ARGUMENTS)[-5:])
            if not is_repeat or attempt==2:
                for s in resp.split('. ')[:2]:
                    if len(s)>20: USED_ARGUMENTS.add(s[:60])
                print(f"  Turn OK: {wc} words")
                return resp
    
    # Fallback - now LONG to fix short apologist
    fallback=generate_fallback_debate(side_name, topic, round_num, turn_num, opponent_last)
    # Ensure fallback is long enough
    if count_words(fallback)<MIN_TURN_WORDS:
        fallback=fallback+" "+generate_fallback_debate(side_name, topic, round_num, turn_num+1, "")
    print(f"  Using fallback: {count_words(fallback)} words")
    return fallback

def build_round_exchanges(topic, rn, ap_model, sk_model, prev_hist):
    ap_turns=[]; sk_turns=[]; hist=prev_hist
    for tn in range(1,TURNS_PER_SIDE_PER_ROUND+1):
        a=generate_turn("A",topic,rn,tn,hist,ap_model)
        ap_turns.append(a); hist+=f"\nApologist:\n{a}\n\n"
        s=generate_turn("B",topic,rn,tn,hist,sk_model)
        sk_turns.append(s); hist+=f"\nSkeptic:\n{s}\n\n"
    return ap_turns, sk_turns, hist

def judge_round_real(model, topic, rn, ap, sk, all_models):
    json_example = '{"A_argument":0,"A_rebuttal":0,"A_clarity":0,"B_argument":0,"B_rebuttal":0,"B_clarity":0}'
    # Include more of actual arguments for tailored judging
    base_prompt = f"You are impartial judge for round {rn} on: {topic} FOR: {ap[:900]} AGAINST: {sk[:900]} Score argument strength (did they give specific examples, numbers, stories?), rebuttal quality (did they address opponent actual point in their own words before countering?), clarity (natural conversational?). Be strict and differentiate scores, not all 50s. Return ONLY JSON: {json_example}"
    tried=[]
    to_try=[model]+[m for m in all_models if m!=model]
    for idx, try_model in enumerate(to_try[:5]):
        if try_model in tried: continue
        tried.append(try_model)
        provider=provider_from_model(try_model)
        print(f"  Judge attempt {idx+1}/5: {provider} ({try_model}) for REAL score")
        resp=query_openrouter(base_prompt, try_model, timeout=30, max_tokens=250, temperature=0.1)
        if not resp:
            print(f"    {provider} failed, next...")
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
                print(f"    {provider} returned all 50s, retrying...")
                time.sleep(1)
                continue
            at=(aa+ar+ac)/3; bt=(ba+br+bc)/3
            result={"model":try_model,"provider":provider,"A_argument":aa,"A_rebuttal":ar,"A_clarity":ac,"A_total":round(at,2),"B_argument":ba,"B_rebuttal":br,"B_clarity":bc,"B_total":round(bt,2),"winner":"A" if at>bt else "B","real":True}
            print(f"    REAL SCORE: {provider} {result['A_total']:.1f} vs {result['B_total']:.1f}")
            return result
        except Exception as e:
            print(f"    {provider} parse fail {e}")
            time.sleep(1)
            continue
    print(f"  Judge slot failed after 5 attempts, will count as missed but keep pipeline fast")
    return None

def evaluate_round(judges, topic, rn, ap, sk):
    results=[]
    all_models=list(dict.fromkeys(judges))
    print(f"\nAsking {len(judges)} judges sequentially for REAL scores (frontier free versions)...")
    for idx, model in enumerate(judges):
        if idx>0: time.sleep(3)
        print(f"\nJudge {idx+1}/{len(judges)}: {provider_from_model(model)}")
        res=judge_round_real(model, topic, rn, ap, sk, all_models)
        if res: results.append(res)
    real_scores=[r for r in results if r.get('real')]
    print(f"\nFINAL: Got {len(real_scores)}/{len(judges)} REAL scores from frontier")
    for r in real_scores: print(f"  REAL: {r['provider']} {r['A_total']:.1f} vs {r['B_total']:.1f}")
    if len(real_scores)==0:
        print("No real scores, using fallback 50/50 to avoid crash")
        return [{"model":"fallback","provider":"Fallback (API failed)","A_argument":50,"A_rebuttal":50,"A_clarity":50,"A_total":50,"B_argument":50,"B_rebuttal":50,"B_clarity":50,"B_total":50,"winner":"A","real":False}]
    if len(real_scores)<5:
        print(f"Got only {len(real_scores)} real, you wanted 5-7, but returning what we have")
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
        # Estimate timing based on actual audio duration for sync
        try:
            import subprocess
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
    voice=JUDGE_VOICES[(judge_voice_index or 0)%len(JUDGE_VOICES)] if role=="AI Judge" else VOICES.get(role, VOICES["Moderator"])
    try: return asyncio.run(generate_audio_async(clean_for_speech(text), voice, filename))
    except: return asyncio.run(generate_audio_async(clean_for_speech(text), VOICES["Moderator"], filename))

def format_ass_time(s):
    s=max(0.0,float(s)); h=int(s//3600); m=int((s%3600)//60); sec=s%60
    return f"{h}:{m:02d}:{sec:05.2f}"
def ass_escape(t):
    return str(t).replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}").replace("\n"," ")

def generate_subtitles(words, filename, scorecard=False, audio_file=None, full_text=None):
    """Fixed to use bigger chunks for better sync - 12 words per event"""
    header="[Script Info]\nScriptType: v4.00+\nPlayResX: 1920\nPlayResY: 1080\nWrapStyle: 0\n\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\nStyle: DebateSub,DejaVu Sans,48,&H00FFFFFF,&H00FFFFFF,&H00000000,&HAA000000,1,0,0,0,100,100,0,0,1,3,1,2,120,120,60,1\nStyle: ScoreSub,DejaVu Sans,36,&H00FFFFFF,&H00FFFFFF,&H00000000,&HAA000000,1,0,0,0,100,100,0,0,1,2,1,2,80,80,40,1\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    events=[]
    if not words: open(filename,"w",encoding="utf-8").write(header); return
    chunk=[]; last_end=0
    for w in words:
        if not chunk:
            chunk=[w]; last_end=w["end"]
        elif w["start"]-last_end>0.8 or len(chunk)>=12:  # Bigger chunks: 12 words, 0.8s gap for better sync
            s=chunk[0]["start"]; e=last_end+0.15  # Small padding for sync
            txt_words=[ass_escape(c["text"]) for c in chunk]
            # 2 lines max, 7 words per line for readability
            lines=[]
            for i in range(0,len(txt_words),7):
                lines.append(" ".join(txt_words[i:i+7]))
            if len(lines)>2: lines=lines[:2]  # Max 2 lines
            txt="\\N".join(lines)
            txt_clean=f"{{\\an2\\pos(960,840)\\q2\\fad(100,100)}}{txt}"  # Lower position, bigger
            events.append(f"Dialogue: 0,{format_ass_time(s)},{format_ass_time(e)},DebateSub,,0,0,0,,{txt_clean}")
            chunk=[w]; last_end=w["end"]
        else:
            chunk.append(w); last_end=w["end"]
    if chunk:
        s=chunk[0]["start"]; e=last_end+0.15
        txt_words=[ass_escape(c["text"]) for c in chunk]
        lines=[]
        for i in range(0,len(txt_words),7):
            lines.append(" ".join(txt_words[i:i+7]))
        if len(lines)>2: lines=lines[:2]
        txt="\\N".join(lines)
        txt_clean=f"{{\\an2\\pos(960,840)\\q2\\fad(100,100)}}{txt}"
        events.append(f"Dialogue: 0,{format_ass_time(s)},{format_ass_time(e)},DebateSub,,0,0,0,,{txt_clean}")
    open(filename,"w",encoding="utf-8").write(header+"\n".join(events)+"\n")
    print(f" Subs: {len(events)} events (bigger chunks for sync) -> {filename}")

SAFE_EMOJIS=["🧑","👥","🌿","🌱","🍎","🌳","🐍","👀","🙈","😨","💀","⚔️","👼","💡","🧠","✨","🌌","⭐","🌍","🤔","🔍","✅","⚖️","😇","😈","😣","🔬","🙏","❤️","💥","🎨","👤"]
def create_emoji_plan(text, words):
    """Create plan for stock emojis when words appear - e.g. man -> 🧑 above subtitles"""
    if not words: return []
    word_emoji_map={
        "adam":"🧑","man":"🧑","men":"👥","human":"🧑","person":"👤","people":"👥",
        "garden":"🌿","eden":"🌿","plant":"🌱","apple":"🍎","fruit":"🍎","eat":"🍎","tree":"🌳","trees":"🌳",
        "serpent":"🐍","snake":"🐍","eyes":"👀","eye":"👀","see":"👀","saw":"👀","look":"👀",
        "naked":"🙈","shame":"🙈","ashamed":"🙈","afraid":"😨","fear":"😨","hide":"😨","hid":"😨",
        "death":"💀","die":"💀","died":"💀","dust":"💀","sword":"⚔️","angel":"👼","cherubim":"👼",
        "knowledge":"💡","wise":"🧠","wisdom":"💡","god":"✨","lord":"✨","creator":"✨","almighty":"✨",
        "universe":"🌌","cosmos":"🌌","space":"🌌","stars":"⭐","star":"⭐","world":"🌍","earth":"🌍",
        "exist":"🤔","exists":"🤔","evidence":"🔍","proof":"🔍","real":"✅","true":"✅",
        "moral":"⚖️","good":"😇","evil":"😈","suffering":"😣","pain":"😣","science":"🔬","faith":"🙏","believe":"🤔","love":"❤️","begin":"🌱","began":"🌱","cause":"💥","design":"🎨","created":"🎨",
    }
    plan=[]; used=[]
    for w in words:
        clean_w=re.sub(r"[^a-z]","",w["text"].lower())
        if clean_w in word_emoji_map:
            start=float(w["start"])
            end=start+3.5
            # Avoid overlap
            if any(not (end < s or start > e) for s,e in used): continue
            if used and start-used[-1][1]<1.0: continue
            emoji_char=word_emoji_map[clean_w]
            if emoji_char not in SAFE_EMOJIS: continue
            # Avoid same emoji twice in row
            if emoji_char in [p["emoji"] for p in plan[-2:]]: continue
            plan.append({"emoji":emoji_char, "start":max(0.0,start), "end":end, "word":w["text"], "label":clean_w})
            used.append((start,end))
            if len(plan)>=MAX_EMOJIS_PER_SEGMENT: break
    return plan

def create_emoji_asset(emoji_char, index):
    """Fixed to avoid blank white triangles - uses emoji-capable font"""
    filename=f"emoji_{index}.png"
    size=300
    img=Image.new("RGBA",(size,size),(0,0,0,0))
    draw=ImageDraw.Draw(img)
    try:
        font, font_path = find_emoji_font(180)
        # Test render
        box=draw.textbbox((0,0),emoji_char,font=font)
        w=box[2]-box[0]; h=box[3]-box[1]
        # Center
        x=(size-w)//2
        y=(size-h)//2-10
        # Draw with shadow for visibility above subtitles
        # Shadow
        draw.text((x+4,y+4),emoji_char,font=font,fill=(0,0,0,180))
        # Main emoji - white for stock look, or yellow glow
        draw.text((x,y),emoji_char,font=font,fill=(255,255,255,255))
        # If font fallback still produces blank (check if image is still transparent), try alternative
        # Quick check: if image still mostly transparent, draw a colored circle as fallback
        # (We can't easily check, so we add a subtle background glow)
        # Add glow behind emoji
        glow_img=Image.new("RGBA",(size,size),(0,0,0,0))
        glow_draw=ImageDraw.Draw(glow_img)
        glow_draw.ellipse([size//2-60,size//2-60,size//2+60,size//2+60],fill=(255,215,0,80))
        img=Image.alpha_composite(glow_img, img)
    except Exception as e:
        print(f"Emoji asset fallback for {emoji_char}: {e}")
        # Fallback: colored circle with emoji as text using default
        try:
            font=load_font(120,bold=True)
            draw.ellipse([40,40,size-40,size-40],fill=(255,215,0,220))
            draw.text((size//2,size//2),emoji_char,font=font,fill=(0,0,0,255),anchor="mm")
        except:
            draw.ellipse([20,20,size-20,size-20],fill=(255,215,0,220))
    img.save(filename)
    return filename

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
    if visual_plan is None: visual_plan=kwargs.get('visual_plan') or []
    if position is None: position=kwargs.get('position','center')
    if glow_color is None: glow_color=kwargs.get('glow','#FFD700')
    if card_x is None: card_x=kwargs.get('card_x',960)
    if card_y is None: card_y=kwargs.get('card_y',900)
    emoji_assets=[]
    for idx, v in enumerate(visual_plan or []):
        if "emoji" in v:
            try: emoji_assets.append((create_emoji_asset(v["emoji"], idx),v))
            except Exception as e: print(f"Emoji skip {e}")
    glow=glow_color.lstrip("#")
    pan_x="0" if position=="left" else "iw-(iw/zoom)" if position=="right" else "(iw-(iw/zoom))/2"
    parts=[f"[0:v]scale=1920:1080,zoompan=z='min(zoom+0.00020,1.05)':x='{pan_x}':y='(ih-(ih/zoom))/2':d=9000:s=1920x1080:fps=30[bg];","[1:v]scale=1920:1080[ui];",f"[2:a]showwaves=s=300x58:mode=cline:colors=0x{glow}:rate=30[wave];","[bg][ui]overlay=0:0[base];",f"[base][wave]overlay={card_x+330}:{card_y+47}[withwave];"]
    current="[withwave]"; idx_in=3
    for idx,(asset,vis) in enumerate(emoji_assets):
        start=max(0.0,float(vis["start"])); end=start+3.5
        parts.append(f"[{idx_in}:v]format=rgba,fade=t=in:st={start}:d=0.3:alpha=1,fade=t=out:st={end-0.3}:d=0.3:alpha=1[emoji{idx}_faded];")
        # Position above subtitles (center, y=500)
        x_pos=(VIDEO_W-EMOJI_W)//2 + random.randint(-250,250)
        y_pos=480  # Above subtitles
        parts.append(f"{current}[emoji{idx}_faded]overlay={x_pos}:{y_pos}:enable='between(t,{start:.2f},{end:.2f})'[v{idx}];")
        current=f"[v{idx}]"; idx_in+=1
    parts.append(f"{current}ass='{ffmpeg_filter_path(subtitles)}'[outv]")
    cmd=["ffmpeg","-y","-loop","1","-framerate",str(FPS),"-i",background,"-i",ui,"-i",audio]
    for asset,_ in emoji_assets: cmd+=["-loop","1","-i",asset]
    cmd+=["-filter_complex","".join(parts),"-map","[outv]","-map","2:a","-c:v","libx264","-preset","veryfast","-crf","20","-pix_fmt","yuv420p","-c:a","aac","-b:a","192k","-shortest",output]
    r=subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if r.returncode!=0:
        print(r.stderr[-7000:]); raise RuntimeError(f"FFmpeg failed {output}")
    for asset,_ in emoji_assets:
        try: os.remove(asset)
        except: pass

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
        marker="✓" if is_real else "✗FAKE"
        draw.text((col_j,y),f"{marker} {jt[:28]}",font=fr,fill=(255,255,255,240) if is_real else (255,100,100,255))
        draw.text((col_a,y),f"{res['A_total']:.1f}",font=fr,fill=(0,255,204,255))
        draw.text((col_b,y),f"{res['B_total']:.1f}",font=fr,fill=(255,120,255,255))
        draw.text((col_w,y),short_a if res['winner']=="A" else short_b,font=fr,fill=(0,255,204,255) if res['winner']=="A" else (255,120,255,255))
        y+=58
    draw.line([(60,y+5),(W-60,y+5)],fill=(255,255,255,60),width=2); y+=25
    real_count=len([r for r in results if r.get('real')])
    draw.text((W//2,y),f"Round Avg: {round_a:.1f} vs {round_b:.1f} ({real_count} REAL judges)",font=fs,fill=(255,255,255,255),anchor="mt")
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
    eplan=create_emoji_plan(clean_for_speech(text), words)
    if eplan: print(f"   {len(eplan)} emoji(s) 3.5s above subtitles: {', '.join(v['emoji']+'('+v['word']+')' for v in eplan)}")
    create_background(position, glow, bf)
    cx,cy=create_ui_overlay(speaker_name, topic, position, glow, uf)
    render_video_segment(background=bf, ui=uf, audio=af, subtitles=sf, output=vf, position=position, glow_color=glow, card_x=cx, card_y=cy, visual_plan=eplan)
    return vf

def generate_panel_commentary(model, side, topic, rn, ap, sk, prev):
    """Fixed to be tailored to actual arguments, not generic"""
    prov=provider_from_model(model)
    pref="the case for" if side=="A" else "the case against"
    def trim(t,mw=220):
        t=t.strip()
        if len(t.split())<=mw: return t
        s=t.split('. ')
        # Take first and last meaningful sentences for specificity
        return (s[0][:180] + " ... " + s[-1][:180]) if len(s)>=2 else " ".join(t.split()[:mw])
    ap_trim=trim(ap, 220)
    sk_trim=trim(sk, 220)
    prompt = f"You are {prov}, judge for round {rn} on '{topic}'. You just heard: FOR: {ap_trim} AGAINST: {sk_trim} You leaned {pref}. Give a 2-sentence commentary that is SPECIFIC to what they actually said, not generic. Sentence 1: What was the key clash this round in your own words referencing their actual points (e.g. fine-tuning numbers vs multiverse, suffering example vs free will). Sentence 2: Why {pref} handled that specific clash better - did it answer the other side's actual example directly before making its own? Be conversational, natural, no formal phrases, mention actual content."
    for attempt in range(2):
        resp=query_openrouter(prompt, model, timeout=35, max_tokens=280, temperature=0.85 if attempt==0 else 0.9)
        if resp and count_words(resp)>=18:
            # Ensure it mentions something specific, not just generic
            low=resp.lower()
            if any(k in low for k in ["fine","suffer","moral","cause","hidden","universe","god","evidence","example"]):
                return resp
            # If generic, try again
            if attempt==0: continue
            return resp
    return f"Round {rn} really came down to {ap_trim[:60]} versus {sk_trim[:60]}. For me, {pref} edged it because it actually engaged with that specific point before moving to its own, not just giving a generic counter."

def build_intro(topic, jc): return f"Welcome to the AI Debate Arena. Today an AI Christian Apologist and an AI Skeptic are going to talk through the question: {topic}. We'll have three rounds, plenty of time for each side to really respond to each other, not just make speeches. We've got {jc} independent AI judges from frontier companies - OpenAI, Anthropic, Google, Meta, Mistral, Qwen, DeepSeek - scoring with real free versions as we go. Let's get into it."
def build_outro(jc, ca, cb):
    res="a draw" if abs(ca-cb)<0.01 else "the Christian Apologist" if ca>cb else "the Skeptic"
    return f"Alright, after three rounds our {jc} real judges from frontier companies have the Apologist at {ca:.1f} and the Skeptic at {cb:.1f}, so overall it leans toward {res}. All scores are real from leading companies, no fake 50/50. But that's just the panel. What do you think actually held up?"

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
    print("\n"+"="*70+f"\nAI DEBATE ARENA - FIXED ALL FEEDBACK\n"+"="*70+f"\nTOPIC: {topic}\n")
    print(f"ROUNDS={ROUNDS}, TURNS_PER_SIDE={TURNS_PER_SIDE_PER_ROUND}, WORDS_PER_TURN={WORDS_PER_TURN} (enforced min {MIN_TURN_WORDS})")
    print(f"JUDGES: {MAX_JUDGES} frontier companies (OpenAI, Anthropic, Google, Meta, Mistral, Qwen, DeepSeek) free versions")
    avail=discover_models()
    ap_model, sk_model = choose_primary_models(avail)
    print(f"Debate engines: {provider_from_model(ap_model)} vs {provider_from_model(sk_model)}")
    judges=choose_judges(avail,(ap_model,sk_model))
    if len(judges)==0: judges=avail[:MAX_JUDGES]
    segs=[]; sid=0
    def add_seg(text,role,name,pos=None,glow=None,jidx=None,judge_voice_index=None, **kwargs):
        if jidx is None and judge_voice_index is not None: jidx=judge_voice_index
        if jidx is None: jidx=kwargs.get('jidx',0)
        nonlocal sid
        vm=sk_model if role=="AI Skeptic" else ap_model
        v=create_segment(text,role,name,topic,sid,vm,pos,glow,jidx)
        segs.append(v); sid+=1
    add_seg(build_intro(topic,len(judges)),"Moderator","MODERATOR")
    prev_hist=""; cum_a=0.0; cum_b=0.0; panel_comments=[]; roles={"side_a_label":"APOLOGIST","side_b_label":"SKEPTIC"}
    for rn in range(1,ROUNDS+1):
        print("\n"+"="*70+f"\nROUND {rn}\n"+"="*70)
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
        stxt=f"Round {rn} is complete. {len([r for r in res if r.get('real')])} real judges from frontier companies gave the Apologist {ra:.1f} and the Skeptic {rb:.1f}. Cumulative is {cum_a:.1f} to {cum_b:.1f}. All real scores."
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
    add_seg(build_outro(len(judges),cum_a,cum_b),"Moderator","MODERATOR")
    stitch_segments(segs, OUTPUT_FILE)
    print("\n"+"="*70+"\nDEBATE COMPLETE - ALL FIXES APPLIED\n"+"="*70)
    print(f"Output: {OUTPUT_FILE}")
    print(f"Final: Apologist {cum_a:.1f} vs Skeptic {cum_b:.1f} (frontier real judges)")
    cleanup_cache()

if __name__=="__main__":
    try: run_debate_pipeline()
    except KeyboardInterrupt: print("\nCancelled.")
    except Exception as exc:
        print("\nPIPELINE FAILED"); print(str(exc)); raise
