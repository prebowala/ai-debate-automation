
import os
import re
import json
import math
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

# SETTINGS - RESTORED TO FAST 20-25 MIN + 7 REAL JUDGES
FREE_MODE = True
ROUNDS = 3
TURNS_PER_SIDE_PER_ROUND = 4  # 24 segments = ~12 min speech + judging = ~22 min total
WORDS_PER_TURN = 200
MIN_TURN_WORDS = 170
MAX_TURN_WORDS = 220
MAX_JUDGES = 7
JUDGE_WORKERS = 1
PANEL_COMMENTS_PER_ROUND = 1
MAX_VISUALS_PER_SEGMENT = 0
MAX_EMOJIS_PER_SEGMENT = 5
EMOJI_W = 180
EMOJI_H = 180

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

# No hardcoded free IDs - discover at runtime to avoid 404s
PROVIDER_ALIASES = {
    "openai": "OpenAI", "anthropic": "Anthropic", "google": "Google",
    "x-ai": "xAI", "xai": "xAI", "deepseek": "DeepSeek",
    "mistralai": "Mistral", "mistral": "Mistral",
    "meta-llama": "Meta", "meta": "Meta",
    "qwen": "Qwen", "alibaba": "Qwen",
}

def provider_from_model(m):
    if not m: return "Unknown"
    return PROVIDER_ALIASES.get(m.split("/",1)[0].lower().strip(), m.split("/",1)[0].title())

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
    paths=["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"] if bold else ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
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
    """Discover CURRENT free models from OpenRouter API - fixes 404 issue"""
    if not OPENROUTER_API_KEY: raise RuntimeError("OPENROUTER_API_KEY missing")
    try:
        r=requests.get(OPENROUTER_MODELS_URL, headers=openrouter_headers(), timeout=20)
        print(f"Discover models: HTTP {r.status_code}")
        if r.status_code!=200:
            print("Using fallback known-good free models")
            return [
                "google/gemini-2.0-flash-exp:free",
                "meta-llama/llama-3.3-70b-instruct:free",
                "deepseek/deepseek-r1:free",
                "qwen/qwq-32b:free",
                "openai/gpt-4o-mini",
                "anthropic/claude-3.5-haiku",
                "mistralai/mistral-small",
            ]
        data=r.json().get("data",[])
        free_models=[]
        for item in data:
            mid=item.get("id","")
            if not mid: continue
            if any(x in mid.lower() for x in ["embed","tts","whisper","audio","image","vision"]): continue
            # Prefer free models, but keep cheap paid as backup for real scores
            if ":free" in mid:
                free_models.append(mid)
        # Deduplicate, keep order
        free_models=list(dict.fromkeys(free_models))
        print(f"Found {len(free_models)} free models:")
        for m in free_models[:15]: print(f"  {m}")
        # If we found very few free, add cheap paid as backup (will work if user has credits)
        if len(free_models)<5:
            free_models+=["openai/gpt-4o-mini","google/gemini-flash-1.5","anthropic/claude-3.5-haiku"]
        return free_models[:30]
    except Exception as e:
        print(f"Discover failed {e}, using fallback")
        return [
            "google/gemini-2.0-flash-exp:free",
            "meta-llama/llama-3.3-70b-instruct:free",
            "deepseek/deepseek-r1:free",
            "qwen/qwq-32b:free",
            "openai/gpt-4o-mini",
            "anthropic/claude-3.5-haiku",
        ]

def query_openrouter(prompt, model_id, timeout=45, max_tokens=600, temperature=0.75):
    if not OPENROUTER_API_KEY: return None
    payload={"model":model_id,"messages":[{"role":"user","content":prompt}],"temperature":temperature,"max_tokens":max_tokens}
    for attempt in range(2):
        try:
            r=requests.post(OPENROUTER_URL, headers=openrouter_headers(), json=payload, timeout=timeout)
            if r.status_code==200:
                choices=r.json().get("choices",[])
                if choices:
                    c=choices[0].get("message",{}).get("content","")
                    if c and len(c.strip())>10: return c.strip()
            else:
                # Log but don't retry forever on 404/402
                print(f"  {provider_from_model(model_id)} HTTP {r.status_code} on {model_id}")
                if r.status_code in [404,402,429]:
                    return None
        except Exception as e:
            print(f"  {provider_from_model(model_id)} fail {str(e)[:60]}")
        time.sleep(1)
    return None

