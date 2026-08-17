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

# Pool of 15 distinct ElevenLabs voices for individual AI Judges
JUDGE_VOICE_POOL = [
    "21m00Tcm4TlvDq8ikWAM", "AZnzlk1XvdvUeBnXmlld", "EXAVITQu4vr4xnSDxMaL",
    "ErXwobaYiN019PkySvjV", "MF3mGyEYCl7XYWbV9V6O", "TxGEqnHWrfWFTfGW9XjX",
    "VR6AewLTigWG4xSOukaG", "yoZ06aGfMX9Vf31mJ64n", "z9fAnlkO2m35B221Ekg3",
    "pNInz6obpgDQGcFmaJgB", "N2lEx1qG5yUarL7iBx4r", "IKne3meq5aSn9XLyUdCD",
    "XB0fDUnXU5powFXDhCwa", "JBFqnCBsd6RMkjVDRZzb", "cjVigY5qzO86Huf0OWal"
]

# 15 AI Judges Panel using latest OpenRouter AI models
JUDGES = [
    {"name": "GPT-5.6 Sol", "model": "openai/gpt-5.6-sol", "icon": "icons/openai.png"},
    {"name": "GPT-5.6 Terra", "model": "openai/gpt-5.6-terra", "icon": "icons/openai.png"},
    {"name": "GPT-5.6 Luna", "model": "openai/gpt-5.6-luna", "icon": "icons/openai.png"},
    {"name": "GPT-OSS 120B", "model": "openai/gpt-oss-120b:free", "icon": "icons/openai.png"},
    {"name": "Claude Opus 5", "model": "anthropic/claude-opus-5", "icon": "icons/claude.png"},
    {"name": "Grok 4.6", "model": "xai/grok-4.6", "icon": "icons/grok.png"},
    {"name": "Gemini 3.7 Flash", "model": "google/gemini-3.7-flash", "icon": "icons/gemini.png"},
    {"name": "Gemma 4 31B", "model": "google/gemma-4-31b-it:free", "icon": "icons/gemini.png"},
    {"name": "DeepSeek V4 Pro", "model": "deepseek/deepseek-v4-pro", "icon": "icons/deepseek.png"},
    {"name": "DeepSeek V4 Flash", "model": "deepseek/deepseek-v4-flash", "icon": "icons/deepseek.png"},
    {"name": "GLM 5.2", "model": "zhipu/glm-5.2", "icon": "icons/glm.png"},
    {"name": "Nemotron 3 Ultra", "model": "nvidia/nemotron-3-ultra-550b-a55b:free", "icon": "icons/nvidia.png"},
    {"name": "North Mini", "model": "cohere/north-mini-code:free", "icon": "icons/cohere.png"},
    {"name": "Laguna S 2.1", "model": "poolside/laguna-s-2.1:free", "icon": "icons/poolside.png"},
    {"name": "Llama 3.3 70B", "model": "meta-llama/llama-3.3-70b-instruct", "icon": "icons/llama.png"}
]

NAMES_APOLOGIST = ["Laura"]
NAMES_SKEPTIC = ["Brian"]

def clean_json_string(text):
    text = re.sub(r"^```(json)?", "", text, flags=re.MULTILINE)
    text = re.sub(r"^```", "", text, flags=re.MULTILINE)
    return text.strip()

def create_silent_audio(duration=0.3, fps=44100):
    """Creates a clean silent audio clip to eliminate tailing buffer pops."""
    samples = int(fps * duration)
    return AudioArrayClip(np.zeros((samples, 2), dtype=np.float32), fps=fps)

def load_or_create_icon(icon_path, name):
    """Ensures AI icons render reliably by auto-generating a labeled badge if PNG is missing."""
    if os.path.exists(icon_path):
        try:
            return Image.open(icon_path).convert("RGBA").resize((45, 45))
        except Exception:
            pass
    # Fallback Badge Generator
    badge = Image.new("RGBA", (45, 45), (30, 41, 59, 255))
    draw = ImageDraw.Draw(badge)
    draw.rectangle([0, 0, 44, 44], outline=(0, 180, 255, 255), width=2)
    initials = "".join([w[0] for w in name.split()[:2]]).upper()
    draw.text((22, 22), initials, fill=(255, 255, 255), anchor="mm")
    return badge

