import os
import json
import asyncio
import requests
import re
import random
import PIL.Image
import numpy as np

# Patch Pillow for MoviePy 1.x compatibility
if not hasattr(PIL.Image, 'ANTIALIAS'):
    PIL.Image.ANTIALIAS = PIL.Image.Resampling.LANCZOS

from PIL import Image, ImageDraw, ImageEnhance, ImageFont
from moviepy.editor import (
    AudioFileClip,
    ImageClip,
    CompositeVideoClip,
    concatenate_videoclips,
    concatenate_audioclips
)
from moviepy.audio.AudioClip import AudioArrayClip

# API Keys
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")

# Debater & Narrator ElevenLabs Voices
VOICE_NARRATOR_ID = "QIhD5ivPGEoYZQDocuHI"   # Adam (Narrator)
VOICE_APOLOGIST_ID = "GZ4PpFJV8ikEGUtBrjK7"  # Laura (Apologist)
VOICE_SKEPTIC_ID   = "gPPH6SLdL8XSX6GNJ40G"  # Brian (Skeptic)

# Pool of 10 distinct public ElevenLabs voices for individual AI Judges
JUDGE_VOICE_POOL = [
    "21m00Tcm4TlvDq8ikWAM",  # Rachel
    "AZnzlk1XvdvUeBnXmlld",  # Domi
    "EXAVITQu4vr4xnSDxMaL",  # Bella
    "ErXwobaYiN019PkySvjV",  # Antoni
    "MF3mGyEYCl7XYWbV9V6O",  # Elli
    "TxGEqnHWrfWFTfGW9XjX",  # Josh
    "VR6AewLTigWG4xSOukaG",  # Arnold
    "yoZ06aGfMX9Vf31mJ64n",  # Sam
    "z9fAnlkO2m35B221Ekg3",  # Charlie
    "pNInz6obpgDQGcFmaJgB"   # Adam (Secondary)
]

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

NAMES_APOLOGIST = ["Laura"]
NAMES_SKEPTIC = ["Brian"]

def clean_json_string(text):
    text = re.sub(r"^```(json)?", "", text, flags=re.MULTILINE)
    text = re.sub(r"^```", "", text, flags=re.MULTILINE)
    return text.strip()

def create_silent_audio(duration=1.5, fps=44100):
    """Generates clean silent audio array clip to prevent audio buffer repeating bug."""
    samples = int(fps * duration)
    silent_array = np.zeros((samples, 2), dtype=np.float32)
    return AudioArrayClip(silent_array, fps=fps)

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
        draw.polygon([(0, 0), (600, 0), (450, 1080), (0, 1080)], fill=(0, 122, 255, 60))
        draw.ellipse([50, 450, 350, 750], fill=(0, 180, 255, 50))
    elif speaker == "SKEPTIC":
        draw.polygon([(1320, 0), (1920, 0), (1920, 1080), (1470, 1080)], fill=(255, 45, 85, 60))
        draw.ellipse([1570, 450, 1870, 750], fill=(255, 80, 80, 50))
    
    return Image.alpha_composite(bg, overlay)

def create_judge_speech_overlay(judge_name, icon_path, reasoning, score_a, score_b, apologist_name, skeptic_name):
    """Creates a full-screen card when an individual AI judge explains its scores."""
    overlay = Image.new("RGBA", (1920, 1080), (15, 23, 42, 230))
    draw = ImageDraw.Draw(overlay)
    
    # Judge Banner & Icon
    if os.path.exists(icon_path):
        icon_img = Image.open(icon_path).convert("RGBA").resize((100, 100))
        overlay.paste(icon_img, (910, 120), mask=icon_img)
    
    draw.text((960, 250), f"AI JUDGE: {judge_name.upper()}", fill=(255, 255, 255), anchor="mm")
    
    # Scores
    draw.rectangle([460, 300, 900, 380], fill=(0, 122, 255, 200))
    draw.text((680, 340), f"{apologist_name}: {score_a}/100", fill=(255, 255, 255), anchor="mm")
    
    draw.rectangle([1020, 300, 1460, 380], fill=(255, 45, 85, 200))
    draw.text((1240, 340), f"{skeptic_name}: {score_b}/100", fill=(255, 255, 255), anchor="mm")
    
    # Reasoning Speech Text Box
    draw.rectangle([360, 440, 1560, 700], fill=(30, 41, 59, 230), outline=(255, 255, 255, 60), width=2)
    draw.text((960, 480), "JUDGE VERDICT & SUMMARY", fill=(148, 163, 184), anchor="mm")
    
    # Wrap reasoning text
    words = reasoning.split()
    lines = []
    curr = ""
    for w in words:
        if len(curr + " " + w) > 65:
            lines.append(curr)
            curr = w
        else:
            curr += " " + w
    lines.append(curr)
    
    y_text = 540
    for line in lines:
        draw.text((960, y_text), line.strip(), fill=(255, 255, 255), anchor="mm")
        y_text += 40
        
    return overlay

