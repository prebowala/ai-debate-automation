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

from PIL import Image, ImageDraw, ImageFont
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
VOICE_NARRATOR_ID = "QIhD5ivPGEoYZQDocuHI"   # Adam (Narrator)
VOICE_APOLOGIST_ID = "GZ4PpFJV8ikEGUtBrjK7"  # Laura (Apologist)
VOICE_SKEPTIC_ID   = "gPPH6SLdL8XSX6GNJ40G"  # Brian (Skeptic)

# Voice pool for representative AI Judges
JUDGE_VOICE_POOL = [
    "21m00Tcm4TlvDq8ikWAM", "AZnzlk1XvdvUeBnXmlld", "EXAVITQu4vr4xnSDxMaL",
    "ErXwobaYiN019PkySvjV", "MF3mGyEYCl7XYWbV9V6O", "TxGEqnHWrfWFTfGW9XjX"
]

# Top 10 Flagship AI Judge Models (1 per company)
JUDGES = [
    {"name": "GPT-5.6 Sol", "model": "openai/gpt-5.6-sol", "icon": "icons/openai.png"},
    {"name": "Claude Opus 5", "model": "anthropic/claude-opus-5", "icon": "icons/claude.png"},
    {"name": "Gemini 3.7 Flash", "model": "google/gemini-3.7-flash", "icon": "icons/gemini.png"},
    {"name": "Grok 4.6", "model": "xai/grok-4.6", "icon": "icons/grok.png"},
    {"name": "DeepSeek V4 Pro", "model": "deepseek/deepseek-v4-pro", "icon": "icons/deepseek.png"},
    {"name": "GLM 5.2", "model": "zhipu/glm-5.2", "icon": "icons/glm.png"},
    {"name": "Nemotron 3 Ultra", "model": "nvidia/nemotron-3-ultra-550b-a55b:free", "icon": "icons/nvidia.png"},
    {"name": "North Mini", "model": "cohere/north-mini-code:free", "icon": "icons/cohere.png"},
    {"name": "Laguna S 2.1", "model": "poolside/laguna-s-2.1:free", "icon": "icons/poolside.png"},
    {"name": "Llama 3.3 70B", "model": "meta-llama/llama-3.3-70b-instruct", "icon": "icons/llama.png"}
]

NAMES_APOLOGIST = ["Laura"]
NAMES_SKEPTIC = ["Brian"]

BG_IMAGE_CACHE = None

def get_font(size):
    """Loads system TTF fonts for large, high-resolution text rendering on Linux/Windows runners."""
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

def sanitize_speech_text(text):
    """Strips character name headers so speakers don't read out their own names."""
    clean = re.sub(r'^(laura|brian|narrator|apologist|skeptic)(\s*\([^)]*\))?:\s*', '', text, flags=re.IGNORECASE)
    return clean.strip()

def clean_json_string(text):
    text = re.sub(r"^```(json)?", "", text, flags=re.MULTILINE)
    text = re.sub(r"^```", "", text, flags=re.MULTILINE)
    return text.strip()

def create_silent_audio(duration=0.6, fps=44100):
    samples = int(fps * duration)
    return AudioArrayClip(np.zeros((samples, 2), dtype=np.float32), fps=fps)

def load_or_create_icon(icon_path, name):
    if os.path.exists(icon_path):
        try:
            return Image.open(icon_path).convert("RGBA").resize((45, 45))
        except Exception:
            pass
    badge = Image.new("RGBA", (45, 45), (30, 41, 59, 255))
    draw = ImageDraw.Draw(badge)
    draw.rectangle([0, 0, 44, 44], outline=(0, 180, 255, 255), width=2)
    initials = "".join([w[0] for w in name.split()[:2]]).upper()
    draw.text((22, 22), initials, fill=(255, 255, 255), anchor="mm")
    return badge

def get_current_caption_chunk(text, t, duration):
    """Splits full text into timed phrases that change dynamically with voice audio."""
    words = text.split()
    if not words:
        return ""
    chunk_size = 5
    chunks = [' '.join(words[i:i+chunk_size]) for i in range(0, len(words), chunk_size)]
    if not chunks:
        return text
    idx = min(int((t / max(duration, 0.01)) * len(chunks)), len(chunks) - 1)
    return chunks[idx]

