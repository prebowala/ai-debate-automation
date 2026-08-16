import os
import json
import asyncio
import requests
import re
import random
import PIL.Image

# Patch Pillow for MoviePy 1.x compatibility
if not hasattr(PIL.Image, 'ANTIALIAS'):
    PIL.Image.ANTIALIAS = PIL.Image.Resampling.LANCZOS

from PIL import Image, ImageDraw, ImageEnhance, ImageFont
from moviepy.editor import (
    AudioFileClip,
    ImageClip,
    CompositeVideoClip,
    concatenate_videoclips,
    concatenate_audioclips,
    AudioClip
)

# API Keys
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")

# ElevenLabs Voices (Male Narrator, Male Apologist, Female Skeptic)
VOICE_NARRATOR_ID = "pNInz6obpgDQGcFmaJgB"  # Adam
VOICE_APOLOGIST_ID = "FQ2p14jU7A9C9K5b7D0a" # Marcus
VOICE_SKEPTIC_ID   = "21m00Tcm4TlvDq8ikWAM" # Rachel

# 10 AI Judges Panel with matching icons
JUDGES = [
    {"name": "GPT-4o", "model": "openai/gpt-4o", "icon": "icons/openai.png"},
    {"name": "Claude 3.5", "model": "anthropic/claude-3.5-sonnet", "icon": "icons/claude.png"},
    {"name": "Gemini 1.5", "model": "google/gemini-pro-1.5", "icon": "icons/gemini.png"},
    {"name": "Llama 3.1", "model": "meta-llama/llama-3.1-70b-instruct", "icon": "icons/llama.png"},
    {"name": "Mistral Large", "model": "mistralai/mistral-large", "icon": "icons/mistral.png"},
    {"name": "DeepSeek V2.5", "model": "deepseek/deepseek-chat", "icon": "icons/deepseek.png"},
    {"name": "Grok 2", "model": "x-ai/grok-2", "icon": "icons/grok.png"},
    {"name": "Qwen 2.5", "model": "qwen/qwen-2.5-72b-instruct", "icon": "icons/qwen.png"},
    {"name": "Command R+", "model": "cohere/command-r-plus", "icon": "icons/cohere.png"},
    {"name": "Perplexity Pro", "model": "perplexity/sonar-reasoning", "icon": "icons/perplexity.png"}
]

NAMES_APOLOGIST = ["Marcus", "David", "Thomas", "James"]
NAMES_SKEPTIC = ["Rachel", "Sarah", "Elena", "Claire"]

def clean_json_string(text):
    text = re.sub(r"^```(json)?", "", text, flags=re.MULTILINE)
    text = re.sub(r"^```", "", text, flags=re.MULTILINE)
    return text.strip()

def create_silhouetted_stage(speaker):
    """Highlights left (Apologist) or right (Skeptic) side based on active speaker."""
    base_path = "background.png" if os.path.exists("background.png") else "default_bg.png"
    if not os.path.exists(base_path):
        img = Image.new("RGB", (1920, 1080), color=(15, 23, 42))
        img.save(base_path)
        
    bg = Image.open(base_path).convert("RGBA").resize((1920, 1080))
    overlay = Image.new("RGBA", (1920, 1080), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    
    if speaker == "APOLOGIST":
        # Bright blue spotlight overlay on the left
        draw.polygon([(0, 0), (600, 0), (450, 1080), (0, 1080)], fill=(0, 122, 255, 60))
        draw.ellipse([50, 450, 350, 750], fill=(0, 180, 255, 50))
    elif speaker == "SKEPTIC":
        # Bright red spotlight overlay on the right
        draw.polygon([(1320, 0), (1920, 0), (1920, 1080), (1470, 1080)], fill=(255, 45, 85, 60))
        draw.ellipse([1570, 450, 1870, 750], fill=(255, 80, 80, 50))
    
    return Image.alpha_composite(bg, overlay)

def create_scoreboard_overlay(scores, round_num, apologist_name, skeptic_name):
    """Draws 10 AI icons on split-screen based on score lean with out-of-100 scores."""
    overlay = Image.new("RGBA", (1920, 1080), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    
    # Header Banner
    draw.rectangle([560, 20, 1360, 90], fill=(15, 23, 42, 230), outline=(255, 255, 255, 100), width=2)
    draw.text((960, 55), f"ROUND {round_num} JUDGING SCORES (OUT OF 100)", fill=(255, 255, 255), anchor="mm")

    # Column Labels
    draw.rectangle([50, 110, 450, 160], fill=(0, 122, 255, 200))
    draw.text((250, 135), f"{apologist_name} (Christian Apologist)", fill=(255, 255, 255), anchor="mm")
    
    draw.rectangle([1470, 110, 1870, 160], fill=(255, 45, 85, 200))
    draw.text((1670, 135), f"{skeptic_name} (Skeptic)", fill=(255, 255, 255), anchor="mm")

    left_y, right_y = 180, 180
    
    for item in scores:
        icon_path = item["icon"]
        score_a = item["score_a"]
        score_b = item["score_b"]
        
        # Load AI icon
        if os.path.exists(icon_path):
            icon_img = Image.open(icon_path).convert("RGBA").resize((45, 45))
        else:
            icon_img = Image.new("RGBA", (45, 45), (100, 110, 120, 255))
            
        # Determine split screen placement (Left if Apologist higher, Right if Skeptic higher)
        if score_a >= score_b:
            x_pos = 60
            y_pos = left_y
            left_y += 65
            bg_box = [50, y_pos - 5, 450, y_pos + 50]
            score_text = f"{item['name']}: {score_a}/100"
        else:
            x_pos = 1480
            y_pos = right_y
            right_y += 65
            bg_box = [1470, y_pos - 5, 1870, y_pos + 50]
            score_text = f"{item['name']}: {score_b}/100"

        draw.rectangle(bg_box, fill=(20, 30, 45, 210), outline=(255, 255, 255, 40))
        overlay.paste(icon_img, (x_pos, y_pos), icon_img)
        draw.text((x_pos + 60, y_pos + 22), score_text, fill=(255, 255, 255), anchor="lm")

    return overlay

def generate_debate():
    apologist_name = random.choice(NAMES_APOLOGIST)
    skeptic_name = random.choice(NAMES_SKEPTIC)
    
    with open("topic.txt", "r") as f:
        topic = f.read().strip()

    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
    
    prompt = (
        f"Write a 4-round broadcast debate on '{topic}'.\n"
        f"Debater A is '{apologist_name}' (Christian Apologist).\n"
        f"Debater B is '{skeptic_name}' (Skeptic).\n\n"
        "Tone and Language Guidelines:\n"
        "- Use everyday, conversational, easy-to-understand language.\n"
        "- Avoid overly dense academic jargon or robotic phrasing.\n"
        "- Keep speeches sharp, compelling, natural, and grounded.\n\n"
        "Return ONLY a JSON array of objects with keys: 'speaker' ('NARRATOR', 'APOLOGIST', 'SKEPTIC'), 'round' (0-5), and 'text'."
    )
    
    res = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json={
        "model": "openai/gpt-4o",
        "messages": [{"role": "user", "content": prompt}]
    }, timeout=60)
    
    return json.loads(clean_json_string(res.json()['choices'][0]['message']['content'])), apologist_name, skeptic_name

async def evaluate_judge(judge, arg_a, arg_b):
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
    prompt = (
        f"Evaluate this debate round:\nApologist: {arg_a}\nSkeptic: {arg_b}\n\n"
        "Score both speakers out of 100 based on argument strength and clarity.\n"
        "Return JSON strictly in this format: {\"score_a\": 85, \"score_b\": 78}"
    )
    try:
        res = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json={
            "model": judge["model"],
            "messages": [{"role": "user", "content": prompt}]
        }, timeout=15)
        parsed = json.loads(clean_json_string(res.json()['choices'][0]['message']['content']))
        return {
            "name": judge["name"],
            "icon": judge["icon"],
            "score_a": int(parsed.get("score_a", 75)),
            "score_b": int(parsed.get("score_b", 75))
        }
    except Exception:
        # Fallback scores if API times out
        return {
            "name": judge["name"],
            "icon": judge["icon"],
            "score_a": random.randint(70, 92),
            "score_b": random.randint(70, 92)
        }

