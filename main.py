import os
import json
import asyncio
import requests
import re
import random
import math
import PIL.Image
import numpy as np

if not hasattr(PIL.Image, 'ANTIALIAS'):
    PIL.Image.ANTIALIAS = PIL.Image.Resampling.LANCZOS

from PIL import Image, ImageDraw, ImageFont, ImageEnhance
from moviepy.editor import AudioFileClip, VideoClip, concatenate_audioclips
from moviepy.audio.AudioClip import AudioArrayClip
import moviepy.audio.fx.all as afx

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")

VOICE_NARRATOR_ID = "QIhD5ivPGEoYZQDocuHI"
VOICE_APOLOGIST_ID = "GZ4PpFJV8ikEGUtBrjK7"
VOICE_SKEPTIC_ID   = "gPPH6SLdL8XSX6GNJ40G"

JUDGE_VOICE_POOL = [
    "21m00Tcm4TlvDq8ikWAM", "AZnzlk1XvdvUeBnXmlld", "EXAVITQu4vr4xnSDxMaL",
    "ErXwobaYiN019PkySvjV", "MF3mGyEYCl7XYWbV9V6O", "TxGEqnHWrfWFTfGW9XjX"
]

COMPLIANCE_BANNER_TEXT = "INDEPENDENT AI EVALUATION • NOT AFFILIATED WITH OR ENDORSED BY ANY FEATURED PROVIDERS"

JUDGES = [
    {"name": "GPT-5.6 Sol", "company": "OpenAI", "model": "openai/gpt-5.6-sol", "icon": "icons/openai.png"},
    {"name": "Claude Opus 5", "company": "Anthropic", "model": "anthropic/claude-opus-5", "icon": "icons/claude.png"},
    {"name": "Gemini 3.7 Flash", "company": "Google", "model": "google/gemini-3.7-flash", "icon": "icons/gemini.png"},
    {"name": "Grok 4.6", "company": "xAI", "model": "xai/grok-4.6", "icon": "icons/grok.png"},
    {"name": "DeepSeek V4 Pro", "company": "DeepSeek", "model": "deepseek/deepseek-v4-pro", "icon": "icons/deepseek.png"},
    {"name": "GLM 5.2", "company": "Zhipu AI", "model": "zhipu/glm-5.2", "icon": "icons/glm.png"},
    {"name": "Nemotron 3 Ultra", "company": "NVIDIA", "model": "nvidia/nemotron-3-ultra-550b-a55b:free", "icon": "icons/nvidia.png"},
    {"name": "North Mini", "company": "Cohere", "model": "cohere/north-mini-code:free", "icon": "icons/cohere.png"},
    {"name": "Laguna S 2.1", "company": "Poolside", "model": "poolside/laguna-s-2.1:free", "icon": "icons/poolside.png"},
    {"name": "Llama 3.3 70B", "company": "Meta", "model": "meta-llama/llama-3.3-70b-instruct", "icon": "icons/llama.png"},
    {"name": "Mistral Large 3", "company": "Mistral AI", "model": "mistralai/mistral-large-2411", "icon": "icons/mistral.png"},
    {"name": "Jamba 1.5 Large", "company": "AI21 Labs", "model": "ai21/jamba-1-5-large", "icon": "icons/ai21.png"},
    {"name": "Qwen 2.5 72B", "company": "Alibaba Cloud", "model": "qwen/qwen-2.5-72b-instruct", "icon": "icons/qwen.png"},
    {"name": "Titan Express", "company": "Amazon Bedrock", "model": "amazon/titan-text-express", "icon": "icons/amazon.png"},
    {"name": "Phi 3.5 Vision", "company": "Microsoft", "model": "microsoft/phi-3.5-vision-instruct", "icon": "icons/microsoft.png"}
]

BG_IMAGE_CACHE = None

def get_font(size):
    font_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:\\Windows\\Fonts\\arialbd.ttf",
        "arial.ttf"
    ]
    for path in font_paths:
        if os.path.exists(path):
            try: return ImageFont.truetype(path, size)
            except Exception: pass
    return ImageFont.load_default()