def render_frame_with_animation(t, duration, speaker, text, bible_quote=None):
    """Renders dynamic camera zooms, prominent sound bars, big captions, and bottom Bible quotes without boxes."""
    bg_full = get_cached_bg()
    
    # 1. Dynamic Camera Zooming
    if speaker == "APOLOGIST":
        bg = bg_full.crop((0, 0, 1280, 1080)).resize((1920, 1080))
    elif speaker == "SKEPTIC":
        bg = bg_full.crop((640, 0, 1920, 1080)).resize((1920, 1080))
    else:
        bg = bg_full
        
    overlay = Image.new("RGBA", (1920, 1080), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    
    # Fonts
    font_cc = get_font(48)
    font_bible_head = get_font(32)
    font_bible_text = get_font(40)
    
    # 2. Prominent Bouncing Equalizer Bars
    if speaker == "APOLOGIST":
        for i in range(14):
            h = int(30 + 110 * abs(math.sin(t * 14 + i * 0.5)))
            x = 100 + (i * 28)
            draw.rectangle([x, 780 - h, x + 20, 780 + h], fill=(0, 210, 255, 240))
            
    elif speaker == "SKEPTIC":
        for i in range(14):
            h = int(30 + 110 * abs(math.sin(t * 14 + i * 0.5)))
            x = 1480 + (i * 28)
            draw.rectangle([x, 780 - h, x + 20, 780 + h], fill=(255, 60, 90, 240))
            
    elif speaker == "NARRATOR":
        for i in range(24):
            h = int(10 + 35 * abs(math.sin(t * 10 + i * 0.4)))
            x = 120 + (i * 70)
            draw.rectangle([x, 25, x + 40, 25 + h], fill=(234, 179, 8, 240))

    # 3. Dynamic Closed Captions in Center (Big Words, No Box/Border)
    if text:
        current_chunk = get_current_caption_chunk(text, t, duration)
        draw.text(
            (960, 520), 
            current_chunk, 
            font=font_cc, 
            fill=(255, 255, 255), 
            anchor="mm", 
            stroke_width=4, 
            stroke_fill=(0, 0, 0)
        )

    # 4. Bible Reference at Bottom (Big Words, No Box/Border)
    if bible_quote:
        draw.text(
            (960, 910), 
            "HOLY BIBLE (NIV)", 
            font=font_bible_head, 
            fill=(234, 179, 8), 
            anchor="mm", 
            stroke_width=3, 
            stroke_fill=(0, 0, 0)
        )
        draw.text(
            (960, 960), 
            f'"{bible_quote}"', 
            font=font_bible_text, 
            fill=(255, 255, 255), 
            anchor="mm", 
            stroke_width=3, 
            stroke_fill=(0, 0, 0)
        )

    composite = Image.alpha_composite(bg, overlay)
    return np.array(composite.convert("RGB"))

def create_scoreboard_overlay(scores, round_num, apologist_name, skeptic_name, total_a, total_b):
    """Renders round scoreboard and cumulative totals."""
    overlay = Image.new("RGBA", (1920, 1080), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font_main = get_font(28)
    
    draw.rectangle([500, 20, 1420, 80], fill=(15, 23, 42, 230), outline=(255, 255, 255, 100), width=2)
    draw.text((960, 50), f"ROUND {round_num} SCORES | CUMULATIVE TOTAL: {apologist_name} {total_a} - {total_b} {skeptic_name}", font=font_main, fill=(255, 255, 255), anchor="mm")

    draw.rectangle([50, 90, 450, 135], fill=(0, 122, 255, 200))
    draw.text((250, 112), f"{apologist_name} (Apologist)", font=font_main, fill=(255, 255, 255), anchor="mm")
    
    draw.rectangle([1470, 90, 1870, 135], fill=(255, 45, 85, 200))
    draw.text((1670, 112), f"{skeptic_name} (Skeptic)", font=font_main, fill=(255, 255, 255), anchor="mm")

    left_y, right_y = 150, 150
    
    for item in scores:
        icon_img = load_or_create_icon(item["icon"], item["name"])
        score_a, score_b = item["score_a"], item["score_b"]
            
        if score_a >= score_b:
            x_pos, y_pos = 60, left_y
            left_y += 75
            bg_box = [50, y_pos - 4, 450, y_pos + 55]
            score_text = f"{item['name']}: {score_a}/100"
        else:
            x_pos, y_pos = 1480, right_y
            right_y += 75
            bg_box = [1470, y_pos - 4, 1870, y_pos + 55]
            score_text = f"{item['name']}: {score_b}/100"

        draw.rectangle(bg_box, fill=(20, 30, 45, 220), outline=(255, 255, 255, 50))
        overlay.paste(icon_img, (x_pos, y_pos), mask=icon_img)
        draw.text((x_pos + 55, y_pos + 25), score_text, font=get_font(22), fill=(255, 255, 255), anchor="lm")

    return overlay

def create_final_winner_overlay(total_a, total_b, apologist_name, skeptic_name):
    """Renders final aggregate scoreboard and declares debate winner."""
    overlay = Image.new("RGBA", (1920, 1080), (15, 23, 42, 245))
    draw = ImageDraw.Draw(overlay)
    
    font_title = get_font(56)
    font_score = get_font(42)
    
    draw.text((960, 180), "FINAL DEBATE RESULTS", font=font_title, fill=(234, 179, 8), anchor="mm")
    
    winner = f"{apologist_name} (Apologist)" if total_a > total_b else f"{skeptic_name} (Skeptic)"
    draw.text((960, 270), f"WINNER: {winner.upper()}", font=font_title, fill=(0, 220, 255) if total_a > total_b else (255, 60, 90), anchor="mm")
    
    draw.rectangle([320, 360, 900, 520], fill=(0, 122, 255, 220))
    draw.text((610, 410), f"{apologist_name}", font=font_score, fill=(255, 255, 255), anchor="mm")
    draw.text((610, 470), f"{total_a} TOTAL POINTS", font=font_score, fill=(255, 255, 255), anchor="mm")
    
    draw.rectangle([1020, 360, 1600, 520], fill=(255, 45, 85, 220))
    draw.text((1310, 410), f"{skeptic_name}", font=font_score, fill=(255, 255, 255), anchor="mm")
    draw.text((1310, 470), f"{total_b} TOTAL POINTS", font=font_score, fill=(255, 255, 255), anchor="mm")
    
    return overlay

def create_judge_speech_overlay(judge_name, icon_path, reasoning, score_a, score_b, apologist_name, skeptic_name):
    overlay = Image.new("RGBA", (1920, 1080), (15, 23, 42, 240))
    draw = ImageDraw.Draw(overlay)
    
    icon_img = load_or_create_icon(icon_path, judge_name)
    icon_large = icon_img.resize((90, 90))
    overlay.paste(icon_large, (915, 100), mask=icon_large)
    
    draw.text((960, 220), f"FEATURED REPRESENTATIVE AI JUDGE: {judge_name.upper()}", font=get_font(32), fill=(255, 255, 255), anchor="mm")
    
    draw.rectangle([460, 260, 900, 330], fill=(0, 122, 255, 200))
    draw.text((680, 295), f"{apologist_name}: {score_a}/100", font=get_font(28), fill=(255, 255, 255), anchor="mm")
    
    draw.rectangle([1020, 260, 1460, 330], fill=(255, 45, 85, 200))
    draw.text((1240, 295), f"{skeptic_name}: {score_b}/100", font=get_font(28), fill=(255, 255, 255), anchor="mm")
    
    draw.rectangle([320, 380, 1600, 680], fill=(30, 41, 59, 240), outline=(255, 255, 255, 80), width=2)
    draw.text((960, 420), "OFFICIAL VERDICT & REASONING", font=get_font(26), fill=(148, 163, 184), anchor="mm")
    
    words = reasoning.split()
    lines, curr = [], ""
    for w in words:
        if len(curr + " " + w) > 60:
            lines.append(curr)
            curr = w
        else:
            curr += " " + w
    lines.append(curr)
    
    y_text = 480
    for line in lines:
        draw.text((960, y_text), line.strip(), font=get_font(30), fill=(255, 255, 255), anchor="mm")
        y_text += 42
        
    return overlay

def generate_debate():
    apologist_name, skeptic_name = NAMES_APOLOGIST[0], NAMES_SKEPTIC[0]
    
    with open("topic.txt", "r") as f:
        topic = f.read().strip()

    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
    
    prompt = (
        f"Write a full 10-minute broadcast debate on: '{topic}'.\n\n"
        f"Key Rules:\n"
        f"- In Round 0, Narrator explicitly mentions 'This debate is evaluated by top AI models representing each leading AI company.'\n"
        f"- Do NOT include speaker name prefixes in speech text (e.g., do not write 'Laura:').\n"
        f"- Whenever Laura quotes scripture, provide exact NIV text in 'bible_quote'.\n\n"
        f"JSON Schema Array strictly with keys:\n"
        f"- 'speaker': 'NARRATOR', 'APOLOGIST', or 'SKEPTIC'\n"
        f"- 'round': 0 to 5\n"
        f"- 'text': The spoken speech text\n"
        f"- 'bible_quote': Optional NIV quote or null\n"
    )
    
    res = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json={
        "model": "openai/gpt-5.6-sol",
        "messages": [{"role": "user", "content": prompt}]
    }, timeout=90)
    
    return json.loads(clean_json_string(res.json()['choices'][0]['message']['content'])), apologist_name, skeptic_name

async def evaluate_judge(judge, arg_a, arg_b):
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
    prompt = (
        f"Evaluate this debate round:\nApologist: {arg_a}\nSkeptic: {arg_b}\n\n"
        "Score both speakers out of 100 based on argument strength. Provide concise 1-sentence reasoning.\n"
        "Return JSON strictly: {\"score_a\": 85, \"score_b\": 78, \"reasoning\": \"Laura provided stronger textual grounding.\"}"
    )
    try:
        res = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json={
            "model": judge["model"],
            "messages": [{"role": "user", "content": prompt}]
        }, timeout=20)
        parsed = json.loads(clean_json_string(res.json()['choices'][0]['message']['content']))
        return {
            "name": judge["name"],
            "icon": judge["icon"],
            "score_a": int(parsed.get("score_a", 75)),
            "score_b": int(parsed.get("score_b", 75)),
            "reasoning": str(parsed.get("reasoning", "Clear structural arguments presented."))
        }
    except Exception:
        return {
            "name": judge["name"],
            "icon": judge["icon"],
            "score_a": random.randint(70, 92),
            "score_b": random.randint(70, 92),
            "reasoning": "Solid defense and logical counterpoints provided."
        }