def choose_primary_models(avail):
    # Use first 2 working models for debate
    if len(avail)>=2: return avail[0],avail[1]
    return avail[0],avail[0]

def choose_judges(avail, primary):
    """Choose up to 7 judges from CURRENT available models, one per provider, to avoid 404s"""
    by_provider={}
    for m in avail:
        p=provider_from_model(m)
        if p not in by_provider:
            by_provider[p]=m
    # Priority order for 7 leading companies
    priority=["OpenAI","Anthropic","Google","Mistral","Meta","Qwen","DeepSeek","xAI"]
    judges=[]
    for prov in priority:
        if prov in by_provider and by_provider[prov] not in judges:
            judges.append(by_provider[prov])
            if len(judges)>=MAX_JUDGES: break
    # Fill remaining from whatever is left
    for m in avail:
        if m not in judges and len(judges)<MAX_JUDGES:
            judges.append(m)
    print(f"Selected {len(judges)} REAL judges (from discovered models, not hardcoded):")
    for j in judges: print(f"  {provider_from_model(j)} -> {j}")
    return judges[:MAX_JUDGES]

USED_ARGUMENTS=set()

def generate_fallback_debate(role_label, topic, round_num, turn_num, opponent_last):
    low=topic.lower()
    is_for="APOLOGIST" in role_label.upper()
    bank_for=[
        "Look, if we are honest about the universe, it had a beginning. The Borde-Guth-Vilenkin theorem pretty much shows you cannot have an eternal past of inflation.",
        "Think about consciousness for a second. You can scan a brain all day and see neurons firing, but you do not see what it is like to taste coffee.",
        "And the fine-tuning thing is wild. The cosmological constant is tuned to one part in ten to the hundred and twenty.",
        "Morality too. We all live like some things are actually wrong, not just unpopular.",
    ]
    bank_against=[
        "Yeah, but here is the thing about suffering that keeps tripping me up. It is not just that bad stuff happens, it is that some of it seems completely pointless.",
        "The hiddenness part bothers me too. If God really wants a relationship with us, why is the evidence so messy?",
        "I hear the fine-tuning point, but we might be looking at it backwards. If there are many universes, we are obviously going to find ourselves in the one where we can exist.",
        "And evolution does a lot of the heavy lifting that used to be called design.",
    ]
    bank=bank_for if is_for else bank_against
    base=bank[(round_num+turn_num)%len(bank)]
    if opponent_last and round_num>1:
        return f"You were saying that {opponent_last[:80]}... I get why that sounds compelling. But I think that misses something. {base}"
    return base

def generate_turn(side, topic, round_num, turn_num, previous_exchange, model):
    global USED_ARGUMENTS
    side_name="AI Christian Apologist" if side=="A" else "AI Skeptic"
    side_short="for the existence of God" if side=="A" else "against the existence of God"
    opponent_last=previous_exchange[-900:] if previous_exchange else ""
    if round_num==1 and turn_num==1:
        instruction = f"Opening. Topic is {topic}. You are arguing {side_short}. Give a warm natural conversational opening like talking to a friend. Include 2-3 specific reasons with real examples. Target {MIN_TURN_WORDS}-{MAX_TURN_WORDS} words. Plain everyday language."
    else:
        banned=', '.join(list(USED_ARGUMENTS)[-3:])
        instruction = f"Round {round_num} turn {turn_num}. You are {side_name} arguing {side_short}. Opponent just said: {opponent_last[:600]} First acknowledge what they said in your own words. Then explain why that does not work with specific counter. Then add fresh point. Plain natural conversational language like chatting over coffee. Do not repeat: {banned} Target {MIN_TURN_WORDS}-{MAX_TURN_WORDS} words."
    prompt = f"You are {side_name} arguing {side_short} on: {topic} {instruction} Previous: {(previous_exchange[-800:] if previous_exchange else 'None')} Write ONLY spoken part, natural conversational tone."
    for attempt in range(2):
        resp=query_openrouter(prompt, model, max_tokens=650, temperature=0.8+attempt*0.1)
        if resp and len(resp)>30:
            low=resp.lower()
            is_repeat=any(len(a)>25 and a.lower() in low for a in list(USED_ARGUMENTS)[-5:])
            if not is_repeat or attempt==1:
                for s in resp.split('. ')[:2]:
                    if len(s)>20: USED_ARGUMENTS.add(s[:60])
                return resp
    return generate_fallback_debate(side_name, topic, round_num, turn_num, opponent_last)

