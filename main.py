
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
from typing import List, Dict, Optional
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
WORDS_PER_SIDE_PER_ROUND = 500
TURNS_PER_SIDE_PER_ROUND = 4
WORDS_PER_TURN = 125
MIN_TURN_WORDS = 105
MAX_TURN_WORDS = 145
MAX_JUDGES = 7
JUDGE_WORKERS = 7
MAX_VISUALS_PER_SEGMENT = 2
MIN_VISUAL_GAP = 2.0
VISUAL_W = 480
VISUAL_H = 480
VISUAL_X = (VIDEO_W - VISUAL_W) // 2
VISUAL_Y = 180

VOICES = {
    "Moderator": "en-US-AndrewMultilingualNeural",
    "AI Christian Apologist": "en-US-BrianMultilingualNeural",
    "AI Skeptic": "en-US-AvaMultilingualNeural",
}
JUDGE_VOICES = ["en-US-ChristopherNeural","en-US-EmmaMultilingualNeural","en-US-GuyNeural","en-US-JennyNeural"]

FALLBACK_MODELS = [
    "openai/gpt-4o-mini",
    "google/gemini-2.0-flash-001",
    "anthropic/claude-3.5-haiku",
    "mistralai/mistral-small",
    "meta-llama/llama-3.1-70b-instruct",
    "qwen/qwen-2.5-72b-instruct",
    "deepseek/deepseek-chat",
]

PROVIDER_ALIASES = {
    "openai":"OpenAI","anthropic":"Anthropic","google":"Google","x-ai":"xAI","xai":"xAI","deepseek":"DeepSeek",
    "mistralai":"Mistral","mistral":"Mistral","meta-llama":"Meta","meta":"Meta","qwen":"Alibaba / Qwen","cohere":"Cohere",
    "perplexity":"Perplexity",
}

def provider_from_model(model_id):
    if not model_id: return "Unknown"
    prefix = model_id.split("/",1)[0].lower().strip()
    return PROVIDER_ALIASES.get(prefix, prefix.replace("-"," ").title())

def get_judge_short_name(model_id):
    low=(model_id or "").lower()
    if "gpt" in low: return "Chat GPT"
    if "claude" in low: return "Claude"
    if "gemini" in low: return "Gemini"
    if "grok" in low: return "Grok"
    if "deepseek" in low: return "DeepSeek"
    if "mistral" in low or "mixtral" in low: return "Mistral"
    if "llama" in low: return "Llama"
    if "qwen" in low: return "Qwen"
    if "command" in low: return "Cohere"
    if "nemotron" in low: return "Nemotron"
    if "kimi" in low or "moonshot" in low: return "Kimi"
    if "perplexity" in low or "sonar" in low: return "Perplexity"
    return provider_from_model(model_id).split()[0]

def cleanup_cache():
    patterns=["*.mp4","*.mp3","*.ass","*.png","*.gif","*_list.txt"]
    protected={OUTPUT_FILE,"background.png","topic.txt"}
    for pattern in patterns:
        for filename in glob.glob(pattern):
            if filename in protected: continue
            try: os.remove(filename)
            except: pass

def count_words(text): return len(re.findall(r"\b[\w'-]+\b", text or ""))
def clean_for_speech(text):
    text=re.sub(r"\([^)]*\)","",text or "")
    for old,new in {"*":"","#":"","_":"","`":"","–":"-","—":"-","\"":"",":":" ",";":" ","&":"and"}.items():
        text=text.replace(old,new)
    return re.sub(r"\s+"," ",text).strip()
def clamp_score(v):
    try: v=float(v)
    except: v=50.0
    return max(0.0,min(100.0,v))
def load_font(size,bold=False):
    paths=[
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:\\Windows\\Fonts\\arialbd.ttf" if bold else "C:\\Windows\\Fonts\\arial.ttf",
    ]
    for p in paths:
        try: return ImageFont.truetype(p,size)
        except: continue
    return ImageFont.load_default()
def hex_to_rgba(h,a):
    h=h.lstrip("#")
    return (int(h[0:2],16),int(h[2:4],16),int(h[4:6],16),a)

def openrouter_headers():
    return {"Authorization":f"Bearer {OPENROUTER_API_KEY}","Content-Type":"application/json","HTTP-Referer":"https://openrouter.ai/","X-Title":"AI Debate Arena"}