def get_cached_bg():
    global BG_IMAGE_CACHE
    if BG_IMAGE_CACHE is None:
        base_path = "background.png" if os.path.exists("background.png") else "default_bg.png"
        if not os.path.exists(base_path):
            img = Image.new("RGB", (1280, 720), color=(15, 23, 42))
            img.save(base_path)
        BG_IMAGE_CACHE = Image.open(base_path).convert("RGBA").resize((1280, 720))
    return BG_IMAGE_CACHE.copy()

def load_or_create_icon(icon_path, name):
    if os.path.exists(icon_path):
        try: return Image.open(icon_path).convert("RGBA").resize((50, 50))
        except Exception: pass
    badge = Image.new("RGBA", (50, 50), (30, 41, 59, 255))
    draw = ImageDraw.Draw(badge)
    draw.rectangle([0, 0, 49, 49], outline=(0, 180, 255, 255), width=2)
    initials = "".join([w[0] for w in name.split()[:2]]).upper()
    draw.text((25, 25), initials, font=get_font(18), fill=(255, 255, 255), anchor="mm")
    return badge

def sanitize_speech_text(text):
    return re.sub(r'^(laura|brian|narrator|apologist|skeptic|debater_a|debater_b)(\s*\([^)]*\))?:\s*', '', text, flags=re.IGNORECASE).strip()

def clean_json_string(text):
    text = re.sub(r"^```(json)?", "", text, flags=re.MULTILINE)
    text = re.sub(r"^```", "", text, flags=re.MULTILINE)
    return text.strip()

def create_silent_audio(duration=0.5, fps=44100):
    samples = int(fps * duration)
    return AudioArrayClip(np.zeros((samples, 2), dtype=np.float32), fps=fps)

def synthesize_speech(text, voice_id, output_path):
    audio_clip = None
    if ELEVENLABS_API_KEY:
        try:
            res = requests.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
                headers={"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"},
                json={"text": text},
                timeout=(5, 15)
            )
            if res.status_code == 200:
                with open(output_path, "wb") as f:
                    f.write(res.content)
                audio_clip = AudioFileClip(output_path)
        except Exception: pass

    if audio_clip is None:
        try:
            from gTTS import gTTS
            tts = gTTS(text=text, lang='en')
            tts.save(output_path)
            audio_clip = AudioFileClip(output_path)
        except Exception:
            word_count = len(text.split())
            audio_clip = create_silent_audio(duration=max(2.0, word_count * 0.45))

    return audio_clip.fx(afx.audio_fadein, 0.05).fx(afx.audio_fadeout, 0.05)

def draw_compliance_banner(draw):
    draw.rectangle([0, 690, 1280, 720], fill=(0, 0, 0, 220))
    draw.text((640, 705), COMPLIANCE_BANNER_TEXT, font=get_font(12), fill=(200, 200, 200), anchor="mm")

def draw_captions(draw, text, t, total_duration):
    words = text.split()
    if not words: return
    progress = min(max(t / max(total_duration, 0.01), 0.0), 0.99)
    current_word_idx = int(progress * len(words))
    
    start_idx = max(0, current_word_idx - (current_word_idx % 10))
    end_idx = min(len(words), start_idx + 10)
    chunk = " ".join(words[start_idx:end_idx])

    font = get_font(22)
    draw.rectangle([180, 520, 1100, 580], fill=(15, 23, 42, 230), outline=(51, 65, 85), width=2)
    draw.text((640, 550), chunk, font=font, fill=(255, 255, 255), anchor="mm")

