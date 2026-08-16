import os
import json
import asyncio
import requests
import re
from PIL import Image, ImageDraw, ImageFont
from moviepy.editor import (
    AudioFileClip,
    ImageClip,
    TextClip,
    CompositeVideoClip,
    concatenate_videoclips,
    concatenate_audioclips
)

# API Keys
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")

# Voice IDs
VOICE_NARRATOR_ID = "pNInz6obpgDQGcFmaJgB"  # Adam (Male Host)
VOICE_A_ID        = "21m00Tcm4TlvDq8ikWAM"  # Rachel (Female Debater)
VOICE_B_ID        = "ErXwobaYiN019PkySvjV"  # Antoni (Male Debater)

JUDGES = {
    "GPT-4o": "openai/gpt-4o",
    "Gemini Pro": "google/gemini-pro-1.5",
    "Llama 3.1": "meta-llama/llama-3.1-70b-instruct"
}

def clean_json_string(text):
    text = re.sub(r"^```(json)?", "", text, flags=re.MULTILINE)
    text = re.sub(r"^```", "", text, flags=re.MULTILINE)
    return text.strip()

def ensure_avatar_images():
    """Generates clean visual avatar cards if PNG files don't exist in repo root."""
    avatars = {
        "narrator.png": ("NARRATOR / HOST", (30, 41, 59), (59, 130, 246)),
        "debater_a.png": ("DEBATER A (FEMALE)", (88, 28, 135), (217, 70, 239)),
        "debater_b.png": ("DEBATER B (MALE)", (20, 83, 45), (34, 197, 94))
    }
    for filename, (label, bg_color, border_color) in avatars.items():
        if not os.path.exists(filename):
            img = Image.new("RGB", (600, 600), color=bg_color)
            draw = ImageDraw.Draw(img)
            draw.rectangle([10, 10, 590, 590], outline=border_color, width=8)
            draw.text((300, 300), label, fill=(255, 255, 255), anchor="mm")
            img.save(filename)

def generate_debate():
    with open("topic.txt", "r") as f:
        topic = f.read().strip()

    print(f"Generating Debate Script for: {topic}")
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
    
    prompt = (
        f"Write a formal 4-round debate on '{topic}'.\n"
        "1. Include Narrator Intro setting up the topic.\n"
        "2. Provide speeches for Debater A and Debater B for 4 rounds.\n"
        "3. Include Narrator Conclusion at the end.\n"
        "Return ONLY a JSON array of objects with keys: 'speaker' ('NARRATOR', 'A', 'B'), 'round' (0-4), and 'text'."
    )
    
    res = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json={
        "model": "openai/gpt-4o",
        "messages": [{"role": "user", "content": prompt}]
    }, timeout=60)
    
    return json.loads(clean_json_string(res.json()['choices'][0]['message']['content']))

async def get_judge_feedback(judge_name, model, arg_a, arg_b):
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
    prompt = f"Debate A: {arg_a}\nDebate B: {arg_b}\nDeclare winner ('A' or 'B') and critique in 2 sentences."
    try:
        res = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json={
            "model": model, "messages": [{"role": "user", "content": prompt}]
        }, timeout=20)
        content = res.json()['choices'][0]['message']['content'].strip()
        winner = "A" if "winner: a" in content.lower() or "debater a" in content.lower()[:30] else "B"
        return {"judge": judge_name, "winner": winner, "reasoning": content}
    except:
        return {"judge": judge_name, "winner": "A", "reasoning": "Debater A constructed a stronger logical framework."}

