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

FREE_MODE = True
ROUNDS = 3
TURNS_PER_SIDE_PER_ROUND = 4
WORDS_PER_TURN = 200
MIN_TURN_WORDS = 170
MAX_TURN_WORDS = 220
WORDS_PER_SIDE_PER_ROUND = 800

MAX_JUDGES = 3 if FREE_MODE else 5
JUDGE_WORKERS = 5
PANEL_COMMENTS_PER_ROUND = 1

MAX_VISUALS_PER_SEGMENT = 0
MIN_VISUAL_GAP = 1.5
VISUAL_X = 700
VISUAL_Y = 525
VISUAL_W = 520
VISUAL_H = 245
MAX_EMOJIS_PER_SEGMENT = 5
EMOJI_W = 180
EMOJI_H = 180

VOICES = {
    "Moderator": "en-US-AndrewMultilingualNeural",
    "AI Christian Apologist": "en-US-BrianMultilingualNeural",
    "AI Skeptic": "en-US-AvaMultilingualNeural",
    "AI Judge 1": "en-US-ChristopherNeural",
    "AI Judge 2": "en-US-EmmaMultilingualNeural",
    "AI Judge 3": "en-US-GuyNeural",
}
JUDGE_VOICES = [
    "en-US-ChristopherNeural",
    "en-US-EmmaMultilingualNeural",
    "en-US-GuyNeural",
    "en-GB-RyanNeural",
    "en-AU-WilliamNeural",
]

FALLBACK_MODELS = [
    "openai/gpt-4o-mini:free",
    "google/gemini-2.0-flash-001:free",
    "anthropic/claude-3-haiku:free",
    "mistralai/mistral-small:free",
    "qwen/qwen-2.5-72b-instruct:free",
]

PROVIDER_ALIASES = {"openai": "OpenAI", "anthropic": "Anthropic", "google": "Google", "x-ai": "xAI", "xai": "xAI", "deepseek": "DeepSeek", "mistralai": "Mistral", "meta-llama": "Meta", "qwen": "Qwen"}
def provider_from_model(m):
    if not m: return "Unknown"
    return PROVIDER_ALIASES.get(m.split("/",1)[0].lower().strip(), m.split("/",1)[0].title())
def cleanup_cache():
    print("Cleaning...")
    patterns=["*.mp4","*.mp3","*.ass","*.png","*.gif","*_list.txt"]
    protected={OUTPUT_FILE,"background.png","topic.txt"}
    for pat in patterns:
        for f in glob.glob(pat):
            if f in protected: continue
            try: os.remove(f)
            except: pass
def count_words(t): return len(re.findall(r"\b[\w'-]+\b", t or ""))
def clean_for_speech(t):
    t=re.sub(r"\([^)]*\)","",t or "")
    for o,n in {"*":"","#":"","_":"","`":"","–":"-","—":"-","\"":"",":":" ", ";":" ", "&":"and"}.items(): t=t.replace(o,n)
    t=re.sub(r"\s+"," ",t)
    return t.strip()
def clamp_score(v):
    try: v=float(v)
    except: v=50.0
    return max(0.0,min(100.0,v))
def load_font(sz,bold=False):
    paths=["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf","/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"] if bold else ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf","/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"]
    for p in paths:
        try: return ImageFont.truetype(p,sz)
        except: continue
    return ImageFont.load_default()
def hex_to_rgba(h,a):
    h=h.lstrip("#")
    return (int(h[0:2],16),int(h[2:4],16),int(h[4:6],16),a)