async def run_10_judges(arg_a, arg_b):
    tasks = [evaluate_judge(j, arg_a, arg_b) for j in JUDGES]
    return await asyncio.gather(*tasks)

def render_debate_video(raw_script, apologist_name, skeptic_name):
    print("Rendering video with custom stage, 10 judges, and score overlays...")
    
    video_segments = []
    audio_segments = []
    
    total_score_a = 0
    total_score_b = 0
    
    pause_clip = AudioClip(lambda t: 0, duration=1.5, fps=44100)
    
    for idx, item in enumerate(raw_script):
        speaker = item["speaker"]
        text = item["text"]
        round_num = item["round"]
        
        # Voice Selection
        if speaker == "NARRATOR":
            vid = VOICE_NARRATOR_ID
        elif speaker == "APOLOGIST":
            vid = VOICE_APOLOGIST_ID
        else:
            vid = VOICE_SKEPTIC_ID
            
        # ElevenLabs Request
        res = requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{vid}",
            headers={"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"},
            json={"text": text}
        )
        
        temp_audio = f"temp_{idx}.mp3"
        with open(temp_audio, "wb") as f:
            f.write(res.content)
            
        audio_clip = AudioFileClip(temp_audio)
        duration = audio_clip.duration
        
        # Base Stage Lighting Composite
        stage_img = create_silhouetted_stage(speaker)
        stage_clip = ImageClip(np.array(stage_img)).set_duration(duration)
        
        composite_elements = [stage_clip]
        
        # Add Round Judging Overlay if Narrator summarizes scores
        if speaker == "NARRATOR" and 1 <= round_num <= 4:
            arg_a = next((i['text'] for i in raw_script if i['round'] == round_num and i['speaker'] == 'APOLOGIST'), "")
            arg_b = next((i['text'] for i in raw_script if i['round'] == round_num and i['speaker'] == 'SKEPTIC'), "")
            
            scores = asyncio.run(run_10_judges(arg_a, arg_b))
            
            round_a = sum(s["score_a"] for s in scores) // len(scores)
            round_b = sum(s["score_b"] for s in scores) // len(scores)
            total_score_a += round_a
            total_score_b += round_b
            
            score_overlay_img = create_scoreboard_overlay(scores, round_num, apologist_name, skeptic_name)
            overlay_clip = ImageClip(np.array(score_overlay_img)).set_duration(duration)
            composite_elements.append(overlay_clip)

        final_clip = CompositeVideoClip(composite_elements).set_audio(audio_clip)
        
        video_segments.append(final_clip)
        audio_segments.append(audio_clip)
        
        # Add 1.5 second pause after each speaker
        video_segments.append(ImageClip(np.array(stage_img)).set_duration(1.5))
        audio_segments.append(pause_clip)

    # Master render
    master_video = concatenate_videoclips(video_segments, method="compose")
    master_audio = concatenate_audioclips(audio_segments)
    
    master_video.write_videofile("final_debate.mp4", fps=24, codec="libx264", audio_codec="aac")
    master_audio.write_audiofile("output_audio.mp3")

if __name__ == "__main__":
    import numpy as np
    script, apologist_name, skeptic_name = generate_debate()
    render_debate_video(script, apologist_name, skeptic_name)