def discover_models():
    if not OPENROUTER_API_KEY: raise RuntimeError("OPENROUTER_API_KEY is missing.")
    try:
        r=requests.get(OPENROUTER_MODELS_URL,headers=openrouter_headers(),timeout=20)
        models=[]
        for item in r.json().get("data",[]):
            mid=item.get("id")
            if not mid: continue
            if any(x in mid.lower() for x in ["embed","tts","whisper","audio","moderation","guard"]): continue
            models.append(mid)
        return list(dict.fromkeys(models))
    except Exception as exc:
        print(f"Model discovery failed: {exc}")
        return []

def query_openrouter(prompt,model_id,timeout=60,max_tokens=1200,temperature=0.7):
    if not OPENROUTER_API_KEY: return None
    payload={"model":model_id,"messages":[{"role":"user","content":prompt}],"temperature":temperature,"max_tokens":max_tokens}
    for attempt in range(3):
        try:
            resp=requests.post(OPENROUTER_URL,headers=openrouter_headers(),json=payload,timeout=timeout)
            if resp.status_code==200:
                c=resp.json().get("choices",[])[0].get("message",{}).get("content","")
                if c and len(c.strip())>10: return c.strip()
        except Exception as exc:
            print(f"Request failed {get_judge_short_name(model_id)}: {exc}")
        if attempt<2: time.sleep(1.5*(attempt+1))
    return None

def choose_primary_models(available_models):
    pref=["openai/gpt-4o","openai/gpt-4o-mini","anthropic/claude-3.5-sonnet","anthropic/claude-3.5-haiku","google/gemini-2.5-flash","google/gemini-2.0-flash-001","deepseek/deepseek-chat","qwen/qwen-2.5-72b-instruct"]
    found=[m for m in pref if m in set(available_models)]
    if len(found)>=2: return found[0],found[1]
    if len(found)==1:
        rem=[m for m in available_models if m!=found[0]]
        if rem: return found[0],rem[0]
    if len(available_models)>=2: return available_models[0],available_models[1]
    return FALLBACK_MODELS[0],FALLBACK_MODELS[1]

def choose_judges(available_models,primary_models):
    excl=set(primary_models)
    cands=[m for m in available_models if m not in excl and "image" not in m.lower()]
    groups={}
    for m in cands:
        prov=provider_from_model(m)
        groups.setdefault(prov,[]).append(m)
    sel=[]
    for prov,models in groups.items():
        models.sort(key=lambda mm:(0 if any(k in mm.lower() for k in ["gpt","claude","gemini","grok","deepseek","mistral","llama","qwen"]) else 1,len(mm)))
        sel.append((prov,models[0]))
    priority=["OpenAI","Anthropic","Google","xAI","DeepSeek","Mistral","Meta","Alibaba / Qwen","Cohere","Perplexity"]
    sel.sort(key=lambda x:(priority.index(x[0]) if x[0] in priority else 999,x[0]))
    return [m for _,m in sel[:MAX_JUDGES]]

def generate_turn(side,topic,round_num,turn_num,previous_exchange,model):
    side_name="AI Christian Apologist" if side=="A" else "AI Skeptic"
    opponent="AI Skeptic" if side=="A" else "AI Christian Apologist"
    instr="Opening - establish foundation." if round_num==1 and turn_num==1 else f"Turn {turn_num} round {round_num} - respond directly."
    prompt=f"You are {side_name} debating {opponent} on {topic}. {instr}\nPrevious:\n{previous_exchange or 'None'}\nWrite ONLY spoken contribution, {WORDS_PER_TURN} words, {MIN_TURN_WORDS}-{MAX_TURN_WORDS}."
    resp=query_openrouter(prompt,model,max_tokens=430,temperature=0.78)
    return resp if resp else "We need to examine whether evidence supports this conclusion."

def build_round_exchanges(topic,round_num,ap_model,sk_model,prev_hist):
    a_turns=[]; s_turns=[]; hist=prev_hist
    for tn in range(1,TURNS_PER_SIDE_PER_ROUND+1):
        a=generate_turn("A",topic,round_num,tn,hist,ap_model); a_turns.append(a); hist=f"Apologist:\n{a}\n\n"
        s=generate_turn("B",topic,round_num,tn,hist,sk_model); s_turns.append(s); hist+=f"Skeptic:\n{s}\n\n"
    return a_turns,s_turns,hist