def openrouter_headers(): return {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json", "HTTP-Referer": "https://openrouter.ai/", "X-Title": "AI Debate Arena"}
def discover_models():
    if not OPENROUTER_API_KEY: raise RuntimeError("OPENROUTER_API_KEY missing")
    try:
        r=requests.get(OPENROUTER_MODELS_URL, headers=openrouter_headers(), timeout=20)
        if r.status_code!=200: return []
        models=[]
        for item in r.json().get("data",[]):
            mid=item.get("id")
            if not mid: continue
            if any(x in mid.lower() for x in ["embed","tts","whisper","audio","image","vision"]): continue
            models.append(mid)
        return list(dict.fromkeys(models))
    except: return []
def query_openrouter(prompt, model_id, timeout=60, max_tokens=900, temperature=0.75):
    if not OPENROUTER_API_KEY: return None
    payload={"model":model_id,"messages":[{"role":"user","content":prompt}],"temperature":temperature,"max_tokens":max_tokens}
    for attempt in range(2):
        try:
            r=requests.post(OPENROUTER_URL, headers=openrouter_headers(), json=payload, timeout=timeout)
            if r.status_code==200:
                data=r.json()
                choices=data.get("choices",[])
                if choices:
                    c=choices[0].get("message",{}).get("content","")
                    if c and len(c.strip())>20: return c.strip()
            else:
                print(f"  {provider_from_model(model_id)} HTTP {r.status_code}")
                if r.status_code==402: return None
        except Exception as e: print(f"  {provider_from_model(model_id)} fail {str(e)[:80]}")
        time.sleep(1.0)
    return None

def choose_primary_models(avail):
    pref=["openai/gpt-4o-mini:free","google/gemini-2.0-flash-001:free","anthropic/claude-3-haiku:free","mistralai/mistral-small:free"]
    found=[m for m in pref if m in set(avail)]
    if len(found)>=2: return found[0],found[1]
    if len(found)==1 and len(avail)>1:
        remaining=[m for m in avail if m!=found[0]]
        return found[0], remaining[0]
    if len(avail)>=2: return avail[0],avail[1]
    return FALLBACK_MODELS[0],FALLBACK_MODELS[1]

def choose_judges(avail, primary):
    excluded=set(primary)
    cands=[m for m in avail if m not in excluded]
    groups={}
    for m in cands:
        p=provider_from_model(m)
        groups.setdefault(p,[]).append(m)
    selected=[]
    for prov,mods in groups.items():
        mods.sort(key=lambda x: len(x))
        selected.append((prov,mods[0]))
    priority=["OpenAI","Anthropic","Google","xAI","Mistral","Meta"]
    selected.sort(key=lambda x: (priority.index(x[0]) if x[0] in priority else 999, x[0]))
    return [m for _,m in selected[:MAX_JUDGES]]

USED_ARGUMENTS=set()

def generate_fallback_debate(role_label, topic, round_num, turn_num, opponent_last):
    low=topic.lower()
    is_for="APOLOGIST" in role_label.upper()
    if "god" in low and "exist" in low:
        if is_for:
            bank=[
                "Look, if we are honest about the universe, it had a beginning. The Borde-Guth-Vilenkin theorem pretty much shows you cannot have an eternal past of inflation. So whatever started everything cannot be inside time and space. That is not a trick, that is just what beginning means.",
                "Think about consciousness for a second. You can scan a brain all day and see neurons firing, but you do not see what it is like to taste coffee or feel love. Material stuff does not explain subjective experience. There is a real gap there that keeps showing up.",
                "And the fine-tuning thing is wild when you actually look at the numbers. The cosmological constant is tuned to one part in ten to the hundred and twenty. If that was slightly different, no stars, no planets, no us. Saying that is just luck feels like saying you found a fully functioning watch in the desert and calling it wind.",
                "Morality too. We all live like some things are actually wrong, not just unpopular. If there is no deeper grounding, then it is just preference, but we do not treat torture that way. We treat it as wrong even if a whole culture says it is fine.",
            ]
        else:
            bank=[
                "Yeah, but here is the thing about suffering that keeps tripping me up. It is not just that bad stuff happens, it is that some of it seems completely pointless. A deer burning for days in a forest fire where no one learns anything from it. If you could stop that and you cared, you would. So if God is all-powerful and all-loving, why does not he?",
                "The hiddenness part bothers me too. If God really wants a relationship with us, why is the evidence so messy? You have got thousands of religions all saying they have the truth, and sincere people in different cultures finding totally different gods. If I wanted to be known, I would not make it a puzzle.",
                "I hear the fine-tuning point, but we might be looking at it backwards. If there are many universes with different constants, we are obviously going to find ourselves in the one where we can exist. That is not design, that is selection bias. And we do not actually know if those constants could have been different.",
                "And evolution does a lot of the heavy lifting that used to be called design. Complex eyes, wings, brains - they build up slowly over billions of years. You do not need a designer in the gap if natural processes can do it step by step.",
            ]
    else:
        if is_for:
            bank=[f"On {topic}, when you line up the different pieces - not just one argument but several - they start pointing the same way. It is not about a single proof, it is about which story makes the most sense of everything we see."]
        else:
            bank=[f"When I look at {topic}, I keep asking what the simplest explanation is. Extraordinary claims need good evidence, and personal experience alone is tricky because our brains are really good at seeing patterns that are not there."]

    idx=(round_num+turn_num)%len(bank)
    base=bank[idx]
    if opponent_last and round_num>1:
        return f"You were saying that {opponent_last[:80]}... I get why that sounds compelling. But I think that misses something. {base} So if I am looking at your point directly, I do not think it holds up the way it first seems."
    return base

def generate_turn(side, topic, round_num, turn_num, previous_exchange, model):
    global USED_ARGUMENTS
    if side=="A":
        side_name="AI Christian Apologist"
        side_short="for the existence of God"
    else:
        side_name="AI Skeptic"
        side_short="against the existence of God"

    opponent_last=previous_exchange[-900:] if previous_exchange else ""

    if round_num==1 and turn_num==1:
        instruction = "Opening. Topic is " + topic + ". You are arguing " + side_short + ". Give a warm natural conversational opening like talking to a friend, not reading a textbook. Include 2-3 specific reasons with real examples, numbers, or stories. Target " + str(MIN_TURN_WORDS) + "-" + str(MAX_TURN_WORDS) + " words. Plain everyday language."
    else:
        banned_str = ', '.join(list(USED_ARGUMENTS)[-3:])
        instruction = "Round " + str(round_num) + " turn " + str(turn_num) + ". You are " + side_name + " arguing " + side_short + ". Opponent just said: " + opponent_last[:600] + " First, acknowledge what they actually said in your own words to show you heard them. Then explain why that does not work with a specific counter example. Then add fresh point. Plain natural conversational language like chatting over coffee, no bullet points. Do not repeat: " + banned_str + " Target " + str(MIN_TURN_WORDS) + "-" + str(MAX_TURN_WORDS) + " words. Start naturally."

    prompt = "You are " + side_name + " arguing " + side_short + " on: " + topic + " " + instruction + " Previous for context: " + (previous_exchange[-800:] if previous_exchange else "None") + " Write ONLY your spoken part, natural conversational tone."

    for attempt in range(2):
        resp=query_openrouter(prompt, model, max_tokens=650, temperature=0.8+attempt*0.1)
        if not resp: continue
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
        ap_turns.append(a); hist+=f"\nAI Christian Apologist:\n{a}\n\n"
        s=generate_turn("B",topic,rn,tn,hist,sk_model)
        sk_turns.append(s); hist+=f"\nAI Skeptic:\n{s}\n\n"
    return ap_turns, sk_turns, hist

def neutral_judge(m): return {"model":m,"provider":provider_from_model(m),"A_argument":50,"A_rebuttal":50,"A_clarity":50,"A_total":50,"B_argument":50,"B_rebuttal":50,"B_clarity":50,"B_total":50,"winner":"A"}
def judge_round(model, topic, rn, ap, sk):
    prompt=f"You are impartial judge for round {rn} on: {topic} FOR: {ap[:900]} AGAINST: {sk[:900]} Score argument strength, rebuttal quality (did they address opponent point before countering?), clarity (natural conversational?). Return ONLY JSON: {{\"A_argument\":0,\"A_rebuttal\":0,\"A_clarity\":0,\"B_argument\":0,\"B_rebuttal\":0,\"B_clarity\":0}}"
    resp=query_openrouter(prompt, model, timeout=30, max_tokens=250, temperature=0.1)
    if not resp: return neutral_judge(model)
    try:
        m=re.search(r"\{.*\}",resp,re.DOTALL)
        if not m: return neutral_judge(model)
        d=json.loads(m.group(0))
        aa=clamp_score(d.get("A_argument",50)); ar=clamp_score(d.get("A_rebuttal",50)); ac=clamp_score(d.get("A_clarity",50))
        ba=clamp_score(d.get("B_argument",50)); br=clamp_score(d.get("B_rebuttal",50)); bc=clamp_score(d.get("B_clarity",50))
        at=(aa+ar+ac)/3; bt=(ba+br+bc)/3
        return {"model":model,"provider":provider_from_model(model),"A_argument":aa,"A_rebuttal":ar,"A_clarity":ac,"A_total":round(at,2),"B_argument":ba,"B_rebuttal":br,"B_clarity":bc,"B_total":round(bt,2),"winner":"A" if at>bt else "B"}
    except: return neutral_judge(model)

def evaluate_round(judges, topic, rn, ap, sk):
    results=[]; print(f"Asking {len(judges)} judges...")
    def worker(m): return judge_round(m,topic,rn,ap,sk)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1,min(JUDGE_WORKERS,len(judges)))) as ex:
        futs={ex.submit(worker,m):m for m in judges}
        for f in concurrent.futures.as_completed(futs):
            try:
                r=f.result(); results.append(r)
                print(f"   {r['provider']} {r['A_total']:.1f} vs {r['B_total']:.1f}")
            except Exception as e: print(f"   {str(e)[:80]}")
    if not results: results=[neutral_judge("fallback")]
    return results