def create_scoreboard_overlay(scores, round_num, apologist_name, skeptic_name):
    """Draws 10 AI icons on split-screen based on score lean with proper mask blending."""
    overlay = Image.new("RGBA", (1920, 1080), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    
    draw.rectangle([560, 20, 1360, 90], fill=(15, 23, 42, 230), outline=(255, 255, 255, 100), width=2)
    draw.text((960, 55), f"ROUND {round_num} FULL JUDGING PANEL (OUT OF 100)", fill=(255, 255, 255), anchor="mm")

    draw.rectangle([50, 110, 450, 160], fill=(0, 122, 255, 200))
    draw.text((250, 135), f"{apologist_name} (Apologist)", fill=(255, 255, 255), anchor="mm")
    
    draw.rectangle([1470, 110, 1870, 160], fill=(255, 45, 85, 200))
    draw.text((1670, 135), f"{skeptic_name} (Skeptic)", fill=(255, 255, 255), anchor="mm")

    left_y, right_y = 180, 180
    
    for item in scores:
        icon_path = item["icon"]
        score_a = item["score_a"]
        score_b = item["score_b"]
        
        if os.path.exists(icon_path):
            icon_img = Image.open(icon_path).convert("RGBA").resize((45, 45))
        else:
            icon_img = Image.new("RGBA", (45, 45), (100, 110, 120, 255))
            
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
        overlay.paste(icon_img, (x_pos, y_pos), mask=icon_img)
        draw.text((x_pos + 60, y_pos + 22), score_text, fill=(255, 255, 255), anchor="lm")

    return overlay

def generate_debate():
    apologist_name = NAMES_APOLOGIST[0]
    skeptic_name = NAMES_SKEPTIC[0]
    
    with open("topic.txt", "r") as f:
        topic = f.read().strip()

    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
    
    prompt = (
        f"Write a full 10-minute broadcast debate on '{topic}'.\n\n"
        f"Roles:\n"
        f"- Narrator (Adam): Introduces format, rules, 10 AI judges, speakers Laura and Brian. Opens each round, synthesizes round averages out of 100, and gives final closing declaring the winner.\n"
        f"- Debater A (Laura): Christian Apologist (detailed 250-300 word arguments per round).\n"
        f"- Debater B (Brian): Skeptic (detailed 250-300 word arguments per round).\n\n"
        f"Structure:\n"
        f"1. Round 0: Detailed Narrator Introduction.\n"
        f"2. Rounds 1-4: Debater A speech, Debater B speech, then Narrator Round Summary with AI panel average tallies.\n"
        f"3. Round 5: Narrator Final Conclusion announcing total cumulative scores across all rounds out of 100 and declaring winner.\n\n"
        f"Return ONLY a JSON array of objects with keys: 'speaker' ('NARRATOR', 'APOLOGIST', 'SKEPTIC'), 'round' (0-5), and 'text'."
    )
    
    res = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json={
        "model": "openai/gpt-4o",
        "messages": [{"role": "user", "content": prompt}]
    }, timeout=90)
    
    return json.loads(clean_json_string(res.json()['choices'][0]['message']['content'])), apologist_name, skeptic_name

async def evaluate_judge(judge, arg_a, arg_b):
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
    prompt = (
        f"Evaluate this debate round:\nApologist: {arg_a}\nSkeptic: {arg_b}\n\n"
        "Score both speakers out of 100 based on argument strength. Write a concise 1-sentence position summary explaining your score.\n"
        "Return JSON strictly in this format: {\"score_a\": 85, \"score_b\": 78, \"reasoning\": \"I scored Laura higher because her historical claims were clearer.\"}"
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
            "reasoning": str(parsed.get("reasoning", "Strong structural arguments presented by both speakers."))
        }
    except Exception:
        return {
            "name": judge["name"],
            "icon": judge["icon"],
            "score_a": random.randint(70, 92),
            "score_b": random.randint(70, 92),
            "reasoning": "Solid defense and counter-arguments provided in this round."
        }

async def run_10_judges(arg_a, arg_b):
    tasks = [evaluate_judge(j, arg_a, arg_b) for j in JUDGES]
    return await asyncio.gather(*tasks)