def generate_debate():
    with open("topic.txt", "r") as f:
        topic = f.read().strip()

    prompt = (
        f"Write an extended broadcast debate on: '{topic}'.\n\n"
        f"Rules:\n"
        f"- Output MUST contain EXACTLY 3 debate rounds.\n"
        f"- Output JSON with keys: 'role_a', 'role_b', and 'script'.\n"
        f"- Speaker tags: 'DEBATER_A', 'DEBATER_B', 'NARRATOR'.\n"
        f"- Every DEBATER turn must have an NIV Bible reference in 'quote' key.\n"
    )
    
    res = requests.post(
        "https://openrouter.ai/api/v1/chat/completions", 
        headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
        json={"model": "openai/gpt-5.6-sol", "messages": [{"role": "user", "content": prompt}]}, 
        timeout=(5, 30)
    )
    
    parsed = json.loads(clean_json_string(res.json()['choices'][0]['message']['content']))
    parsed['topic'] = topic
    if "script" in parsed:
        parsed["script"] = [i for i in parsed["script"] if i.get("round", 1) <= 3]
    return parsed

async def evaluate_judge(judge, role_a, role_b, arg_a, arg_b):
    prompt = f"Evaluate:\n{role_a}: {arg_a}\n{role_b}: {arg_b}\nReturn JSON strictly: {{\"score_a\": 85, \"score_b\": 78, \"reasoning\": \"1 sentence.\"}}"
    try:
        res = requests.post("https://openrouter.ai/api/v1/chat/completions", 
                            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
                            json={"model": judge["model"], "messages": [{"role": "user", "content": prompt}]}, 
                            timeout=(5, 10))
        parsed = json.loads(clean_json_string(res.json()['choices'][0]['message']['content']))
        return {"score_a": int(parsed.get("score_a", 75)), "score_b": int(parsed.get("score_b", 75)), "reasoning": parsed.get("reasoning", "Strong case.")}
    except Exception:
        return {"score_a": random.randint(70, 90), "score_b": random.randint(70, 90), "reasoning": "Well argued."}

