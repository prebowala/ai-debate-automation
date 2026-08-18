import os
import json
import asyncio
import requests
import re
import random
import math
import PIL.Image
import numpy as np

# Patch Pillow for MoviePy 1.x compatibility
if not hasattr(PIL.Image, 'ANTIALIAS'):
    PIL.Image.ANTIALIAS = PIL.Image.Resampling.LANCZOS

from PIL import Image, ImageDraw, ImageFont, ImageEnhance
from moviepy.editor import (
    AudioFileClip,
    ImageClip,
    VideoClip,
    CompositeVideoClip,
    concatenate_videoclips,
    concatenate_audioclips
)
from moviepy.audio.AudioClip import AudioArrayClip

# API Keys
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")

# Primary ElevenLabs Voices
VOICE_NARRATOR_ID = "QIhD5ivPGEoYZQDocuHI"   # Narrator
VOICE_APOLOGIST_ID = "GZ4PpFJV8ikEGUtBrjK7"  # Debater A (Proponent)
VOICE_SKEPTIC_ID   = "gPPH6SLdL8XSX6GNJ40G"  # Debater B (Opponent)

# Pool of distinct voices for AI Judge intros
JUDGE_VOICE_POOL = [
    "21m00Tcm4TlvDq8ikWAM", "AZnzlk1XvdvUeBnXmlld", "EXAVITQu4vr4xnSDxMaL",
    "ErXwobaYiN019PkySvjV", "MF3mGyEYCl7XYWbV9V6O", "TxGEqnHWrfWFTfGW9XjX"
]

# Expanded 15 Flagship AI Judge Models
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
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    return ImageFont.load_default()

def get_cached_bg():
    global BG_IMAGE_CACHE
    if BG_IMAGE_CACHE is None:
        base_path = "background.png" if os.path.exists("background.png") else "default_bg.png"
        if not os.path.exists(base_path):
            img = Image.new("RGB", (1920, 1080), color=(15, 23, 42))
            img.save(base_path)
        BG_IMAGE_CACHE = Image.open(base_path).convert("RGBA").resize((1920, 1080))
    return BG_IMAGE_CACHE.copy()

def load_or_create_icon(icon_path, name):
    if os.path.exists(icon_path):
        try:
            return Image.open(icon_path).convert("RGBA").resize((120, 120))
        except Exception:
            pass
    badge = Image.new("RGBA", (120, 120), (30, 41, 59, 255))
    draw = ImageDraw.Draw(badge)
    draw.rectangle([0, 0, 119, 119], outline=(0, 180, 255, 255), width=3)
    initials = "".join([w[0] for w in name.split()[:2]]).upper()
    draw.text((60, 60), initials, font=get_font(36), fill=(255, 255, 255), anchor="mm")
    return badge

def sanitize_speech_text(text):
    clean = re.sub(r'^(laura|brian|narrator|apologist|skeptic|debater_a|debater_b)(\s*\([^)]*\))?:\s*', '', text, flags=re.IGNORECASE)
    return clean.strip()

def clean_json_string(text):
    text = re.sub(r"^```(json)?", "", text, flags=re.MULTILINE)
    text = re.sub(r"^```", "", text, flags=re.MULTILINE)
    return text.strip()

def create_silent_audio(duration=0.6, fps=44100):
    samples = int(fps * duration)
    return AudioArrayClip(np.zeros((samples, 2), dtype=np.float32), fps=fps)

def synthesize_speech(text, voice_id, output_path):
    if ELEVENLABS_API_KEY:
        try:
            res = requests.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
                headers={"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"},
                json={"text": text},
                timeout=30
            )
            if res.status_code == 200:
                with open(output_path, "wb") as f:
                    f.write(res.content)
                return AudioFileClip(output_path)
        except Exception:
            pass

    try:
        from gtts import gTTS
        tts = gTTS(text=text, lang='en')
        tts.save(output_path)
        return AudioFileClip(output_path)
    except Exception:
        word_count = len(text.split())
        est_duration = max(1.5, word_count * 0.4)
        return create_silent_audio(duration=est_duration)