def neutral_judge(model):
    return {"model":model,"provider":provider_from_model(model),"display_name":get_judge_short_name(model),
            "A_argument":50,"A_rebuttal":50,"A_clarity":50,"A_total":50,"B_argument":50,"B_rebuttal":50,"B_clarity":50,"B_total":50,"winner":"A"}

def judge_round(model,topic,round_num,apologist,skeptic):
    prompt=f"Judge debate Topic:{topic} Round:{round_num} A:{apologist} B:{skeptic} Return JSON {{'A_argument':0,'A_rebuttal':0,'A_clarity':0,'B_argument':0,'B_rebuttal':0,'B_clarity':0}}"
    resp=query_openrouter(prompt,model,timeout=35,max_tokens=250,temperature=0.1)
    if not resp: return neutral_judge(model)
    try:
        m=re.search(r"\{.*\}",resp,re.DOTALL)
        if not m: return neutral_judge(model)
        d=json.loads(m.group(0))
        aa,ar,ac=clamp_score(d.get("A_argument",50)),clamp_score(d.get("A_rebuttal",50)),clamp_score(d.get("A_clarity",50))
        ba,br,bc=clamp_score(d.get("B_argument",50)),clamp_score(d.get("B_rebuttal",50)),clamp_score(d.get("B_clarity",50))
        at=(aa+ar+ac)/3; bt=(ba+br+bc)/3
        return {"model":model,"provider":provider_from_model(model),"display_name":get_judge_short_name(model),
                "A_argument":aa,"A_rebuttal":ar,"A_clarity":ac,"A_total":round(at,2),
                "B_argument":ba,"B_rebuttal":br,"B_clarity":bc,"B_total":round(bt,2),"winner":"A" if at>bt else "B"}
    except: return neutral_judge(model)

def evaluate_round(judges,topic,round_num,apologist,skeptic):
    results=[]
    print(f"Asking {len(judges)} judges ({', '.join(get_judge_short_name(j) for j in judges)})...")
    def worker(m): return judge_round(m,topic,round_num,apologist,skeptic)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1,min(JUDGE_WORKERS,len(judges)))) as ex:
        futs={ex.submit(worker,m):m for m in judges}
        for fu in concurrent.futures.as_completed(futs):
            try:
                res=fu.result(); results.append(res)
                print(f"  Judge — {res['display_name']}")
            except Exception as e:
                print(f"Judge failed {get_judge_short_name(futs[fu])}: {e}")
    if not results: results=[neutral_judge("fallback")]
    return results

def calculate_round_average(results):
    a=sum(r["A_total"] for r in results)/len(results)
    b=sum(r["B_total"] for r in results)/len(results)
    return round(a,2),round(b,2)

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
    if role=="AI Judge":
        voice=JUDGE_VOICES[(judge_voice_index or 0)%len(JUDGE_VOICES)]
    else:
        voice=VOICES.get(role,VOICES["Moderator"])
    clean=clean_for_speech(text)
    try:
        return asyncio.run(generate_audio_async(clean,voice,filename))
    except Exception as e:
        print(f"TTS failed {voice}: {e}")
        return asyncio.run(generate_audio_async(clean,VOICES["Moderator"],filename))