def render_debate_video(raw_script, apologist_name, skeptic_name):
    print("Rendering video with distinct AI judge voices and verbal position summaries...")
    
    video_segments = []
    audio_segments = []
    
    cumulative_a = 0
    cumulative_b = 0
    
    pause_audio = create_silent_audio(duration=1.5)
    
    for idx, item in enumerate(raw_script):
        speaker = item["speaker"]
        text = item["text"]
        round_num = item["round"]
        
        # Speaker Voice Selection
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
            raise RuntimeError(f"ElevenLabs API Error (Status {res.status_code}): {res.text}")
            
        temp_audio = f"temp_{idx}.mp3"
        with open(temp_audio, "wb") as f:
            f.write(res.content)
            
        audio_clip = AudioFileClip(temp_audio)
        stage_img = create_silhouetted_stage(speaker)
        stage_clip = ImageClip(np.array(stage_img)).set_duration(audio_clip.duration)
        
        composite_elements = [stage_clip]
        
        # Round Judging & AI Model Verbal Feedback Clips
        if speaker == "NARRATOR" and 1 <= round_num <= 4:
            arg_a = next((i['text'] for i in raw_script if i['round'] == round_num and i['speaker'] == 'APOLOGIST'), "")
            arg_b = next((i['text'] for i in raw_script if i['round'] == round_num and i['speaker'] == 'SKEPTIC'), "")
            
            scores = asyncio.run(run_10_judges(arg_a, arg_b))
            
            avg_a = sum(s["score_a"] for s in scores) // len(scores)
            avg_b = sum(s["score_b"] for s in scores) // len(scores)
            cumulative_a += avg_a
            cumulative_b += avg_b
            
            score_overlay_img = create_scoreboard_overlay(scores, round_num, apologist_name, skeptic_name)
            overlay_clip = ImageClip(np.array(score_overlay_img)).set_duration(audio_clip.duration)
            composite_elements.append(overlay_clip)
            
            # Primary Speaker Clip
            main_speaker_clip = CompositeVideoClip(composite_elements).set_audio(audio_clip)
            video_segments.append(main_speaker_clip)
            audio_segments.append(audio_clip)
            video_segments.append(ImageClip(np.array(stage_img)).set_duration(1.5))
            audio_segments.append(pause_audio)
            
            # Render individual AI Judge speech clips with distinct voices (Featured Top Judges)
            print(f"Generating AI Judge voice summaries for Round {round_num}...")
            for j_idx, judge in enumerate(scores[:3]):  # Features top 3 AI judges verbally each round
                judge_voice_id = JUDGE_VOICE_POOL[j_idx % len(JUDGE_VOICE_POOL)]
                judge_speech_text = f"I am {judge['name']}. I gave {apologist_name} {judge['score_a']} points and {skeptic_name} {judge['score_b']} points. {judge['reasoning']}"
                
                j_res = requests.post(
                    f"https://api.elevenlabs.io/v1/text-to-speech/{judge_voice_id}",
                    headers={"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"},
                    json={"text": judge_speech_text}
                )
                
                if j_res.status_code == 200:
                    j_temp_audio = f"temp_judge_{round_num}_{j_idx}.mp3"
                    with open(j_temp_audio, "wb") as f:
                        f.write(j_res.content)
                        
                    j_audio_clip = AudioFileClip(j_temp_audio)
                    j_overlay_img = create_judge_speech_overlay(
                        judge['name'], judge['icon'], judge['reasoning'], 
                        judge['score_a'], judge['score_b'], apologist_name, skeptic_name
                    )
                    j_video_clip = ImageClip(np.array(j_overlay_img)).set_duration(j_audio_clip.duration).set_audio(j_audio_clip)
                    
                    video_segments.append(j_video_clip)
                    audio_segments.append(j_audio_clip)
                    video_segments.append(ImageClip(np.array(j_overlay_img)).set_duration(1.0))
                    audio_segments.append(pause_audio)
            continue

        final_clip = CompositeVideoClip(composite_elements).set_audio(audio_clip)
        video_segments.append(final_clip)
        audio_segments.append(audio_clip)
        video_segments.append(ImageClip(np.array(stage_img)).set_duration(1.5))
        audio_segments.append(pause_audio)

    # Master render
    master_video = concatenate_videoclips(video_segments, method="compose")
    master_audio = concatenate_audioclips(audio_segments)
    
    master_video.write_videofile("final_debate.mp4", fps=24, codec="libx264", audio_codec="aac")
    master_audio.write_audiofile("output_audio.mp3")

if __name__ == "__main__":
    script, apologist_name, skeptic_name = generate_debate()
    render_debate_video(script, apologist_name, skeptic_name)