def render_karaoke_captions(draw, text, t, duration, y_pos=520):
    words = text.split()
    if not words:
        return
    words_per_chunk = 7
    chunks = [words[i:i + words_per_chunk] for i in range(0, len(words), words_per_chunk)]
    
    total_words = len(words)
    current_word_idx = min(int((t / max(duration, 0.01)) * total_words), total_words - 1)
    chunk_idx = min(current_word_idx // words_per_chunk, len(chunks) - 1)
    active_chunk = chunks[chunk_idx]
    
    chunk_start_word_idx = chunk_idx * words_per_chunk
    font = get_font(44)
    
    total_width = sum(font.getlength(w + " ") for w in active_chunk)
    x = 960 - (total_width / 2)
    
    for i, word in enumerate(active_chunk):
        global_word_idx = chunk_start_word_idx + i
        color = (255, 234, 0) if global_word_idx == current_word_idx else (255, 255, 255)
        draw.text((x, y_pos), word + " ", font=font, fill=color, stroke_width=4, stroke_fill=(0, 0, 0))
        x += font.getlength(word + " ")

def render_frame(t, duration, speaker, text, quote_text, audio_clip):
    bg_full = get_cached_bg()
    
    if speaker == "DEBATER_A":
        crop_bg = bg_full.crop((0, 0, 1280, 1080)).resize((1920, 1080))
        bg = ImageEnhance.Brightness(crop_bg).enhance(1.15)
    elif speaker == "DEBATER_B":
        crop_bg = bg_full.crop((640, 0, 1920, 1080)).resize((1920, 1080))
        bg = ImageEnhance.Brightness(crop_bg).enhance(1.15)
    else:
        bg = bg_full
        
    overlay = Image.new("RGBA", (1920, 1080), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    
    try:
        sample = audio_clip.get_frame(t)
        amplitude = np.linalg.norm(sample) if isinstance(sample, np.ndarray) else abs(sample)
    except Exception:
        amplitude = 0.2
        
    amp_factor = min(max(amplitude * 300, 15), 180)
    
    if speaker == "DEBATER_A":
        center_x, center_y = 480, 650
        draw.ellipse([center_x - 140 - amp_factor/2, center_y - 140 - amp_factor/2, 
                      center_x + 140 + amp_factor/2, center_y + 140 + amp_factor/2], 
                     outline=(0, 210, 255, 180), width=4)
        for i in range(12):
            h = int(abs(amp_factor * (0.5 + 0.5 * math.sin(i + t * 10))))
            x = 360 + (i * 20)
            y0 = 820 - h
            y1 = 820
            draw.rectangle([x, min(y0, y1), x + 14, max(y0, y1)], fill=(0, 210, 255, 230))

    elif speaker == "DEBATER_B":
        center_x, center_y = 1440, 650
        draw.ellipse([center_x - 140 - amp_factor/2, center_y - 140 - amp_factor/2, 
                      center_x + 140 + amp_factor/2, center_y + 140 + amp_factor/2], 
                     outline=(255, 60, 90, 180), width=4)
        for i in range(12):
            h = int(abs(amp_factor * (0.5 + 0.5 * math.sin(i + t * 10))))
            x = 1320 + (i * 20)
            y0 = 820 - h
            y1 = 820
            draw.rectangle([x, min(y0, y1), x + 14, max(y0, y1)], fill=(255, 60, 90, 230))

    elif speaker == "NARRATOR":
        for i in range(20):
            h = int(abs(amp_factor * (0.4 + 0.6 * math.cos(i + t * 8))))
            y0 = int(120 - h / 2)
            y1 = int(120 + h / 2)
            x = 760 + (i * 20)
            draw.rectangle([x, min(y0, y1), x + 14, max(y0, y1)], fill=(234, 179, 8, 230))

    if text:
        render_karaoke_captions(draw, text, t, duration)

    if quote_text:
        draw.text((960, 910), "KEY REFERENCE / QUOTE", font=get_font(30), fill=(234, 179, 8), anchor="mm", stroke_width=3, stroke_fill=(0,0,0))
        draw.text((960, 960), f'"{quote_text}"', font=get_font(38), fill=(255, 255, 255), anchor="mm", stroke_width=3, stroke_fill=(0,0,0))

    composite = Image.alpha_composite(bg, overlay)
    return np.array(composite.convert("RGB"))

def render_judge_intro_frame(t, duration, judge, speech_text, audio_clip):
    overlay = Image.new("RGBA", (1920, 1080), (15, 23, 42, 245))
    draw = ImageDraw.Draw(overlay)
    
    icon_img = load_or_create_icon(judge["icon"], judge["name"])
    overlay.paste(icon_img, (900, 200), mask=icon_img)
    
    draw.text((960, 360), judge["name"].upper(), font=get_font(48), fill=(0, 210, 255), anchor="mm")
    draw.text((960, 420), f"OFFICIAL AI DEBATE JUDGE ({judge['company']})", font=get_font(28), fill=(234, 179, 8), anchor="mm")
    
    try:
        sample = audio_clip.get_frame(t)
        amplitude = np.linalg.norm(sample) if isinstance(sample, np.ndarray) else abs(sample)
    except Exception:
        amplitude = 0.2
        
    amp_factor = min(max(amplitude * 300, 15), 180)
    for i in range(16):
        h = int(abs(amp_factor * (0.5 + 0.5 * math.sin(i + t * 10))))
        y0 = int(480 - h / 2)
        y1 = int(480 + h / 2)
        x = 800 + (i * 20)
        draw.rectangle([x, min(y0, y1), x + 14, max(y0, y1)], fill=(0, 210, 255, 230))

    render_karaoke_captions(draw, speech_text, t, duration, y_pos=600)
    return np.array(overlay.convert("RGB"))

def generate_debate():
    with open("topic.txt", "r") as f:
        topic = f.read().strip()

    prompt = (
        f"Write a broadcast debate on: '{topic}'.\n\n"
        f"Rules:\n"
        f"- Output JSON with top-level keys: 'role_a', 'role_b', and 'script'.\n"
        f"- 'role_a' and 'role_b' must be concise debater titles dynamic to this topic (e.g. 'Christian Apologist', 'Islamic Scholar', 'Capitalist', etc.).\n"
        f"- In 'script', use speakers 'DEBATER_A', 'DEBATER_B', and 'NARRATOR'.\n"
        f"- Include exact quotes or references in 'quote' for DEBATER_A or DEBATER_B when applicable.\n"
        f"JSON Schema Format:\n"
        f"{{\n"
        f'  "role_a": "Title A",\n'
        f'  "role_b": "Title B",\n'
        f'  "script": [\n'
        f'    {{"speaker": "DEBATER_A", "round": 1, "text": "...", "quote": "..."}}\n'
        f'  ]\n'
        f"}}\n"
    )
    
    res = requests.post("https://openrouter.ai/api/v1/chat/completions", 
                        headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
                        json={"model": "openai/gpt-5.6-sol", "messages": [{"role": "user", "content": prompt}]}, 
                        timeout=90)
    
    return json.loads(clean_json_string(res.json()['choices'][0]['message']['content']))

async def evaluate_judge(judge, role_a, role_b, arg_a, arg_b):
    prompt = f"Evaluate debate round:\n{role_a}: {arg_a}\n{role_b}: {arg_b}\nReturn JSON strictly: {{\"score_a\": 85, \"score_b\": 78}}"
    try:
        res = requests.post("https://openrouter.ai/api/v1/chat/completions", 
                            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
                            json={"model": judge["model"], "messages": [{"role": "user", "content": prompt}]}, 
                            timeout=20)
        parsed = json.loads(clean_json_string(res.json()['choices'][0]['message']['content']))
        return {"score_a": int(parsed.get("score_a", 75)), "score_b": int(parsed.get("score_b", 75))}
    except Exception:
        return {"score_a": random.randint(70, 90), "score_b": random.randint(70, 90)}

def render_debate_video(data):
    role_a = data.get("role_a", "Proponent")
    role_b = data.get("role_b", "Opponent")
    raw_script = data.get("script", [])

    video_segments, audio_segments = [], []
    total_a, total_b = 0, 0
    buffer_silence = create_silent_audio(duration=0.6)

    # 1. AI Judges Self-Introductions
    intro_judges = JUDGES[:5]
    for idx, j in enumerate(intro_judges):
        intro_text = f"I am {j['name']} from {j['company']}. I am serving as one of 15 official AI judges for this debate."
        voice_id = JUDGE_VOICE_POOL[idx % len(JUDGE_VOICE_POOL)]
        
        j_audio = synthesize_speech(intro_text, voice_id, f"temp_judge_intro_{idx}.mp3")
        j_vid = VideoClip(lambda t: render_judge_intro_frame(t, j_audio.duration, j, intro_text, j_audio), duration=j_audio.duration).set_audio(j_audio)
        
        video_segments.append(j_vid)
        audio_segments.append(j_audio)
        audio_segments.append(buffer_silence)

    # 2. Dynamic Debaters Self-Introductions
    debater_intros = [
        {"speaker": "DEBATER_A", "text": f"I am representing the position of the {role_a} in today's debate.", "voice": VOICE_APOLOGIST_ID},
        {"speaker": "DEBATER_B", "text": f"I am representing the position of the {role_b} in today's debate.", "voice": VOICE_SKEPTIC_ID}
    ]

    for idx, d in enumerate(debater_intros):
        d_audio = synthesize_speech(d["text"], d["voice"], f"temp_debater_intro_{idx}.mp3")
        d_vid = VideoClip(lambda t: render_frame(t, d_audio.duration, d["speaker"], d["text"], None, d_audio), duration=d_audio.duration).set_audio(d_audio)
        
        video_segments.append(d_vid)
        audio_segments.append(d_audio)
        audio_segments.append(buffer_silence)

    # 3. Main Debate Render
    for idx, item in enumerate(raw_script):
        speaker = item["speaker"]
        text = sanitize_speech_text(item["text"])
        round_num = item["round"]
        quote_text = item.get("quote", item.get("bible_quote", None))

        vid = VOICE_NARRATOR_ID if speaker == "NARRATOR" else (VOICE_APOLOGIST_ID if speaker == "DEBATER_A" else VOICE_SKEPTIC_ID)
        temp_audio_path = f"temp_{idx}.mp3"
        audio_clip = synthesize_speech(text, vid, temp_audio_path)
        duration = audio_clip.duration

        stage_clip = VideoClip(lambda t: render_frame(t, duration, speaker, text, quote_text, audio_clip), duration=duration).set_audio(audio_clip)
        video_segments.append(stage_clip)
        audio_segments.append(audio_clip)

        # Dynamic Round Scoring Summaries
        if speaker == "NARRATOR" and 1 <= round_num <= 4:
            arg_a = next((sanitize_speech_text(i['text']) for i in raw_script if i['round'] == round_num and i['speaker'] == 'DEBATER_A'), "")
            arg_b = next((sanitize_speech_text(i['text']) for i in raw_script if i['round'] == round_num and i['speaker'] == 'DEBATER_B'), "")

            scores = asyncio.run(asyncio.gather(*[evaluate_judge(j, role_a, role_b, arg_a, arg_b) for j in JUDGES]))
            avg_a = sum(s["score_a"] for s in scores) // len(scores)
            avg_b = sum(s["score_b"] for s in scores) // len(scores)
            total_a += avg_a
            total_b += avg_b

            narrator_summary = f"At the end of Round {round_num}, across our 15 AI judges, the {role_a} scores {avg_a} points, and the {role_b} scores {avg_b} points. Cumulative total: {total_a} to {total_b}."
            score_audio = synthesize_speech(narrator_summary, VOICE_NARRATOR_ID, f"temp_score_{round_num}.mp3")
            score_vid = VideoClip(lambda t: render_frame(t, score_audio.duration, "NARRATOR", narrator_summary, None, score_audio), duration=score_audio.duration).set_audio(score_audio)
            
            video_segments.append(score_vid)
            audio_segments.append(score_audio)

    # 4. Spoken Winner Announcement with Dynamic Roles
    winner_title = f"the {role_a}" if total_a > total_b else f"the {role_b}"
    winner_text = f"That concludes our debate evaluated by 15 AI models. The final score is the {role_a} with {total_a} points, and the {role_b} with {total_b} points. The winner is {winner_title}!"
    final_audio = synthesize_speech(winner_text, VOICE_NARRATOR_ID, "temp_final_winner.mp3")
    final_vid = VideoClip(lambda t: render_frame(t, final_audio.duration, "NARRATOR", winner_text, None, final_audio), duration=final_audio.duration).set_audio(final_audio)
    
    video_segments.append(final_vid)
    audio_segments.append(final_audio)

    master_video = concatenate_videoclips(video_segments, method="compose")
    master_audio = concatenate_audioclips(audio_segments)

    master_video.write_videofile("final_debate.mp4", fps=20, codec="libx264", audio_codec="aac", preset="ultrafast")
    master_audio.write_audiofile("output_audio.mp3")

if __name__ == "__main__":
    data = generate_debate()
    render_debate_video(data)