def calculate_round_average(res):
    a=sum(r["A_total"] for r in res)/len(res)
    b=sum(r["B_total"] for r in res)/len(res)
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
    except Exception as e:
        print(f"TTS fail {voice}: {str(e)[:100]}")
        return asyncio.run(generate_audio_async(clean_for_speech(text), VOICES["Moderator"], filename))

def format_ass_time(s):
    s=max(0.0,float(s)); h=int(s//3600); m=int((s%3600)//60); sec=s%60
    return f"{h}:{m:02d}:{sec:05.2f}"
def ass_escape(t):
    t=str(t); return t.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}").replace("\n"," ")

def generate_subtitles(words, filename, scorecard=False, audio_file=None, full_text=None):
    header="[Script Info]\nScriptType: v4.00+\nPlayResX: 1920\nPlayResY: 1080\nWrapStyle: 0\n\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\nStyle: DebateSub,DejaVu Sans,42,&H00FFFFFF,&H00FFFFFF,&H00000000,&HAA000000,1,0,0,0,100,100,0,0,1,3,1,2,120,120,80,1\nStyle: ScoreSub,DejaVu Sans,36,&H00FFFFFF,&H00FFFFFF,&H00000000,&HAA000000,1,0,0,0,100,100,0,0,1,2,1,2,80,80,40,1\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    events=[]
    if not words: open(filename,"w",encoding="utf-8").write(header); return
    chunk=[]; last_end=0
    for w in words:
        if not chunk: chunk=[w]; last_end=w["end"]
        elif w["start"]-last_end>0.6 or len(chunk)>=8:
            s=chunk[0]["start"]; e=last_end
            txt_words=[ass_escape(c["text"]) for c in chunk]
            lines=[]
            for i in range(0,len(txt_words),10): lines.append(" ".join(txt_words[i:i+10]))
            if len(lines)>4: lines=lines[:4]
            txt="\\N".join(lines)
            txt_clean=f"{{\\an2\\pos(960,800)\\q2\\fad(150,150)}}{txt}"
            events.append(f"Dialogue: 0,{format_ass_time(s)},{format_ass_time(e)},DebateSub,,0,0,0,,{txt_clean}")
            chunk=[w]; last_end=w["end"]
        else: chunk.append(w); last_end=w["end"]
    if chunk:
        s=chunk[0]["start"]; e=last_end
        txt_words=[ass_escape(c["text"]) for c in chunk]
        lines=[]
        for i in range(0,len(txt_words),10): lines.append(" ".join(txt_words[i:i+10]))
        if len(lines)>4: lines=lines[:4]
        txt="\\N".join(lines)
        txt_clean=f"{{\\an2\\pos(960,800)\\q2\\fad(150,150)}}{txt}"
        events.append(f"Dialogue: 0,{format_ass_time(s)},{format_ass_time(e)},DebateSub,,0,0,0,,{txt_clean}")
    open(filename,"w",encoding="utf-8").write(header+"\n".join(events)+"\n")
    print(f" Subs: {len(events)} events -> {filename}")