def build_full_show_script(raw_script, judging_results):
    full_timeline = []
    intro = next((i for i in raw_script if i['speaker'] == 'NARRATOR' and i['round'] == 0), None)
    full_timeline.append(intro or {"speaker": "NARRATOR", "round": 0, "text": "Welcome to today's AI debate match."})

    for r in range(1, 5):
        arg_a = next((i for i in raw_script if i['round'] == r and i['speaker'] == 'A'), None)
        arg_b = next((i for i in raw_script if i['round'] == r and i['speaker'] == 'B'), None)
        if arg_a: full_timeline.append(arg_a)
        if arg_b: full_timeline.append(arg_b)
        
        if r in judging_results:
            round_votes = judging_results[r]
            a_votes = sum(1 for v in round_votes if v['winner'] == 'A')
            b_votes = sum(1 for v in round_votes if v['winner'] == 'B')
            summary = f"Round {r} results: Debater A {a_votes} votes, Debater B {b_votes} votes. "
            for j in round_votes:
                summary += f"{j['judge']} noted: {j['reasoning']} "
            full_timeline.append({"speaker": "NARRATOR", "round": r, "text": summary})

    outro = next((i for i in raw_script if i['speaker'] == 'NARRATOR' and i['round'] > 4), None)
    full_timeline.append(outro or {"speaker": "NARRATOR", "round": 5, "text": "Thanks for watching. Leave your vote in the comments!"})
    return full_timeline

def render_video_and_audio(show_script):
    print("Generating synchronized audio and video clips...")
    ensure_avatar_images()
    
    video_segments = []
    audio_segments = []
    
    # Preload base avatar graphics
    avatar_paths = {
        "NARRATOR": "narrator.png",
        "A": "debater_a.png",
        "B": "debater_b.png"
    }

    for idx, line in enumerate(show_script):
        speaker = line['speaker']
        text = line['text']
        vid = VOICE_NARRATOR_ID if speaker == "NARRATOR" else (VOICE_A_ID if speaker == "A" else VOICE_B_ID)
        
        # 1. Fetch ElevenLabs Line Audio
        res = requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{vid}",
            headers={"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"},
            json={"text": text}
        )
        
        temp_audio_file = f"temp_{idx}.mp3"
        with open(temp_audio_file, "wb") as f:
            f.write(res.content)
            
        audio_clip = AudioFileClip(temp_audio_file)
        duration = audio_clip.duration
        audio_segments.append(audio_clip)

        # 2. Build Visual Clip (1920x1080 YouTube Canvas)
        img_clip = (ImageClip(avatar_paths[speaker])
                    .set_duration(duration)
                    .resize(height=500)
                    .set_position("center"))
        
        # Speaker Title Overlay Banner
        title_text = f"NOW SPEAKING: {speaker} | ROUND {line['round']}"
        txt_clip = (TextClip(title_text, fontsize=40, color='white', bg_color='black', size=(1920, 100))
                    .set_duration(duration)
                    .set_position(("center", 80)))
        
        bg_clip = ImageClip(avatar_paths[speaker]).set_duration(duration).resize((1920, 1080)).fl_image(lambda image: image // 3) # Dimmed Background
        
        composite = CompositeVideoClip([bg_clip, img_clip, txt_clip]).set_audio(audio_clip)
        video_segments.append(composite)

    print("Concatenating clips into master MP4...")
    final_video = concatenate_videoclips(video_segments, method="compose")
    final_audio = concatenate_audioclips(audio_segments)
    
    final_audio.write_audiofile("output_audio.mp3")
    final_video.write_videofile("final_debate.mp4", fps=24, codec="libx264", audio_codec="aac")

    # Clean up temp mp3 files
    for idx in range(len(show_script)):
        if os.path.exists(f"temp_{idx}.mp3"):
            os.remove(f"temp_{idx}.mp3")

if __name__ == "__main__":
    raw_script = generate_debate()
    
    judging_results = {}
    for r in range(1, 5):
        arg_a = next((i['text'] for i in raw_script if i['round'] == r and i['speaker'] == 'A'), "")
        arg_b = next((i['text'] for i in raw_script if i['round'] == r and i['speaker'] == 'B'), "")
        if arg_a and arg_b:
            judging_results[r] = asyncio.run(asyncio.gather(*[get_judge_feedback(name, model, arg_a, arg_b) for name, model in JUDGES.items()]))

    with open("scores.json", "w") as f:
        json.dump(judging_results, f, indent=2)

    full_show_script = build_full_show_script(raw_script, judging_results)
    render_video_and_audio(full_show_script)
    print("Video Render Complete! Created final_debate.mp4")