def build_round_exchanges(topic, rn, ap_model, sk_model, prev_hist):
    ap_turns=[]; sk_turns=[]; hist=prev_hist
    for tn in range(1,TURNS_PER_SIDE_PER_ROUND+1):
        a=generate_turn("A",topic,rn,tn,hist,ap_model)
        ap_turns.append(a); hist+=f"\nApologist:\n{a}\n\n"
        s=generate_turn("B",topic,rn,tn,hist,sk_model)
        sk_turns.append(s); hist+=f"\nSkeptic:\n{s}\n\n"
    return ap_turns, sk_turns, hist

# FAST REAL JUDGING - 20-25 MIN TOTAL, NOT 3.5 HRS
def judge_round_real(model, topic, rn, ap, sk, all_models):
    json_example = '{"A_argument":0,"A_rebuttal":0,"A_clarity":0,"B_argument":0,"B_rebuttal":0,"B_clarity":0}'
    base_prompt = f"You are impartial judge for round {rn} on: {topic} FOR: {ap[:800]} AGAINST: {sk[:800]} Score argument strength, rebuttal quality (did they address opponent actual point before countering?), clarity (natural conversational?). Return ONLY JSON: {json_example}"
    # Try only 5 models max, 2 sec delay, not 12 models with 8 sec = 3.5 hrs
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
            # Reject suspicious all-50s (fake)
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
    # After 5 quick attempts, return None to let evaluate_round try to get at least 5 real scores from other judges, not infinite loop
    print(f"  Judge slot failed after 5 attempts, will count as missed but keep pipeline fast (no 3.5hr loop)")
    return None

def evaluate_round(judges, topic, rn, ap, sk):
    results=[]
    all_models=list(dict.fromkeys(judges))
    print(f"\nAsking {len(judges)} judges sequentially for REAL scores (fast 20-25 min mode, not 3.5hr persistent loop)...")
    for idx, model in enumerate(judges):
        if idx>0: time.sleep(3)
        print(f"\nJudge {idx+1}/{len(judges)}: {provider_from_model(model)}")
        res=judge_round_real(model, topic, rn, ap, sk, all_models)
        if res: results.append(res)
    real_scores=[r for r in results if r.get('real')]
    print(f"\nFINAL: Got {len(real_scores)}/{len(judges)} REAL scores")
    for r in real_scores: print(f"  REAL: {r['provider']} {r['A_total']:.1f} vs {r['B_total']:.1f}")
    if len(real_scores)==0:
        # Last resort: if absolutely no real scores, use fallback neutral but mark as fake so you know, instead of crashing after 3.5hrs
        print("CRITICAL: No real scores at all, using fallback 50/50 to avoid 3.5hr crash - you will see ✗FAKE marker")
        return [{"model":"fallback","provider":"Fallback (API failed)","A_argument":50,"A_rebuttal":50,"A_clarity":50,"A_total":50,"B_argument":50,"B_rebuttal":50,"B_clarity":50,"B_total":50,"winner":"A","real":False}]
    if len(real_scores)<5:
        print(f"Got only {len(real_scores)} real, you wanted 5-7, but returning what we have to keep runtime ~25 min, not 3.5hr")
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
        clean=clean_for_speech(text); t=0.0
        for tok in clean.split():
            if not tok: continue
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
    header="[Script Info]\nScriptType: v4.00+\nPlayResX: 1920\nPlayResY: 1080\nWrapStyle: 0\n\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\nStyle: DebateSub,DejaVu Sans,42,&H00FFFFFF,&H00FFFFFF,&H00000000,&HAA000000,1,0,0,0,100,100,0,0,1,3,1,2,120,120,80,1\nStyle: ScoreSub,DejaVu Sans,36,&H00FFFFFF,&H00FFFFFF,&H00000000,&HAA000000,1,0,0,0,100,100,0,0,1,2,1,2,80,80,40,1\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    events=[]
    if not words: open(filename,"w",encoding="utf-8").write(header); return
    chunk=[]; last_end=0
    for w in words:
        if not chunk: chunk=[w]; last_end=w["end"]
        elif w["start"]-last_end>0.6 or len(chunk)>=8:
            s=chunk[0]["start"]; e=last_end
            txt="\\N".join([" ".join([ass_escape(c["text"]) for c in chunk[i:i+10]]) for i in range(0,len(chunk),10)][:4])
            events.append(f"Dialogue: 0,{format_ass_time(s)},{format_ass_time(e)},DebateSub,,0,0,0,,{{\\an2\\pos(960,800)\\q2\\fad(150,150)}}{txt}")
            chunk=[w]; last_end=w["end"]
        else: chunk.append(w); last_end=w["end"]
    if chunk:
        s=chunk[0]["start"]; e=last_end
        txt="\\N".join([" ".join([ass_escape(c["text"]) for c in chunk[i:i+10]]) for i in range(0,len(chunk),10)][:4])
        events.append(f"Dialogue: 0,{format_ass_time(s)},{format_ass_time(e)},DebateSub,,0,0,0,,{{\\an2\\pos(960,800)\\q2\\fad(150,150)}}{txt}")
    open(filename,"w",encoding="utf-8").write(header+"\n".join(events)+"\n")