def get_audio_duration(p):
    try:
        r=subprocess.run(["ffprobe","-v","error","-show_entries","format=duration","-of","default=noprint_wrappers=1:nokey=1",p],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=10)
        return float(r.stdout.strip())
    except: return 0.0

SAFE_EMOJIS=["🧑","👥","🌿","🌱","🍎","🌳","🐍","👀","🙈","😨","💀","⚔️","👼","💡","🧠","✨","🌌","⭐","🌍","🤔","🔍","✅","⚖️","😇","😈","😣","🔬","🙏","❤️","💥","🎨","👤"]
def create_emoji_plan(text, words):
    if not words: return []
    word_emoji_map={
        "adam":"🧑","man":"🧑","men":"👥","human":"🧑","person":"👤","people":"👥",
        "garden":"🌿","eden":"🌿","plant":"🌱","apple":"🍎","fruit":"🍎","eat":"🍎","tree":"🌳","trees":"🌳",
        "serpent":"🐍","snake":"🐍","eyes":"👀","eye":"👀","see":"👀","naked":"🙈","shame":"🙈",
        "afraid":"😨","fear":"😨","hide":"😨","hid":"😨","death":"💀","die":"💀","dust":"💀","sword":"⚔️","angel":"👼",
        "knowledge":"💡","wise":"🧠","wisdom":"💡","god":"✨","lord":"✨","creator":"✨",
        "universe":"🌌","cosmos":"🌌","space":"🌌","stars":"⭐","star":"⭐","world":"🌍","earth":"🌍",
        "exist":"🤔","exists":"🤔","evidence":"🔍","proof":"🔍","real":"✅",
        "moral":"⚖️","good":"😇","evil":"😈","suffering":"😣","pain":"😣","science":"🔬","faith":"🙏","believe":"🤔","love":"❤️","begin":"🌱","began":"🌱","cause":"💥","design":"🎨",
    }
    plan=[]; used_times=[]
    for w in words:
        clean_w=re.sub(r"[^a-z]","",w["text"].lower())
        if clean_w in word_emoji_map:
            start=float(w["start"])
            end=start+3.5
            overlaps=False
            for s,e in used_times:
                if not (end < s or start > e):
                    overlaps=True; break
            if overlaps: continue
            if used_times and start - used_times[-1][1] < 0.8: continue
            emoji_char=word_emoji_map[clean_w]
            if emoji_char not in SAFE_EMOJIS: continue
            if emoji_char in [p["emoji"] for p in plan[-2:]]: continue
            plan.append({"emoji":emoji_char, "start":max(0.0,start), "end":end, "label":clean_w, "word":w["text"]})
            used_times.append((start,end))
            if len(plan)>=MAX_EMOJIS_PER_SEGMENT: break
    return plan