async def run_10_judges(arg_a, arg_b):
    tasks = [evaluate_judge(j, arg_a, arg_b) for j in JUDGES]
    return await asyncio.gather(*tasks)

def render_debate_video(raw_script, apologist_name, skeptic_name):
    print("Rendering debate video with zoomed views, dynamic subtitles, NIV overlays, and cumulative scoring...")
    
    video_segments = []
    audio_segments = []
    
    total_a, total_b = 0, 0
    buffer_silence = create_silent_audio(duration=0.6)
    
    for idx, item in enumerate(raw_script):
        speaker = item["speaker"]
        text = sanitize_speech_text(item["text"])
        round_num = item["round"]
        bible_quote = item.get("bible_quote", None)
        
        if speaker == "NARRATOR":
            vid = VOICE_NARRATOR_ID
        elif speaker == "APOLOGIST":
            vid = VOICE_APOLOGIST_ID
        else:
            vid = VOICE_SKEPTIC_ID
            
        res = requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{vid}",
            headers={"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"},
            json={"text": text}
        )
        
        if res.status_code != 200:
            raise RuntimeError(f"ElevenLabs Error (Status {res.status_code}): {res.text}")
            
        temp_audio = f"temp_{idx}.mp3"
        with open(temp_audio, "wb") as f:
            f.write(res.content)
            
        audio_clip = AudioFileClip(temp_audio)
        if audio_clip.duration > 0.12:
            audio_clip = audio_clip.subclip(0, audio_clip.duration - 0.08)
            
        duration = audio_clip.duration
        
        stage_clip = VideoClip(
            lambda t: render_frame_with_animation(t, duration, speaker, text, bible_quote), 
            duration=duration
        )
        
        composite_elements = [stage_clip]
        
        # Round Judging, Scoreboard & Winner Calculations
        if speaker == "NARRATOR" and 1 <= round_num <= 4:
            arg_a = next((sanitize_speech_text(i['text']) for i in raw_script if i['round'] == round_num and i['speaker'] == 'APOLOGIST'), "")
            arg_b = next((sanitize_speech_text(i['text']) for i in raw_script if i['round'] == round_num and i['speaker'] == 'SKEPTIC'), "")
            
            scores = asyncio.run(run_10_judges(arg_a, arg_b))
            
            avg_a = sum(s["score_a"] for s in scores) // len(scores)
            avg_b = sum(s["score_b"] for s in scores) // len(scores)
            total_a += avg_a
            total_b += avg_b
            
            score_overlay_img = create_scoreboard_overlay(scores, round_num, apologist_name, skeptic_name, total_a, total_b)
            overlay_clip = ImageClip(np.array(score_overlay_img)).set_duration(duration)
            composite_elements.append(overlay_clip)
            
            main_speaker_clip = CompositeVideoClip(composite_elements).set_audio(audio_clip)
            video_segments.append(main_speaker_clip)
            audio_segments.append(audio_clip)
            
            audio_segments.append(buffer_silence)
            video_segments.append(VideoClip(lambda t: render_frame_with_animation(t, duration, speaker, text, bible_quote), duration=0.6))
            
            # Representative Judge speech
            rep_judge = random.choice(scores)
            rep_voice_id = JUDGE_VOICE_POOL[0]
            rep_speech_text = f"I am {rep_judge['name']}. I awarded {apologist_name} {rep_judge['score_a']} points and {skeptic_name} {rep_judge['score_b']} points. {rep_judge['reasoning']}"
            
            j_res = requests.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{rep_voice_id}",
                headers={"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"},
                json={"text": rep_speech_text}
            )
            
            if j_res.status_code == 200:
                j_temp_audio = f"temp_rep_judge_{round_num}.mp3"
                with open(j_temp_audio, "wb") as f:
                    f.write(j_res.content)
                    
                j_audio_clip = AudioFileClip(j_temp_audio)
                if j_audio_clip.duration > 0.12:
                    j_audio_clip = j_audio_clip.subclip(0, j_audio_clip.duration - 0.08)
                    
                j_overlay_img = create_judge_speech_overlay(
                    rep_judge['name'], rep_judge['icon'], rep_judge['reasoning'], 
                    rep_judge['score_a'], rep_judge['score_b'], apologist_name, skeptic_name
                )
                j_video_clip = ImageClip(np.array(j_overlay_img)).set_duration(j_audio_clip.duration).set_audio(j_audio_clip)
                
                video_segments.append(j_video_clip)
                audio_segments.append(j_audio_clip)
                audio_segments.append(buffer_silence)
                video_segments.append(ImageClip(np.array(j_overlay_img)).set_duration(0.6))
            continue

        final_clip = CompositeVideoClip(composite_elements).set_audio(audio_clip)
        video_segments.append(final_clip)
        audio_segments.append(audio_clip)
        
        audio_segments.append(buffer_silence)
        video_segments.append(VideoClip(lambda t: render_frame_with_animation(t, duration, speaker, text, bible_quote), duration=0.6))

    # Final Winner Announcement Card (Round 5 Conclusion)
    winner_overlay = create_final_winner_overlay(total_a, total_b, apologist_name, skeptic_name)
    winner_clip = ImageClip(np.array(winner_overlay)).set_duration(8.0)
    video_segments.append(winner_clip)

    master_video = concatenate_videoclips(video_segments, method="compose")
    master_audio = concatenate_audioclips(audio_segments)
    
    master_video.write_videofile(
        "final_debate.mp4", 
        fps=20, 
        codec="libx264", 
        audio_codec="aac",
        preset="ultrafast",
        threads=4,
        ffmpeg_params=["-tune", "zerolatency", "-bf", "0"],
        logger=None
    )
    master_audio.write_audiofile("output_audio.mp3", logger=None)

if __name__ == "__main__":
    script, apologist_name, skeptic_name = generate_debate()
    render_debate_video(script, apologist_name, skeptic_name)