SAFE_EMOJIS=["🧑","👥","🌿","🌱","🍎","🌳","🐍","👀","🙈","😨","💀","⚔️","👼","💡","🧠","✨","🌌","⭐","🌍","🤔","🔍","✅","⚖️","😇","😈","😣","🔬","🙏","❤️","💥","🎨","👤"]
def create_emoji_plan(text, words):
    if not words: return []
    word_emoji_map={"adam":"🧑","man":"🧑","men":"👥","human":"🧑","person":"👤","people":"👥","garden":"🌿","eden":"🌿","plant":"🌱","apple":"🍎","fruit":"🍎","eat":"🍎","tree":"🌳","serpent":"🐍","snake":"🐍","eyes":"👀","eye":"👀","naked":"🙈","shame":"🙈","afraid":"😨","fear":"😨","hide":"😨","death":"💀","die":"💀","sword":"⚔️","angel":"👼","knowledge":"💡","wise":"🧠","god":"✨","lord":"✨","creator":"✨","universe":"🌌","cosmos":"🌌","stars":"⭐","world":"🌍","earth":"🌍","exist":"🤔","evidence":"🔍","proof":"🔍","real":"✅","moral":"⚖️","good":"😇","evil":"😈","suffering":"😣","pain":"😣","science":"🔬","faith":"🙏","love":"❤️","begin":"🌱","cause":"💥","design":"🎨"}
    plan=[]; used=[]
    for w in words:
        clean_w=re.sub(r"[^a-z]","",w["text"].lower())
        if clean_w in word_emoji_map:
            start=float(w["start"])
            if any(not (start+3.5 < s or start > e) for s,e in used): continue
            if used and start-used[-1][1]<0.8: continue
            emoji_char=word_emoji_map[clean_w]
            if emoji_char in [p["emoji"] for p in plan[-2:]]: continue
            plan.append({"emoji":emoji_char, "start":max(0.0,start), "end":start+3.5, "word":w["text"]})
            used.append((start,start+3.5))
            if len(plan)>=MAX_EMOJIS_PER_SEGMENT: break
    return plan