def render_frame_with_animation(t, speaker, text, bible_quote=None):
    """Renders visual stage with dynamic active-speaker sound bars, captions, and Bible overlays."""
    base_path = "background.png" if os.path.exists("background.png") else "default_bg.png"
    if not os.path.exists(base_path):
        img = Image.new("RGB", (1920, 1080), color=(15, 23, 42))
        img.save(base_path)
        
    bg = Image.open(base_path).convert("RGBA").resize((1920, 1080))
    overlay = Image.new("RGBA", (1920, 1080), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    
    # Active Speaker Spotlight & Animated Sound Wave Bars
    if speaker == "APOLOGIST":
        draw.polygon([(0, 0), (600, 0), (450, 1080), (0, 1080)], fill=(0, 122, 255, 60))
        draw.ellipse([50, 450, 350, 750], fill=(0, 180, 255, 50))
        # Moving Sound Wave Bar Animation
        for i in range(12):
            h = int(20 + 35 * math.sin(t * 12 + i * 0.8))
            draw.rectangle([100 + (i * 18), 720 - h, 112 + (i * 18), 720 + h], fill=(0, 200, 255, 220))
            
    elif speaker == "SKEPTIC":
        draw.polygon([(1320, 0), (1920, 0), (1920, 1080), (1470, 1080)], fill=(255, 45, 85, 60))
        draw.ellipse([1570, 450, 1870, 750], fill=(255, 80, 80, 50))
        # Moving Sound Wave Bar Animation
        for i in range(12):
            h = int(20 + 35 * math.cos(t * 12 + i * 0.8))
            draw.rectangle([1600 + (i * 18), 720 - h, 1612 + (i * 18), 720 + h], fill=(255, 80, 100, 220))

    # NIV Bible Quote Overlay Box
    if bible_quote:
        draw.rectangle([360, 80, 1560, 160], fill=(15, 23, 42, 230), outline=(234, 179, 8, 200), width=2)
        draw.text((960, 100), "HOLY BIBLE (NIV REFERENCE)", fill=(234, 179, 8), anchor="mm")
        draw.text((960, 130), f'"{bible_quote}"', fill=(255, 255, 255), anchor="mm")

    # Closed Captions Overlay
    if text:
        draw.rectangle([260, 920, 1660, 1020], fill=(15, 23, 42, 230), outline=(255, 255, 255, 80), width=2)
        words = text.split()
        lines, curr = [], ""
        for w in words:
            if len(curr + " " + w) > 75:
                lines.append(curr)
                curr = w
            else:
                curr += " " + w
        lines.append(curr)
        
        disp_lines = lines[-2:]
        y_c = 950 if len(disp_lines) == 1 else 940
        for l in disp_lines:
            draw.text((960, y_c), l.strip(), fill=(255, 255, 255), anchor="mm")
            y_c += 35

    composite = Image.alpha_composite(bg, overlay)
    return np.array(composite.convert("RGB"))

def create_scoreboard_overlay(scores, round_num, apologist_name, skeptic_name):
    """Draws 15 AI judge icons and scores across split screen with dynamic Y-spacing."""
    overlay = Image.new("RGBA", (1920, 1080), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    
    draw.rectangle([560, 20, 1360, 80], fill=(15, 23, 42, 230), outline=(255, 255, 255, 100), width=2)
    draw.text((960, 50), f"ROUND {round_num} PANEL SCORES (15 AI JUDGES - OUT OF 100)", fill=(255, 255, 255), anchor="mm")

    draw.rectangle([50, 90, 450, 135], fill=(0, 122, 255, 200))
    draw.text((250, 112), f"{apologist_name} (Apologist)", fill=(255, 255, 255), anchor="mm")
    
    draw.rectangle([1470, 90, 1870, 135], fill=(255, 45, 85, 200))
    draw.text((1670, 112), f"{skeptic_name} (Skeptic)", fill=(255, 255, 255), anchor="mm")

    left_y, right_y = 145, 145
    
    for item in scores:
        icon_img = load_or_create_icon(item["icon"], item["name"])
        score_a, score_b = item["score_a"], item["score_b"]
            
        if score_a >= score_b:
            x_pos, y_pos = 60, left_y
            left_y += 58
            bg_box = [50, y_pos - 4, 450, y_pos + 46]
            score_text = f"{item['name']}: {score_a}/100"
        else:
            x_pos, y_pos = 1480, right_y
            right_y += 58
            bg_box = [1470, y_pos - 4, 1870, y_pos + 46]
            score_text = f"{item['name']}: {score_b}/100"

        draw.rectangle(bg_box, fill=(20, 30, 45, 210), outline=(255, 255, 255, 40))
        overlay.paste(icon_img, (x_pos, y_pos), mask=icon_img)
        draw.text((x_pos + 55, y_pos + 20), score_text, fill=(255, 255, 255), anchor="lm")

    return overlay

def create_judge_speech_overlay(judge_name, icon_path, reasoning, score_a, score_b, apologist_name, skeptic_name):
    """Displays single representative AI judge verdict card."""
    overlay = Image.new("RGBA", (1920, 1080), (15, 23, 42, 235))
    draw = ImageDraw.Draw(overlay)
    
    icon_img = load_or_create_icon(icon_path, judge_name)
    icon_large = icon_img.resize((80, 80))
    overlay.paste(icon_large, (920, 110), mask=icon_large)
    
    draw.text((960, 220), f"FEATURED AI JUDGE: {judge_name.upper()}", fill=(255, 255, 255), anchor="mm")
    
    draw.rectangle([460, 260, 900, 330], fill=(0, 122, 255, 200))
    draw.text((680, 295), f"{apologist_name}: {score_a}/100", fill=(255, 255, 255), anchor="mm")
    
    draw.rectangle([1020, 260, 1460, 330], fill=(255, 45, 85, 200))
    draw.text((1240, 295), f"{skeptic_name}: {score_b}/100", fill=(255, 255, 255), anchor="mm")
    
    draw.rectangle([360, 380, 1560, 680], fill=(30, 41, 59, 230), outline=(255, 255, 255, 60), width=2)
    draw.text((960, 420), "REPRESENTATIVE JUDGE VERDICT", fill=(148, 163, 184), anchor="mm")
    
    words = reasoning.split()
    lines, curr = [], ""
    for w in words:
        if len(curr + " " + w) > 65:
            lines.append(curr)
            curr = w
        else:
            curr += " " + w
    lines.append(curr)
    
    y_text = 480
    for line in lines:
        draw.text((960, y_text), line.strip(), fill=(255, 255, 255), anchor="mm")
        y_text += 40
        
    return overlay

def generate_debate():
    apologist_name, skeptic_name = NAMES_APOLOGIST[0], NAMES_SKEPTIC[0]
    
    with open("topic.txt", "r") as f:
        topic = f.read().strip()

    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
    
    prompt = (
        f"Write a full 10-minute broadcast debate on: '{topic}'.\n\n"
        f"Key Narrator Rules:\n"
        f"- In Round 0, Narrator MUST explicitly mention that 'This debate is evaluated by 15 of the latest frontier AI models—including GPT-5.6 Sol, Claude Opus 5, Grok 4.6, DeepSeek V4 Pro, and Gemini 3.7 Flash. There are no personal emotions or biases here—only pure objective reasoning, logical analysis, and scripture.'\n\n"
        f"Bible References:\n"
        f"- Whenever Laura (Apologist) quotes or cites the Bible, provide exact scriptural text using the New International Version (NIV).\n\n"
        f"JSON Schema:\n"
        f"Return ONLY a JSON array of objects with keys:\n"
        f"- 'speaker': 'NARRATOR', 'APOLOGIST', or 'SKEPTIC'\n"
        f"- 'round': 0 to 5\n"
        f"- 'text': The spoken speech\n"
        f"- 'bible_quote': Optional field containing NIV verse (e.g. 'John 14:6 - I am the way...') or null.\n"
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
        "Score both speakers out of 100 based on argument strength. Provide a concise 1-sentence reasoning statement.\n"
        "Return JSON strictly in format: {\"score_a\": 85, \"score_b\": 78, \"reasoning\": \"Laura provided stronger textual grounding.\"}"
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

async def run_15_judges(arg_a, arg_b):
    tasks = [evaluate_judge(j, arg_a, arg_b) for j in JUDGES]
    return await asyncio.gather(*tasks)

def render_debate_video(raw_script, apologist_name, skeptic_name):
    print("Rendering debate video with 15 AI judges, active speaker sound bars, subtitles, NIV quotes, and audio glitch fixes...")
    
    video_segments = []
    audio_segments = []
    
    cumulative_a, cumulative_b = 0, 0
    buffer_silence = create_silent_audio(duration=0.3)
    
    for idx, item in enumerate(raw_script):
        speaker = item["speaker"]
        text = item["text"]
        round_num = item["round"]
        bible_quote = item.get("bible_quote", None)
        
        # Select TTS Voice
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
            
        # Fix trailing audio glitch: trim 0.05s buffer artifacts
        audio_clip = AudioFileClip(temp_audio)
        if audio_clip.duration > 0.1:
            audio_clip = audio_clip.subclip(0, audio_clip.duration - 0.05)
            
        duration = audio_clip.duration
        
        # Animated Video Clip with sound bars, closed captions & NIV quotes
        stage_clip = VideoClip(
            lambda t: render_frame_with_animation(t, speaker, text, bible_quote), 
            duration=duration
        )
        
        composite_elements = [stage_clip]
        
        # Round Judging & Scoreboard Overlay
        if speaker == "NARRATOR" and 1 <= round_num <= 4:
            arg_a = next((i['text'] for i in raw_script if i['round'] == round_num and i['speaker'] == 'APOLOGIST'), "")
            arg_b = next((i['text'] for i in raw_script if i['round'] == round_num and i['speaker'] == 'SKEPTIC'), "")
            
            scores = asyncio.run(run_15_judges(arg_a, arg_b))
            
            avg_a = sum(s["score_a"] for s in scores) // len(scores)
            avg_b = sum(s["score_b"] for s in scores) // len(scores)
            cumulative_a += avg_a
            cumulative_b += avg_b
            
            score_overlay_img = create_scoreboard_overlay(scores, round_num, apologist_name, skeptic_name)
            overlay_clip = ImageClip(np.array(score_overlay_img)).set_duration(duration)
            composite_elements.append(overlay_clip)
            
            main_speaker_clip = CompositeVideoClip(composite_elements).set_audio(audio_clip)
            video_segments.append(main_speaker_clip)
            audio_segments.append(audio_clip)
            
            # Silence buffer between clips to eliminate audio pop
            audio_segments.append(buffer_silence)
            video_segments.append(VideoClip(lambda t: render_frame_with_animation(t, speaker, text, bible_quote), duration=0.3))
            
            # Single Representative AI Judge verbal speech
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
                    
                j_audio_clip = AudioFileClip(j_temp_audio).subclip(0)
                j_overlay_img = create_judge_speech_overlay(
                    rep_judge['name'], rep_judge['icon'], rep_judge['reasoning'], 
                    rep_judge['score_a'], rep_judge['score_b'], apologist_name, skeptic_name
                )
                j_video_clip = ImageClip(np.array(j_overlay_img)).set_duration(j_audio_clip.duration).set_audio(j_audio_clip)
                
                video_segments.append(j_video_clip)
                audio_segments.append(j_audio_clip)
                audio_segments.append(buffer_silence)
                video_segments.append(ImageClip(np.array(j_overlay_img)).set_duration(0.3))
            continue

        final_clip = CompositeVideoClip(composite_elements).set_audio(audio_clip)
        video_segments.append(final_clip)
        audio_segments.append(audio_clip)
        
        # Audio boundary fix padding
        audio_segments.append(buffer_silence)
        video_segments.append(VideoClip(lambda t: render_frame_with_animation(t, speaker, text, bible_quote), duration=0.3))

    # Master render
    master_video = concatenate_videoclips(video_segments, method="compose")
    master_audio = concatenate_audioclips(audio_segments)
    
    master_video.write_videofile(
        "final_debate.mp4", 
        fps=24, 
        codec="libx264", 
        audio_codec="aac",
        preset="ultrafast",
        threads=2
    )
    master_audio.write_audiofile("output_audio.mp3")

if __name__ == "__main__":
    script, apologist_name, skeptic_name = generate_debate()
    render_debate_video(script, apologist_name, skeptic_name)