def format_ass_time(s):
    s=max(0.0,float(s)); h=int(s//3600); m=int((s%3600)//60); sec=s%60
    return f"{h}:{m:02d}:{sec:05.2f}"
def ass_escape(t):
    return str(t).replace("\\",r"\\").replace("{",r"\{").replace("}",r"\}").replace("\n"," ")

def generate_subtitles(words,filename,scorecard=False):
    margin_v=90 if scorecard else 210
    header=f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: DebateSub,DejaVu Sans,40,&H00FFFFFF,&H00FFFFFF,&H00000000,&HCC000000,1,0,0,0,100,100,0,0,1,3.5,1,2,320,320,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    if not words:
        with open(filename,"w",encoding="utf-8") as f: f.write(header); return
    chunks=[]; cur=[]
    for w in words:
        cur.append(w)
        ends=str(w["text"]).strip().endswith(('.', '?', '!'))
        if (ends and len(cur)>=8) or len(cur)>=14:
            chunks.append(cur); cur=[]
    if cur: chunks.append(cur)
    events=[]
    for chunk in chunks:
        if not chunk: continue
        s=float(chunk[0]["start"]); e=float(chunk[-1]["end"])+0.15
        txt_words=[ass_escape(w["text"]) for w in chunk]
        if len(txt_words)>9:
            mid=len(txt_words)//2
            txt=" ".join(txt_words[:mid])+r"\N"+" ".join(txt_words[mid:])
        else:
            txt=" ".join(txt_words)
        ass_text=f"{{\\an2\\pos(960,820)\\q2\\fad(120,180)}}{txt}"
        events.append(f"Dialogue: 0,{format_ass_time(s)},{format_ass_time(e)},DebateSub,,0,0,0,,{ass_text}")
    with open(filename,"w",encoding="utf-8") as f:
        f.write(header+"\n".join(events)+"\n")

def plan_visuals(text,model):
    prompt=f"""You are visual director. Read: {text}\nFind up to {MAX_VISUALS_PER_SEGMENT} concrete visual moments like Adam eating apple, Garden of Eden. Return ONLY JSON: [{{"phrase":"exact phrase","label":"2-4 words","description":"detailed prompt like Adam and Eve in Garden of Eden Adam biting red apple cinematic","kind":"person"}}]"""
    resp=query_openrouter(prompt,model,timeout=35,max_tokens=500,temperature=0.2)
    if not resp: return []
    try:
        m=re.search(r"\[.*\]",resp,re.DOTALL)
        if not m: return []
        data=json.loads(m.group(0))
        out=[]
        for item in data:
            if not isinstance(item,dict): continue
            phrase=str(item.get("phrase","")).strip(); label=str(item.get("label","")).strip(); desc=str(item.get("description","")).strip()
            if not phrase or not label or len(desc)<8: continue
            if phrase.lower() not in text.lower(): continue
            out.append({"phrase":phrase,"label":label[:30],"description":desc[:220],"kind":item.get("kind","concept")})
            if len(out)>=MAX_VISUALS_PER_SEGMENT: break
        return out
    except: return []

def find_phrase_timing(phrase,words):
    if not phrase or not words: return None
    pw=re.findall(r"\b[\w'-]+\b",phrase.lower())
    sw=[re.sub(r"[^\w'-]","",str(w["text"]).lower()) for w in words]
    pw=[x for x in pw if x]
    if not pw: return None
    for i in range(len(sw)-len(pw)+1):
        if sw[i:i+len(pw)]==pw:
            s=float(words[i]["start"]); e=float(words[min(len(words)-1,i+len(pw)-1)]["end"])+2.5
            return {"start":max(0.0,s-0.15),"end":max(s+2.5,e)}
    for p in pw:
        if len(p)<4: continue
        for idx,s in enumerate(sw):
            if p==s:
                return {"start":float(words[idx]["start"]),"end":float(words[min(len(words)-1,idx+12)]["end"])+1.5}
    return None

def fallback_visual_timing(idx,total,words):
    if not words: return None
    last=float(words[-1]["end"]); us=0.15*last; ue=0.85*last
    s=us if total<=1 else us + (ue-us)*idx/max(1,total-1)
    return {"start":max(0.0,s),"end":s+3.0}

def create_visual_plan(text,words,model):
    if not words: return []
    cands=plan_visuals(text,model)
    if not cands: return []
    timed=[]
    for idx,item in enumerate(cands):
        t=find_phrase_timing(item["phrase"],words) or fallback_visual_timing(idx,len(cands),words)
        if not t: continue
        it=dict(item); it.update(t); timed.append(it)
    timed.sort(key=lambda x:x["start"])
    out=[]
    for it in timed:
        if any(abs(it["start"]-p["start"])<MIN_VISUAL_GAP for p in out): continue
        out.append(it)
        if len(out)>=MAX_VISUALS_PER_SEGMENT: break
    return out

def build_visual_prompt(v):
    return f"{v.get('description','')}, {v.get('label','')}, ultra detailed, cinematic, 8k, photorealistic, no text, no words, no watermark, no border"

def fetch_topic_image(v):
    try:
        prompt=build_visual_prompt(v)
        enc=quote(prompt)
        url=f"https://image.pollinations.ai/prompt/{enc}?width=768&height=768&model=flux&enhance=true&nologo=true&seed={random.randint(0,999999)}"
        print(f" Image {v.get('label')} -> {prompt[:70]}...")
        r=requests.get(url,timeout=35)
        if r.status_code==200 and len(r.content)>15000:
            return Image.open(BytesIO(r.content)).convert("RGB")
    except Exception as e:
        print(f" Image fetch failed: {e}")
    return None

def create_visual_asset(visual,index):
    filename=f"visual_{index}.gif"
    real=fetch_topic_image(visual)
    if real is None:
        print(f"   No image for {visual.get('label')} - skipping visual")
        return None
    frames=[]
    for f in range(30):
        prog=math.sin(math.pi*f/30); bob=int(4*math.sin(2*math.pi*f/30))
        scale=1.0+0.12*prog; target=int(VISUAL_W*scale)
        sc=real.resize((target,target),Image.LANCZOS)
        left=(target-VISUAL_W)//2; top=(target-VISUAL_H)//2 + bob
        crop=sc.crop((left,max(0,top),left+VISUAL_W,max(0,top)+VISUAL_H))
        final=Image.new("RGBA",(VISUAL_W,VISUAL_H),(0,0,0,0))
        mask=Image.new("L",(VISUAL_W,VISUAL_H),0)
        ImageDraw.Draw(mask).rounded_rectangle((0,0,VISUAL_W,VISUAL_H),radius=32,fill=255)
        final.paste(crop,(0,0),mask)
        frames.append(final)
    frames[0].save(filename,format='GIF',save_all=True,append_images=frames[1:],duration=36,loop=0,disposal=2)
    return filename

def create_background(position,glow_color,filename):
    source=os.path.join(os.path.dirname(os.path.abspath(__file__)),"background.png")
    if os.path.exists(source):
        try: image=Image.open(source).convert("RGB").resize((VIDEO_W,VIDEO_H))
        except: image=Image.new("RGB",(VIDEO_W,VIDEO_H),(12,16,32))
    else:
        image=Image.new("RGB",(VIDEO_W,VIDEO_H),(12,16,32))
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
    title_font=load_font(30,bold=True); name_font=load_font(30,bold=True)
    title=f"TOPIC: {topic}"
    box=draw.textbbox((0,0),title,font=title_font)
    draw.text(((VIDEO_W-(box[2]-box[0]))//2,24),title,fill="white",font=title_font)
    cw=650; ch=110; cy=885
    cx=75 if position=="left" else 1195 if position=="right" else (VIDEO_W-cw)//2
    draw.rounded_rectangle([cx,cy,cx+cw,cy+ch],radius=18,fill=(18,26,46,235),outline=glow_color,width=4)
    draw.ellipse([cx+22,cy+27,cx+47,cy+52],fill=glow_color)
    draw.text((cx+65,cy+22),speaker_name,fill="white",font=name_font)
    image.save(filename)
    return cx,cy

def ffmpeg_filter_path(fn):
    return os.path.abspath(fn).replace("\\","/").replace("'","\\'").replace(":","\\:")

def render_video_segment(background,ui,audio,subtitles,output,position,glow_color,card_x,card_y,visual_plan):
    for p in [background,ui,audio,subtitles]:
        if not os.path.exists(p): raise FileNotFoundError(p)
    vassets=[]
    for idx,vis in enumerate(visual_plan or []):
        try:
            asset=create_visual_asset(vis,idx)
            if asset is None: continue
            vassets.append((asset,vis))
        except Exception as e:
            print(f"Visual skip: {e}")
    glow=glow_color.lstrip("#")
    pan_x="0" if position=="left" else "iw-(iw/zoom)" if position=="right" else "(iw-(iw/zoom))/2"
    parts=[]
    parts.append(f"[0:v]scale=1920:1080,zoompan=z='min(zoom+0.00020,1.05)':x='{pan_x}':y='(ih-(ih/zoom))/2':d=9000:s=1920x1080:fps=30[bg];")
    parts.append("[1:v]scale=1920:1080[ui];")
    parts.append(f"[2:a]showwaves=s=300x58:mode=cline:colors=0x{glow}:rate=30[wave];")
    parts.append("[bg][ui]overlay=0:0[base];")
    parts.append(f"[base][wave]overlay={card_x+330}:{card_y+47}[withwave];")
    cur="[withwave]"; idx_in=3
    for i,(asset,vis) in enumerate(vassets):
        s=max(0.0,float(vis["start"])); e=max(s+2.0,float(vis["end"]))
        parts.append(f"[{idx_in}:v]format=rgba,fade=t=in:st={s}:d=0.4:alpha=1,fade=t=out:st={e-0.4}:d=0.4:alpha=1[vf{i}];")
        x=(VIDEO_W-VISUAL_W)//2; y_expr=f"{VISUAL_Y} - (t-{s})*12"; en=f"between(t,{s:.2f},{e:.2f})"
        parts.append(f"{cur}[vf{i}]overlay={x}:'{y_expr}':enable='{en}'[v{i}];")
        cur=f"[v{i}]"; idx_in+=1
    parts.append(f"{cur}ass='{ffmpeg_filter_path(subtitles)}'[outv]")
    fc="".join(parts)
    cmd=["ffmpeg","-y","-loop","1","-framerate",str(FPS),"-i",background,"-i",ui,"-i",audio]
    for a,_ in vassets: cmd+=["-ignore_loop","0","-i",a]
    cmd+=["-filter_complex",fc,"-map","[outv]","-map","2:a","-c:v","libx264","-preset","veryfast","-crf","20","-pix_fmt","yuv420p","-c:a","aac","-b:a","192k","-shortest",output]
    res=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    if res.returncode!=0:
        print(res.stderr[-7000:]); raise RuntimeError(f"FFmpeg failed {output}")
    for a,_ in vassets:
        try: os.remove(a)
        except: pass

def generate_scoreboard(round_num,results,round_a,round_b,cumulative_a,cumulative_b,filename):
    src=os.path.join(os.path.dirname(os.path.abspath(__file__)),"background.png")
    if os.path.exists(src):
        try: img=Image.open(src).convert("RGB").resize((VIDEO_W,VIDEO_H))
        except: img=Image.new("RGB",(VIDEO_W,VIDEO_H),(12,16,32))
    else: img=Image.new("RGB",(VIDEO_W,VIDEO_H),(12,16,32))
    over=Image.new("RGBA",(VIDEO_W,VIDEO_H),(0,0,0,235))
    img=Image.alpha_composite(img.convert("RGBA"),over).convert("RGB")
    d=ImageDraw.Draw(img)
    hdr=load_font(38,bold=True); sub=load_font(22,bold=True); sml=load_font(20)
    def centred(y,txt,fnt,col):
        box=d.textbbox((0,0),txt,font=fnt); w=box[2]-box[0]; d.text(((VIDEO_W-w)//2,y),txt,fill=col,font=fnt)
    centred(24,f"ROUND {round_num} — AI JUDGING PANEL",hdr,"#FFD700")
    centred(72,f"{len(results)} JUDGES — {', '.join(r['display_name'] for r in results)}",sub,"white")
    centred(112,f"ROUND SCORE   APOLOGIST {round_a:.1f}   VS   SKEPTIC {round_b:.1f}",sub,"white")
    centred(150,f"CUMULATIVE   APOLOGIST {cumulative_a:.1f}   VS   SKEPTIC {cumulative_b:.1f}",sub,"#FFD700")
    d.text((100,225),"CATEGORY AVERAGES",fill="#FFD700",font=sub)
    d.text((500,265),"APOLOGIST",fill="#00FFCC",font=sml); d.text((680,265),"SKEPTIC",fill="#FF66FF",font=sml)
    y=310
    for label,ak,bk in [("Argument strength","A_argument","B_argument"),("Rebuttal quality","A_rebuttal","B_rebuttal"),("Clarity & reasoning","A_clarity","B_clarity")]:
        a=sum(r[ak] for r in results)/len(results); b=sum(r[bk] for r in results)/len(results)
        d.text((100,y),label,fill="white",font=sml); d.text((500,y),f"{a:.1f}",fill="#00FFCC",font=sml); d.text((680,y),f"{b:.1f}",fill="#FF66FF",font=sml); y+=48
    d.text((980,225),"INDIVIDUAL JUDGES",fill="#FFD700",font=sub)
    d.text((980,270),"MODEL",fill="white",font=sml); d.text((1500,270),"A",fill="#00FFCC",font=sml); d.text((1580,270),"B",fill="#FF66FF",font=sml)
    d.line([(970,300),(1680,300)],fill=(100,110,140,255),width=2)
    sy=320
    for r in results:
        d.text((980,sy),r.get("display_name","?"),fill="white",font=sml)
        d.text((1500,sy),f"{r['A_total']:.1f}",fill="#00FFCC",font=sml)
        d.text((1580,sy),f"{r['B_total']:.1f}",fill="#FF66FF",font=sml); sy+=48
    img.save(filename)

def render_scorecard_video(scorecard,audio,subtitles,output):
    for p in [scorecard,audio,subtitles]:
        if not os.path.exists(p): raise FileNotFoundError(p)
    fc=f"[0:v]scale=1920:1080[base];[base]ass='{ffmpeg_filter_path(subtitles)}'[outv]"
    cmd=["ffmpeg","-y","-loop","1","-framerate",str(FPS),"-i",scorecard,"-i",audio,"-filter_complex",fc,"-map","[outv]","-map","1:a","-c:v","libx264","-preset","veryfast","-crf","20","-pix_fmt","yuv420p","-c:a","aac","-b:a","192k","-shortest",output]
    res=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    if res.returncode!=0:
        print(res.stderr[-7000:]); raise RuntimeError("Scorecard failed")

def create_segment(text,role,speaker_name,topic,segment_id,model_for_visuals,position=None,glow=None,judge_voice_index=None):
    if position is None:
        position="left" if role=="AI Christian Apologist" else "right" if role=="AI Skeptic" else "center"
    if glow is None:
        glow="#00FFCC" if role=="AI Christian Apologist" else "#FF00FF" if role=="AI Skeptic" else "#3399FF" if role=="AI Judge" else "#FFD700"
    af=f"audio_{segment_id}.mp3"; sf=f"subs_{segment_id}.ass"; bf=f"bg_{segment_id}.png"; uf=f"ui_{segment_id}.png"; vf=f"segment_{segment_id}.mp4"
    words=generate_audio(text,role,af,judge_voice_index)
    generate_subtitles(words,sf)
    vplan=[]
    try:
        vplan=create_visual_plan(clean_for_speech(text),words,model_for_visuals)
        if vplan: print(f"   {len(vplan)} visual(s): {', '.join(v['label'] for v in vplan)}")
    except Exception as e:
        print(f"Visual planning skipped: {e}")
    create_background(position,glow,bf)
    cx,cy=create_ui_overlay(speaker_name,topic,position,glow,uf)
    render_video_segment(bf,uf,af,sf,vf,position,glow,cx,cy,vplan)
    return vf

def generate_panel_commentary(model,side,topic,round_num,apologist,skeptic,previous_comments):
    prov=get_judge_short_name(model)
    pref="AI Christian Apologist" if side=="A" else "AI Skeptic"
    recent="\n".join(previous_comments[-6:])
    def trim(t,mw=220):
        wl=t.split(); return t if len(wl)<=mw else " ".join(wl[-mw:])
    prompt=f"You are {prov} judge. Topic:{topic} Round:{round_num} Apologist:{trim(apologist)} Skeptic:{trim(skeptic)} You preferred:{pref} Give short specific observation.\nPrevious:{recent}\n2-3 sentences."
    resp=query_openrouter(prompt,model,timeout=40,max_tokens=220,temperature=0.85)
    return resp if resp else "The key is whether argument answered strongest objection."

def build_intro(topic,jc):
    return f"Welcome to the AI Debate Arena. Today, an AI Christian Apologist faces an AI Skeptic on {topic}. Three rounds, equal time. Panel of {jc} AIs will score argument, rebuttal, clarity. Let's begin."

def build_outro(jc,ca,cb):
    if math.isclose(ca,cb,abs_tol=0.01): res="a draw"
    elif ca>cb: res="the AI Christian Apologist"
    else: res="the AI Skeptic"
    return f"After three rounds, panel of {jc} judges gave Apologist {ca:.1f}, Skeptic {cb:.1f}. Final result is {res}. But final verdict is yours."

def stitch_segments(segs,out):
    lf="concat_list.txt"
    with open(lf,"w",encoding="utf-8") as f:
        for s in segs:
            p=os.path.abspath(s).replace("'","'\\''"); f.write(f"file '{p}'\n")
    print("Stitching final video...")
    cmd=["ffmpeg","-y","-f","concat","-safe","0","-i",lf,"-c","copy",out]
    r=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    if r.returncode!=0:
        print(r.stderr[-7000:]); raise RuntimeError("Concat failed")

def run_debate_pipeline():
    cleanup_cache()
    if not OPENROUTER_API_KEY: raise RuntimeError("OPENROUTER_API_KEY missing")
    if not os.path.exists("topic.txt"):
        with open("topic.txt","w",encoding="utf-8") as f: f.write("Does the universe require a creator?")
    topic=open("topic.txt","r",encoding="utf-8").read().strip() or "Does the universe require a creator?"
    print(f"\nTOPIC: {topic}\n")
    avail=discover_models()
    if not avail:
        avail=FALLBACK_MODELS.copy()
    ap_model,sk_model=choose_primary_models(avail)
    print(f"Apologist: {get_judge_short_name(ap_model)}  Skeptic: {get_judge_short_name(sk_model)}")
    judges=choose_judges(avail,(ap_model,sk_model))
    if not judges:
        used=set(); judges=[]
        for m in FALLBACK_MODELS:
            prov=provider_from_model(m)
            if prov in used or m in (ap_model,sk_model): continue
            judges.append(m); used.add(prov)
            if len(judges)>=MAX_JUDGES: break
    print(f"Judges: {len(judges)} — {', '.join(get_judge_short_name(j) for j in judges)}")
    segs=[]; sid=0
    def add_seg(text,role,name,pos=None,glow=None,jvi=None):
        nonlocal sid
        vm=sk_model if role=="AI Skeptic" else ap_model
        v=create_segment(text,role,name,topic,sid,vm,pos,glow,jvi); segs.append(v); sid+=1
    add_seg(build_intro(topic,len(judges)),"Moderator","MODERATOR")
    prev=""; cum_a=0.0; cum_b=0.0; pcom=[]
    for rn in range(1,ROUNDS+1):
        print(f"\nROUND {rn}")
        a_turns,s_turns,prev=build_round_exchanges(topic,rn,ap_model,sk_model,prev)
        for ti in range(TURNS_PER_SIDE_PER_ROUND):
            print(f"  Exchange {ti+1}: A={count_words(a_turns[ti])} B={count_words(s_turns[ti])}")
            add_seg(a_turns[ti],"AI Christian Apologist","AI CHRISTIAN APOLOGIST","left","#00FFCC")
            add_seg(s_turns[ti],"AI Skeptic","AI SKEPTIC","right","#FF00FF")
        a_full="\n".join(a_turns); s_full="\n".join(s_turns)
        res=evaluate_round(judges,topic,rn,a_full,s_full)
        ra,rb=calculate_round_average(res); cum_a+=ra; cum_b+=rb
        print(f"Round {rn}: A {ra:.1f} vs B {rb:.1f} | Cum: {cum_a:.1f} vs {cum_b:.1f}")
        sb=f"scoreboard_r{rn}.png"
        generate_scoreboard(rn,res,ra,rb,cum_a,cum_b,sb)
        st=f"Round {rn} complete. Judges gave Apologist {ra:.1f} and Skeptic {rb:.1f}. Cumulative {cum_a:.1f} to {cum_b:.1f}."
        sa=f"score_audio_r{rn}.mp3"; ss=f"score_subs_r{rn}.ass"; sv=f"score_video_r{rn}.mp4"
        sw=generate_audio(st,"Moderator",sa); generate_subtitles(sw,ss,scorecard=True)
        render_scorecard_video(sb,sa,ss,sv); segs.append(sv)
        if res:
            a_res=[r for r in res if r["winner"]=="A"] or res
            b_res=[r for r in res if r["winner"]=="B"] or res
            ja=random.choice(a_res); jb=random.choice(b_res)
            ca=generate_panel_commentary(ja["model"],"A",topic,rn,a_full,s_full,pcom); pcom.append(ca)
            add_seg(ca,"AI Judge",f"AI JUDGE — {ja['display_name'].upper()}","center","#3399FF",judge_voice_index=0)
            cb=generate_panel_commentary(jb["model"],"B",topic,rn,a_full,s_full,pcom); pcom.append(cb)
            add_seg(cb,"AI Judge",f"AI JUDGE — {jb['display_name'].upper()}","center","#3399FF",judge_voice_index=1)
    add_seg(build_outro(len(judges),cum_a,cum_b),"Moderator","MODERATOR")
    stitch_segments(segs,OUTPUT_FILE)
    print(f"\nCOMPLETE: {OUTPUT_FILE}")
    print(f"Judges: {', '.join(get_judge_short_name(j) for j in judges)}")
    print(f"Final: Apologist {cum_a:.1f} vs Skeptic {cum_b:.1f}")
    cleanup_cache()

if __name__=="__main__":
    try: run_debate_pipeline()
    except KeyboardInterrupt: print("Cancelled")
    except Exception as e:
        print("FAILED"); print(str(e)); raise