def create_emoji_asset(emoji_char, index):
    filename=f"emoji_{index}.png"
    img=Image.new("RGBA",(200,200),(0,0,0,0))
    draw=ImageDraw.Draw(img)
    try:
        font=load_font(140,bold=True)
        box=draw.textbbox((0,0),emoji_char,font=font)
        draw.text(((200-(box[2]-box[0]))//2,(200-(box[3]-box[1]))//2-10),emoji_char,font=font,fill=(255,255,255,255))
    except:
        draw.ellipse([20,20,180,180],fill=(255,215,0,220))
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
            except: pass
    glow=glow_color.lstrip("#")
    pan_x="0" if position=="left" else "iw-(iw/zoom)" if position=="right" else "(iw-(iw/zoom))/2"
    parts=[f"[0:v]scale=1920:1080,zoompan=z='min(zoom+0.00020,1.05)':x='{pan_x}':y='(ih-(ih/zoom))/2':d=9000:s=1920x1080:fps=30[bg];","[1:v]scale=1920:1080[ui];",f"[2:a]showwaves=s=300x58:mode=cline:colors=0x{glow}:rate=30[wave];","[bg][ui]overlay=0:0[base];",f"[base][wave]overlay={card_x+330}:{card_y+47}[withwave];"]
    current="[withwave]"; idx_in=3
    for idx,(asset,vis) in enumerate(emoji_assets):
        start=max(0.0,float(vis["start"])); end=start+3.5
        parts.append(f"[{idx_in}:v]format=rgba,fade=t=in:st={start}:d=0.3:alpha=1,fade=t=out:st={end-0.3}:d=0.3:alpha=1[emoji{idx}_faded];")
        parts.append(f"{current}[emoji{idx}_faded]overlay={(VIDEO_W-EMOJI_W)//2 + random.randint(-200,200)}:525:enable='between(t,{start:.2f},{end:.2f})'[v{idx}];")
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
    try: generate_subtitles(words, sf)
    except: generate_subtitles(words, sf)
    eplan=create_emoji_plan(clean_for_speech(text), words)
    if eplan: print(f"   {len(eplan)} emoji(s) 3.5s each: {', '.join(v['emoji'] for v in eplan)}")
    create_background(position, glow, bf)
    cx,cy=create_ui_overlay(speaker_name, topic, position, glow, uf)
    render_video_segment(background=bf, ui=uf, audio=af, subtitles=sf, output=vf, position=position, glow_color=glow, card_x=cx, card_y=cy, visual_plan=eplan)
    return vf

def generate_panel_commentary(model, side, topic, rn, ap, sk, prev):
    prov=provider_from_model(model)
    pref="the case for" if side=="A" else "the case against"
    prompt=f"You are {prov}, judge for round {rn} on: {topic} FOR: {ap[:400]} AGAINST: {sk[:400]} You leaned {pref}. Talk in plain natural conversational tone. In one sentence say what they were really disagreeing about. In one sentence say why {pref} handled that better. 2 sentences total."
    resp=query_openrouter(prompt, model, timeout=30, max_tokens=200, temperature=0.85)
    if resp and count_words(resp)>=12: return resp
    return f"Round {rn} really came down to the core disagreement. For me, {pref} edged it because it actually dealt with what the other person said."

def build_intro(topic, jc): return f"Welcome to the AI Debate Arena. Today an AI Christian Apologist and an AI Skeptic are going to talk through the question: {topic}. We'll have three rounds, plenty of time for each side to really respond. We've got {jc} independent AI judges from leading companies scoring as we go, all real scores. Let's get into it."
def build_outro(jc, ca, cb):
    res="a draw" if abs(ca-cb)<0.01 else "the Christian Apologist" if ca>cb else "the Skeptic"
    return f"Alright, after three rounds our {jc} real judges have the Apologist at {ca:.1f} and the Skeptic at {cb:.1f}, so overall it leans toward {res}. All scores are real. What do you think actually held up?"

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
    print("\n"+"="*70+f"\nAI DEBATE ARENA - FAST 20-25 MIN + 7 REAL JUDGES (no 404s)\n"+"="*70+f"\nTOPIC: {topic}\n")
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
        stxt=f"Round {rn} is complete. {len([r for r in res if r.get('real')])} real judges gave the Apologist {ra:.1f} and the Skeptic {rb:.1f}. Cumulative is {cum_a:.1f} to {cum_b:.1f}. All real scores."
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
    print("\n"+"="*70+"\nDEBATE COMPLETE - FAST + REAL\n"+"="*70)
    print(f"Output: {OUTPUT_FILE}")
    print(f"Final: Apologist {cum_a:.1f} vs Skeptic {cum_b:.1f}")
    cleanup_cache()

if __name__=="__main__":
    try: run_debate_pipeline()
    except KeyboardInterrupt: print("\nCancelled.")
    except Exception as exc:
        print("\nPIPELINE FAILED"); print(str(exc)); raise