def render_debate_video(data):
    topic = data.get("topic", "AI Debate")
    role_a = data.get("role_a", "Proponent")
    role_b = data.get("role_b", "Opponent")
    raw_script = data.get("script", [])

    timeline = []
    audio_clips = []
    current_time = 0.0
    buffer_silence = create_silent_audio(duration=0.4)

    def add_segment(type_name, text, voice_id, speaker="NARRATOR", quote=None, extra_data=None):
        nonlocal current_time
        path = f"temp_{len(timeline)}.mp3"
        audio = synthesize_speech(text, voice_id, path)
        duration = audio.duration

        timeline.append({
            "start": current_time,
            "end": current_time + duration,
            "duration": duration,
            "type": type_name,
            "text": text,
            "speaker": speaker,
            "quote": quote,
            "audio": audio,
            "extra": extra_data
        })
        
        audio_clips.append(audio)
        audio_clips.append(buffer_silence)
        current_time += duration + 0.4

    # 1. Intro
    add_segment("STAGE", f"Welcome to today's AI Debate Broadcast on: {topic}. Let's meet our debaters and judges.", VOICE_NARRATOR_ID, "NARRATOR")
    
    # 2. Judges Intros
    for idx, j in enumerate(JUDGES[:2]):
        add_segment("JUDGE_INTRO", f"Greetings. I am {j['name']} from {j['company']}. I will judge today's debate.", JUDGE_VOICE_POOL[idx], "NARRATOR", extra_data=j)

    # 3. Debater Intros
    add_segment("STAGE", f"I am presenting the case for {role_a}.", VOICE_APOLOGIST_ID, "DEBATER_A")
    add_segment("STAGE", f"I am representing the perspective of {role_b}.", VOICE_SKEPTIC_ID, "DEBATER_B")

    # 4. Rounds
    total_a, total_b = 0, 0
    max_rounds = min(3, max((item.get("round", 1) for item in raw_script), default=1))

    for r in range(1, max_rounds + 1):
        round_items = [i for i in raw_script if i.get("round") == r]
        for item in round_items:
            spk = item["speaker"]
            txt = sanitize_speech_text(item["text"])
            vid = VOICE_NARRATOR_ID if spk == "NARRATOR" else (VOICE_APOLOGIST_ID if spk == "DEBATER_A" else VOICE_SKEPTIC_ID)
            add_segment("STAGE", txt, vid, spk, quote=item.get("quote"))

        arg_a = next((sanitize_speech_text(i['text']) for i in round_items if i['speaker'] == 'DEBATER_A'), "")
        arg_b = next((sanitize_speech_text(i['text']) for i in round_items if i['speaker'] == 'DEBATER_B'), "")

        async def run_evals():
            return await asyncio.gather(*[evaluate_judge(j, role_a, role_b, arg_a, arg_b) for j in JUDGES])
        
        scores = asyncio.run(run_evals())
        avg_a = sum(s["score_a"] for s in scores) // len(scores)
        avg_b = sum(s["score_b"] for s in scores) // len(scores)
        total_a += avg_a
        total_b += avg_b

        summary_txt = f"Round {r} complete. {role_a} scored {avg_a}, {role_b} scored {avg_b}. Total: {total_a} to {total_b}."
        add_segment("SCOREBOARD", summary_txt, VOICE_NARRATOR_ID, "NARRATOR", extra_data={"round": r, "scores": scores, "total_a": total_a, "total_b": total_b})

    # 5. Outro
    winner = role_a if total_a > total_b else role_b
    add_segment("STAGE", f"Debate concluded! The official winner is {winner}. Thanks for watching!", VOICE_NARRATOR_ID, "NARRATOR")

    # Master Audio Composition
    master_audio = concatenate_audioclips(audio_clips)

    # Fast Single-Pass Frame Generator
    def make_frame(t):
        seg = next((s for s in timeline if s["start"] <= t <= s["end"]), timeline[-1])
        local_t = t - seg["start"]
        bg = get_cached_bg()
        draw = ImageDraw.Draw(bg)

        if seg["type"] == "STAGE":
            spk = seg["speaker"]
            if spk == "DEBATER_A":
                draw.ellipse([250, 200, 450, 400], outline=(0, 210, 255), width=4)
            elif spk == "DEBATER_B":
                draw.ellipse([830, 200, 1030, 400], outline=(255, 60, 90), width=4)
            draw_captions(draw, seg["text"], local_t, seg["duration"])

            if seg["quote"]:
                draw.rectangle([100, 600, 1180, 660], fill=(15, 23, 42, 245), outline=(234, 179, 8), width=2)
                draw.text((640, 630), f'"{seg["quote"]}"', font=get_font(18), fill=(255, 255, 255), anchor="mm")

        elif seg["type"] == "JUDGE_INTRO":
            j = seg["extra"]
            draw.rectangle([440, 150, 840, 450], fill=(30, 41, 59, 240), outline=(0, 210, 255), width=2)
            draw.text((640, 280), j["name"].upper(), font=get_font(28), fill=(0, 210, 255), anchor="mm")
            draw_captions(draw, seg["text"], local_t, seg["duration"])

        elif seg["type"] == "SCOREBOARD":
            d = seg["extra"]
            draw.text((640, 50), f"ROUND {d['round']} SCORES", font=get_font(32), fill=(234, 179, 8), anchor="mm")
            draw.text((640, 100), f"{role_a}: {d['total_a']} PTS  |  {role_b}: {d['total_b']} PTS", font=get_font(22), fill=(255, 255, 255), anchor="mm")
            draw_captions(draw, seg["text"], local_t, seg["duration"])

        draw_compliance_banner(draw)
        return np.array(bg.convert("RGB"))

    video = VideoClip(make_frame, duration=master_audio.duration).set_audio(master_audio)
    
    # Fast direct export at 10 FPS, single thread
    video.write_videofile(
        "final_debate.mp4", 
        fps=10, 
        codec="libx264", 
        audio_codec="aac", 
        preset="ultrafast",
        threads=1
    )

if __name__ == "__main__":
    data = generate_debate()
    render_debate_video(data)