def create_emoji_asset(emoji_char, index):
    filename=f"emoji_{index}.png"
    size=200
    img=Image.new("RGBA",(size,size),(0,0,0,0))
    draw=ImageDraw.Draw(img)
    try:
        font=load_font(140,bold=True)
        box=draw.textbbox((0,0),emoji_char,font=font)
        w=box[2]-box[0]; h=box[3]-box[1]
        draw.text(((size-w)//2,(size-h)//2-10),emoji_char,font=font,fill=(255,255,255,255))
    except:
        draw.ellipse([20,20,size-20,size-20],fill=(255,215,0,220))
    img.save(filename)
    return filename

def create_background(position, glow_color, filename):
    source=os.path.join(os.path.dirname(os.path.abspath(__file__)),"background.png")
    if os.path.exists(source):
        try: image=Image.open(source).convert("RGB").resize((VIDEO_W,VIDEO_H))
        except: image=Image.new("RGB",(VIDEO_W,VIDEO_H),(12,16,32))
    else:
        image=Image.new("RGB",(VIDEO_W,VIDEO_H),(12,16,32))
        draw=ImageDraw.Draw(image)
        for x in range(0,VIDEO_W,60): draw.line([(x,0),(x,VIDEO_H)],fill=(20,26,45),width=2)
        for y in range(0,VIDEO_H,60): draw.line([(0,y),(VIDEO_W,y)],fill=(20,26,45),width=2)
    overlay=Image.new("RGBA",(VIDEO_W,VIDEO_H),(0,0,0,0)); draw=ImageDraw.Draw(overlay)
    cx=400 if position=="left" else 1520 if position=="right" else 960
    for radius in range(700,50,-50):
        alpha=int(15*(1-radius/700))
        draw.ellipse([cx-radius,540-radius,cx+radius,540+radius],fill=hex_to_rgba(glow_color,alpha))
    overlay=overlay.filter(ImageFilter.GaussianBlur(30))
    result=Image.alpha_composite(image.convert("RGBA"),overlay).convert("RGB")
    result.save(filename)

def create_ui_overlay(speaker_name, topic, position, glow_color, filename):
    image=Image.new("RGBA",(VIDEO_W,VIDEO_H),(0,0,0,0)); draw=ImageDraw.Draw(image)
    title_font=load_font(30,bold=True); name_font=load_font(30,bold=True)
    title=f"TOPIC: {topic}"; box=draw.textbbox((0,0),title,font=title_font); width=box[2]-box[0]
    draw.text(((VIDEO_W-width)//2,24),title,fill="white",font=title_font)
    card_width=650; card_height=110; card_y=885
    if position=="left": card_x=75
    elif position=="right": card_x=1195
    else: card_x=(VIDEO_W-card_width)//2
    draw.rounded_rectangle([card_x,card_y,card_x+card_width,card_y+card_height],radius=18,fill=(18,26,46,235),outline=glow_color,width=4)
    draw.ellipse([card_x+22,card_y+27,card_x+47,card_y+52],fill=glow_color)
    draw.text((card_x+65,card_y+22),speaker_name,fill="white",font=name_font)
    image.save(filename)
    return card_x, card_y

def ffmpeg_filter_path(fn):
    p=os.path.abspath(fn); p=p.replace("\\","/").replace("'","\\'").replace(":","\\:")
    return p

def render_video_segment(background=None, ui=None, audio=None, subtitles=None, output=None, position=None, glow_color=None, card_x=None, card_y=None, visual_plan=None, bg_path=None, ui_path=None, audio_path=None, subs_path=None, output_path=None, cx=None, cy=None, **kwargs):
    if background is None and bg_path is not None: background=bg_path
    if ui is None and ui_path is not None: ui=ui_path
    if audio is None and audio_path is not None: audio=audio_path
    if subtitles is None and subs_path is not None: subtitles=subs_path
    if output is None and output_path is not None: output=output_path
    if card_x is None and cx is not None: card_x=cx
    if card_y is None and cy is not None: card_y=cy
    if visual_plan is None: visual_plan=kwargs.get('visual_plan') or kwargs.get('eplan') or []
    if position is None: position=kwargs.get('position','center')
    if glow_color is None: glow_color=kwargs.get('glow',kwargs.get('glow_color','#FFD700'))
    if card_x is None: card_x=kwargs.get('card_x',kwargs.get('cx',960))
    if card_y is None: card_y=kwargs.get('card_y',kwargs.get('cy',900))
    for p in [background,ui,audio,subtitles]:
        if not os.path.exists(p): raise FileNotFoundError(f"Missing {p}")
    emoji_assets=[]
    for idx, v in enumerate(visual_plan or []):
        if "emoji" in v:
            try:
                asset=create_emoji_asset(v["emoji"], idx)
                emoji_assets.append((asset,v))
            except Exception as e: print(f"Emoji skip {e}")
    glow=glow_color.lstrip("#")
    pan_x="0" if position=="left" else "iw-(iw/zoom)" if position=="right" else "(iw-(iw/zoom))/2"
    filter_parts=[]
    filter_parts.append(f"[0:v]scale=1920:1080,zoompan=z='min(zoom+0.00020,1.05)':x='{pan_x}':y='(ih-(ih/zoom))/2':d=9000:s=1920x1080:fps=30[bg];")
    filter_parts.append("[1:v]scale=1920:1080[ui];")
    filter_parts.append(f"[2:a]showwaves=s=300x58:mode=cline:colors=0x{glow}:rate=30[wave];")
    filter_parts.append("[bg][ui]overlay=0:0[base];")
    wave_x=card_x+330; wave_y=card_y+47
    filter_parts.append(f"[base][wave]overlay={wave_x}:{wave_y}[withwave];")
    current="[withwave]"; idx_input=3
    for idx,(asset,vis) in enumerate(emoji_assets):
        label=f"emoji{idx}"
        start=max(0.0,float(vis["start"])); end=start+3.5
        filter_parts.append(f"[{idx_input}:v]format=rgba,fade=t=in:st={start}:d=0.3:alpha=1,fade=t=out:st={end-0.3}:d=0.3:alpha=1[{label}_faded];")
        x_pos=(VIDEO_W-EMOJI_W)//2 + random.randint(-200,200)
        y_pos=VISUAL_Y
        enable=f"between(t,{start:.2f},{end:.2f})"
        filter_parts.append(f"{current}[{label}_faded]overlay={x_pos}:{y_pos}:enable='{enable}'[v{idx}];")
        current=f"[v{idx}]"; idx_input+=1
    sub_path=ffmpeg_filter_path(subtitles)
    filter_parts.append(f"{current}ass='{sub_path}'[outv]")
    filter_complex="".join(filter_parts)
    cmd=["ffmpeg","-y","-loop","1","-framerate",str(FPS),"-i",background,"-i",ui,"-i",audio]
    for asset,_ in emoji_assets:
        cmd+=["-loop","1","-i",asset]
    cmd+=["-filter_complex",filter_complex,"-map","[outv]","-map","2:a","-c:v","libx264","-preset","veryfast","-crf","20","-pix_fmt","yuv420p","-c:a","aac","-b:a","192k","-shortest",output]
    res=subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode!=0:
        print("\nFFmpeg failed:"); print(res.stderr[-7000:])
        raise RuntimeError(f"FFmpeg failed {output}")
    for asset,_ in emoji_assets:
        try: os.remove(asset)
        except: pass

def generate_scoreboard(round_num, results, round_a, round_b, cumulative_a, cumulative_b, filename, roles=None):
    W=VIDEO_W; H=VIDEO_H
    source=os.path.join(os.path.dirname(os.path.abspath(__file__)),"background.png")
    if os.path.exists(source):
        try: base=Image.open(source).convert("RGB").resize((W,H),Image.LANCZOS)
        except: base=Image.new("RGB",(W,H),(12,16,32))
    else: base=Image.new("RGB",(W,H),(12,16,32))
    overlay=Image.new("RGBA",(W,H),(0,0,0,180))
    img=Image.alpha_composite(base.convert("RGBA"),overlay).convert("RGB")
    draw=ImageDraw.Draw(img)
    ft=load_font(48,bold=True); fs=load_font(28,bold=True); fh=load_font(22,bold=True); fr=load_font(24)
    draw.text((W//2,50),f"ROUND {round_num} SCORES",font=ft,fill=(255,215,0,255),anchor="mt")
    if roles:
        rt=f"{roles.get('side_a_label','FOR')} vs {roles.get('side_b_label','AGAINST')}"
        draw.text((W//2,115),rt,font=fs,fill=(255,255,255,230),anchor="mt")
    header_y=190; col_j=120; col_a=750; col_b=1050; col_w=1350
    short_a="APOLOGIST"; short_b="SKEPTIC"
    if roles:
        short_a=roles.get('side_a_label','APOLOGIST').split()[0][:12]
        short_b=roles.get('side_b_label','SKEPTIC').split()[0][:12]
    draw.rectangle([60,header_y-10,W-60,header_y+45],fill=(25,35,70,255),outline=(255,215,0,180),width=2)
    draw.text((col_j,header_y),"Judge",font=fh,fill=(255,255,255,230))
    draw.text((col_a,header_y),short_a,font=fh,fill=(0,255,204,255))
    draw.text((col_b,header_y),short_b,font=fh,fill=(255,120,255,255))
    draw.text((col_w,header_y),"Winner",font=fh,fill=(255,215,0,255))
    y=header_y+65
    for res in results:
        draw.rectangle([60,y-8,W-60,y+42],fill=(20,28,50,255) if (y//58)%2==0 else (15,22,40,255))
        jt=res.get('provider','Judge')
        if len(jt)>32: jt=jt[:30]+".."
        draw.text((col_j,y),jt,font=fr,fill=(255,255,255,240))
        draw.text((col_a,y),f"{res['A_total']:.1f}",font=fr,fill=(0,255,204,255))
        draw.text((col_b,y),f"{res['B_total']:.1f}",font=fr,fill=(255,120,255,255))
        wl=short_a if res['winner']=="A" else short_b
        wc=(0,255,204,255) if res['winner']=="A" else (255,120,255,255)
        draw.text((col_w,y),wl,font=fr,fill=wc)
        y+=58
    draw.line([(60,y+5),(W-60,y+5)],fill=(255,255,255,60),width=2); y+=25
    draw.text((W//2,y),f"Round Avg: {round_a:.1f} vs {round_b:.1f}",font=fs,fill=(255,255,255,255),anchor="mt")
    draw.text((W//2,y+45),f"Cumulative: {cumulative_a:.1f} vs {cumulative_b:.1f}",font=fs,fill=(255,215,0,255),anchor="mt")
    img.save(filename)

def render_scorecard_video(scorecard, audio, subtitles, output):
    for p in [scorecard,audio,subtitles]:
        if not os.path.exists(p): raise FileNotFoundError(f"Missing {p}")
    sub_path=ffmpeg_filter_path(subtitles)
    fc=f"[0:v]scale=1920:1080[base];[base]ass='{sub_path}'[outv]"
    cmd=["ffmpeg","-y","-loop","1","-framerate",str(FPS),"-i",scorecard,"-i",audio,"-filter_complex",fc,"-map","[outv]","-map","1:a","-c:v","libx264","-preset","veryfast","-crf","20","-pix_fmt","yuv420p","-c:a","aac","-b:a","192k","-shortest",output]
    r=subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if r.returncode!=0:
        print(r.stderr[-7000:]); raise RuntimeError("Scorecard render failed.")

def create_segment(text, role, speaker_name, topic, segment_id, model_for_visuals, position=None, glow=None, judge_voice_index=None):
    if position is None:
        if role=="AI Christian Apologist": position="left"
        elif role=="AI Skeptic": position="right"
        else: position="center"
    if glow is None:
        if role=="AI Christian Apologist": glow="#00FFCC"
        elif role=="AI Skeptic": glow="#FF00FF"
        elif role=="AI Judge": glow="#3399FF"
        else: glow="#FFD700"
    af=f"audio_{segment_id}.mp3"; sf=f"subs_{segment_id}.ass"; bf=f"bg_{segment_id}.png"; uf=f"ui_{segment_id}.png"; vf=f"segment_{segment_id}.mp4"
    words=generate_audio(text, role, af, judge_voice_index)
    try: generate_subtitles(words, sf, scorecard=False, audio_file=af, full_text=text)
    except TypeError: generate_subtitles(words, sf)
    eplan=[]
    try:
        eplan=create_emoji_plan(clean_for_speech(text), words)
        if eplan: print(f"   {len(eplan)} emoji(s) 3.5s each: {', '.join(v['emoji']+'('+v['word']+')' for v in eplan)}")
    except Exception as e: print(f"Emoji planning skipped: {e}")
    create_background(position, glow, bf)
    cx,cy=create_ui_overlay(speaker_name, topic, position, glow, uf)
    render_video_segment(background=bf, ui=uf, audio=af, subtitles=sf, output=vf, position=position, glow_color=glow, card_x=cx, card_y=cy, visual_plan=eplan)
    return vf

def generate_panel_commentary(model, side, topic, rn, ap, sk, prev):
    prov=provider_from_model(model)
    pref="the case for" if side=="A" else "the case against"
    def trim(t,mw=180):
        t=t.strip()
        if len(t.split())<=mw: return t
        s=t.split('. ')
        if len(s)>=2: return s[0][:140]+" ... "+s[-1][:140]
        return " ".join(t.split()[:mw])
    def core(txt):
        low=txt.lower()
        if "evil" in low or "suffer" in low: return "suffering and whether it makes sense"
        if "hidden" in low: return "why God seems hidden"
        if "fine" in low or "tuned" in low: return "fine-tuning"
        if "cause" in low or "began" in low: return "whether universe needs a cause"
        if "moral" in low: return "moral values"
        return "what matters most in this round"
    apc=core(ap); skc=core(sk)
    prompt=f"You are {prov}, judge for round {rn} on: {topic} FOR: {trim(ap)} AGAINST: {trim(sk)} You leaned {pref}. Talk in plain natural conversational tone, like telling a friend what happened. In one sentence say what they were really disagreeing about this round ({apc} vs {skc}). In one sentence say why {pref} handled that better - did it answer the other side point directly before making its own? 2 sentences total, natural, no formal phrases, no listing. Previous: {' '.join(prev[-2:])}"
    for attempt in range(2):
        resp=query_openrouter(prompt, model, timeout=30, max_tokens=200, temperature=0.85 if attempt==0 else 0.9)
        if resp and count_words(resp)>=12: return resp
    return f"Round {rn} really came down to {apc} versus {skc}. For me, {pref} edged it because it actually dealt with what the other person said before jumping to its own point."

def build_intro(topic, jc):
    return f"Welcome to the AI Debate Arena. Today an AI Christian Apologist and an AI Skeptic are going to talk through the question: {topic}. We'll have three rounds, plenty of time for each side to really respond to each other, not just make speeches. We've got {jc} independent AI judges scoring as we go. Let's get into it."

def build_outro(jc, ca, cb):
    if math.isclose(ca,cb,abs_tol=0.01): res="a draw"
    elif ca>cb: res="the Christian Apologist"
    else: res="the Skeptic"
    return f"Alright, after three rounds our {jc} judges have the Apologist at {ca:.1f} and the Skeptic at {cb:.1f}, so overall it leans toward {res}. But that's just the panel. What do you think actually held up when you listened to the back and forth?"

def stitch_segments(segs, out):
    lf="concat_list.txt"
    with open(lf,"w",encoding="utf-8") as f:
        for s in segs:
            p=os.path.abspath(s); p=p.replace("'","'\\''")
            f.write(f"file '{p}'\n")
    print("Stitching final video...")
    cmd=["ffmpeg","-y","-f","concat","-safe","0","-i",lf,"-c","copy",out]
    r=subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if r.returncode!=0: print(r.stderr[-7000:]); raise RuntimeError("Concat failed.")

def run_debate_pipeline():
    cleanup_cache()
    if not OPENROUTER_API_KEY: raise RuntimeError("OPENROUTER_API_KEY missing")
    if not os.path.exists("topic.txt"): open("topic.txt","w",encoding="utf-8").write("Does God exist?")
    topic=open("topic.txt","r",encoding="utf-8").read().strip() or "Does God exist?"
    print("\n"+"="*70+"\nAI DEBATE ARENA\n"+"="*70+f"\n\nTOPIC: {topic}\n")
    print(f"FREE_MODE={FREE_MODE}, ROUNDS={ROUNDS}, TURNS_PER_SIDE={TURNS_PER_SIDE_PER_ROUND}, WORDS_PER_TURN={WORDS_PER_TURN} => target ~10+ mins")
    avail=discover_models()
    if not avail:
        print("Using fallback models"); avail=FALLBACK_MODELS.copy()
    ap_model, sk_model = choose_primary_models(avail)
    print(f"Debate engines: {provider_from_model(ap_model)} vs {provider_from_model(sk_model)}")
    judges=choose_judges(avail,(ap_model,sk_model))
    if not judges: judges=FALLBACK_MODELS[:MAX_JUDGES]
    print(f"Judges: {len(judges)}")
    for m in judges: print(f"   {provider_from_model(m)} — {m.split('/',1)[-1][:30]}")
    segs=[]; sid=0
    def add_seg(text,role,name,pos=None,glow=None,jidx=None):
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
        print(f"   Round total: A={count_words(ap_full)} words | B={count_words(sk_full)} words")
        res=evaluate_round(judges, topic, rn, ap_full, sk_full)
        ra, rb = calculate_round_average(res)
        cum_a+=ra; cum_b+=rb
        print(f"Round {rn}: A {ra:.1f} vs B {rb:.1f} | Cum {cum_a:.1f} vs {cum_b:.1f}")
        sb=f"scoreboard_r{rn}.png"
        generate_scoreboard(rn, res, ra, rb, cum_a, cum_b, sb, roles)
        stxt=f"Round {rn} is complete. The {len(res)} judges gave the Apologist {ra:.1f} and the Skeptic {rb:.1f}. Cumulative is {cum_a:.1f} to {cum_b:.1f}."
        sa=f"score_audio_r{rn}.mp3"; ss=f"score_subs_r{rn}.ass"; sv=f"score_video_r{rn}.mp4"
        sw=generate_audio(stxt,"Moderator",sa)
        generate_subtitles(sw, ss, scorecard=True, audio_file=sa, full_text=stxt)
        render_scorecard_video(sb, sa, ss, sv)
        segs.append(sv)
        if res:
            if PANEL_COMMENTS_PER_ROUND==1:
                wj=random.choice(res)
                com=generate_panel_commentary(wj["model"], wj["winner"], topic, rn, ap_full, sk_full, panel_comments)
                panel_comments.append(com)
                add_seg(com,"AI Judge","AI JUDGE — "+wj["provider"].upper(),"center","#3399FF", judge_voice_index=0)
            else:
                a_res=[r for r in res if r["winner"]=="A"]; b_res=[r for r in res if r["winner"]=="B"]
                if not a_res: a_res=res
                if not b_res: b_res=res
                ja=random.choice(a_res); jb=random.choice(b_res)
                ca=generate_panel_commentary(ja["model"],"A",topic,rn,ap_full,sk_full,panel_comments); panel_comments.append(ca)
                add_seg(ca,"AI Judge","AI JUDGE — "+ja["provider"].upper(),"center","#3399FF", judge_voice_index=0)
                cb=generate_panel_commentary(jb["model"],"B",topic,rn,ap_full,sk_full,panel_comments); panel_comments.append(cb)
                add_seg(cb,"AI Judge","AI JUDGE — "+jb["provider"].upper(),"center","#3399FF", judge_voice_index=1)
    add_seg(build_outro(len(judges),cum_a,cum_b),"Moderator","MODERATOR")
    stitch_segments(segs, OUTPUT_FILE)
    print("\n"+"="*70+"\nDEBATE COMPLETE\n"+"="*70)
    print(f"Output: {OUTPUT_FILE}")
    print(f"Final: Apologist {cum_a:.1f} vs Skeptic {cum_b:.1f}")
    cleanup_cache()

if __name__=="__main__":
    try: run_debate_pipeline()
    except KeyboardInterrupt: print("\nCancelled.")
    except Exception as exc:
        print("\nPIPELINE FAILED"); print(str(exc)); raise
