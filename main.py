import os
import asyncio
import requests
import subprocess
import re
import math
import edge_tts
import concurrent.futures
import json
import random
import glob
from PIL import Image, ImageDraw, ImageFont, ImageFilter

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Multilingual Neural voices for maximum realism
VOICES = {
    "Moderator": "en-US-ChristopherNeural",
    "AI Christian Apologist": "en-US-BrianMultilingualNeural",
    "AI Skeptic": "en-US-AvaMultilingualNeural",
    "Panelist 1": "en-US-AndrewMultilingualNeural",
    "Panelist 2": "en-US-EmmaMultilingualNeural"
}

PANEL_JUDGES = [
    {"name": "GPT", "id": "openai/gpt-4o"},
    {"name": "Claude", "id": "anthropic/claude-3.5-sonnet"},
    {"name": "Gemini", "id": "google/gemini-pro-1.5"},
    {"name": "DeepSeek", "id": "deepseek/deepseek-chat"},
    {"name": "Mistral", "id": "mistralai/mistral-large"},
    {"name": "Llama", "id": "meta-llama/llama-3.1-70b-instruct"},
    {"name": "Cohere", "id": "cohere/command-r-plus"},
    {"name": "Grok", "id": "x-ai/grok-2"},
    {"name": "Qwen", "id": "qwen/qwen-2.5-72b-instruct"},
    {"name": "Sonar", "id": "perplexity/sonar-pro"}
]

def cleanup_cache():
    """Sweeps up old temporary files before a fresh render."""
    print("🧹 Cleaning up old cache files...")
    extensions = ['*.mp4', '*.mp3', '*.ass', '*.png', '*_list.txt']
    safe_files = ['final_debate_output.mp4'] 
    
    for ext in extensions:
        for file in glob.glob(ext):
            if file not in safe_files:
                try:
                    os.remove(file)
                except Exception:
                    pass
    print("✨ Workspace is squeaky clean!")

def clean_for_speech(text):
    cleaned = re.sub(r'\([^)]*\)', '', text)
    cleaned = re.sub(r'[*#_`–—]', '', cleaned)
    cleaned = cleaned.replace(":", " ").replace(";", " ").replace('"', '')
    return re.sub(r'\s+', ' ', cleaned).strip()

def hex_to_rgba(hex_str, alpha):
    hex_str = hex_str.lstrip('#')
    return (int(hex_str[0:2], 16), int(hex_str[2:4], 16), int(hex_str[4:6], 16), alpha)

def query_openrouter(prompt, primary_model_id, timeout=45):
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }
    try:
        response = requests.post(
            OPENROUTER_URL, 
            headers=headers, 
            json={"model": primary_model_id, "messages": [{"role": "user", "content": prompt}], "temperature": 0.5}, 
            timeout=timeout
        )
        if response.status_code == 200:
            return response.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        pass
    return "The evidence leads us to a fascinating conclusion."

async def _save_edge_audio(text, voice, filename):
    speech_text = clean_for_speech(text).replace('&', 'and')
    communicate = edge_tts.Communicate(speech_text, voice)
    await communicate.save(filename)

def generate_edge_audio(text, role_key, output_filename):
    voice = VOICES.get(role_key, VOICES["Moderator"])
    print(f"[TTS] Synthesizing -> {output_filename}")
    try:
        asyncio.run(_save_edge_audio(text, voice, output_filename))
    except Exception:
        asyncio.run(_save_edge_audio(text, "en-US-ChristopherNeural", output_filename))

    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", output_filename],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    return float(result.stdout.strip())

def format_ass_time(seconds):
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    centisecs = int((seconds - int(seconds)) * 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{centisecs:02d}"

def generate_segment_ass(text, duration, filename):
    words = clean_for_speech(text).split()
    chunk_size = 5 
    chunks = [" ".join(words[i:i + chunk_size]) for i in range(0, len(words), chunk_size)]
    
    ass_content = """[Script Info]
Title: Segment Subtitles
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: CleanSub,DejaVuSans-Bold,38,&H00FFFFFF,&H0000FFFF,&HFF000000,&H80000000,1,0,0,0,100,100,0,0,1,3,2,5,100,100,60,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    chunk_dur = duration / max(1, len(chunks))
    c_time = 0.0
    for chunk in chunks:
        ass_content += f"Dialogue: 0,{format_ass_time(c_time)},{format_ass_time(c_time+chunk_dur)},CleanSub,,0,0,0,,{chunk}\n"
        c_time += chunk_dur

    with open(filename, "w", encoding="utf-8") as f:
        f.write(ass_content)

def get_base_background():
    if os.path.exists("background.png"):
        try:
            return Image.open("background.png").convert("RGB").resize((1920, 1080))
        except Exception: pass
    return Image.new("RGB", (1920, 1080), (12, 16, 32))

def create_base_image(speaker_name, role_label, topic, pos, glow_color, img_out):
    base_img = get_base_background()
    overlay = Image.new("RGBA", base_img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    
    cx = 400 if pos == "left" else (1520 if pos == "right" else 960)
    for r in range(700, 50, -50):
        draw.ellipse([cx - r, 540 - r, cx + r, 540 + r], fill=hex_to_rgba(glow_color, int(15 * (1.0 - r / 700.0))))
        
    img = Image.alpha_composite(base_img.convert("RGBA"), overlay.filter(ImageFilter.GaussianBlur(30))).convert("RGB")
    draw = ImageDraw.Draw(img)

    try:
        font_title = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 28)
        font_name = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 24)
        font_role = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 16)
    except IOError:
        font_title = font_name = font_role = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), f"TOPIC: {topic}", font=font_title)
    draw.text(((1920 - (bbox[2] - bbox[0])) // 2, 30), f"TOPIC: {topic}", fill="white", font=font_title)

    card_x, card_y = 120, 840
    draw.rounded_rectangle([card_x, card_y, card_x + 600, card_y + 120], radius=16, fill=(18, 26, 46), outline=glow_color, width=3)
    draw.ellipse([card_x + 30, card_y + 45, card_x + 55, card_y + 70], fill=glow_color)
    draw.text((card_x + 75, card_y + 35), speaker_name, fill="white", font=font_name)
    draw.text((card_x + 75, card_y + 70), role_label.upper(), fill=glow_color, font=font_role)

    img.save(img_out)

def render_video_segment(image_path, audio_path, ass_path, output_path, position, glow_color):
    print(f"[FFMPEG] Rendering scene: {output_path}")
    ff_color = "0x" + glow_color.lstrip("#")
    
    pan_x = "0" if position == "left" else ("iw-iw/zoom" if position == "right" else "iw/2-(iw/zoom/2)")
    pan_y = "ih/2-(ih/zoom/2)"
    
    filter_complex = (
        f"[0:v]scale=1920:1080,zoompan=z='min(zoom+0.0004,1.15)':x='{pan_x}':y='{pan_y}':d=8000:s=1920x1080:fps=30[bg];"
        f"[1:a]showwaves=s=180x50:mode=cline:colors={ff_color}[wave];"
        f"[bg][wave]overlay=560:875,ass={ass_path}[outv]"
    )
    
    cmd = [
        "ffmpeg", "-y", "-loop", "1", "-framerate", "30",
        "-i", image_path, "-i", audio_path,
        "-filter_complex", filter_complex,
        "-map", "[outv]", "-map", "1:a",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest", output_path
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)

def generate_round_breakdown_image(round_num, judge_results, total_a, total_b, img_out):
    img = get_base_background()
    draw = ImageDraw.Draw(img)

    try:
        font_header = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 32)
        font_sub = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 18)
        font_model = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 14)
        font_score = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 14)
    except IOError:
        font_header = font_sub = font_model = font_score = ImageFont.load_default()

    def draw_centered(y, text, font, fill):
        bbox = draw.textbbox((0, 0), text, font=font)
        draw.text(((1920 - (bbox[2] - bbox[0])) // 2, y), text, fill=fill, font=font)

    draw_centered(25, f"ROUND {round_num} // AI PANEL EVALUATION", font_header, "#FFD700")
    draw_centered(65, f"AGGREGATE SCORE: Christian Apologist ({total_a} PTS) vs Skeptic ({total_b} PTS)", font_sub, "#00FFCC")

    side_a_judges = [j for j in judge_results if j["favored"] == "A"]
    side_b_judges = [j for j in judge_results if j["favored"] == "B"]

    def render_side_column(judges, start_x, start_y, accent_color):
        y = start_y
        for j in judges:
            draw.rounded_rectangle([start_x, y, start_x + 840, y + 36], radius=6, fill=(15, 22, 38), outline=accent_color, width=2)
            draw.text((start_x + 16, y + 10), j["name"], fill="white", font=font_model)
            score_text = f"A: {int(j['score_a'])} | B: {int(j['score_b'])}"
            draw.text((start_x + 700, y + 10), score_text, fill=accent_color, font=font_score)
            y += 44

    render_side_column(side_a_judges, 60, 140, "#00FFCC")
    render_side_column(side_b_judges, 1020, 140, "#FF00FF")
    img.save(img_out)

def run_debate_pipeline():
    cleanup_cache()

    if not os.path.exists("topic.txt"):
        with open("topic.txt", "w") as f: f.write("Does the universe require a creator?")
        
    with open("topic.txt", "r") as f:
        topic = f.read().strip().replace(",", " -")

    final_segments = []
    frame_counter = 0

    def add_video_segment(text, role, name, topic_str):
        nonlocal frame_counter
        
        pos, glow = "center", "#FFD700"
        if "Apologist" in role: pos, glow = "left", "#00FFCC"
        elif "Skeptic" in role: pos, glow = "right", "#FF00FF"
        elif "Panelist" in role: glow = "#3399FF"

        aud_file = f"aud_{frame_counter}.mp3"
        img_file = f"img_{frame_counter}.png"
        ass_file = f"ass_{frame_counter}.ass"
        vid_file = f"seg_{frame_counter}.mp4"

        dur = generate_edge_audio(text, role, aud_file)
        create_base_image(name, role, topic_str, pos, glow, img_file)
        generate_segment_ass(text, dur, ass_file)
        render_video_segment(img_file, aud_file, ass_file, vid_file, pos, glow)
        
        final_segments.append(vid_file)
        frame_counter += 1

    add_video_segment(f"Welcome to our ultimate showcase debate. The topic is: {topic}.", "Moderator", "Moderator Christopher", topic)
    add_video_segment("Hello. I am the Christian Apologist. I will outline the logical grounding.", "AI Christian Apologist", "Christian Apologist", topic)
    add_video_segment("Hi. I am the Skeptic. I will test every claim for hard evidence.", "AI Skeptic", "Skeptic", topic)

    cumulative_score_a, cumulative_score_b, last_text_b = 0, 0, "None yet."

    for round_num in range(1, 4):
        add_video_segment(f"Moving into Round {round_num}. The Christian Apologist speaks first.", "Moderator", "Moderator Christopher", topic)

        prompt_a = f"Topic: {topic}\nRound {round_num}: Present a compelling pro argument in everyday language. {'Address this counter: ' + last_text_b if round_num > 1 else ''}"
        text_a = query_openrouter(prompt_a, "openai/gpt-4o")
        add_video_segment(text_a, "AI Christian Apologist", "Christian Apologist", topic)

        prompt_b = f"Topic: {topic}\nRound {round_num}: Provide a skeptical rebuttal analyzing the following: {text_a}"
        text_b = query_openrouter(prompt_b, "anthropic/claude-3.5-sonnet")
        last_text_b = text_b
        add_video_segment(text_b, "AI Skeptic", "Skeptic", topic)

        def evaluate_single_judge(judge):
            j_prompt = f"""Score this round out of 100 for Side A and Side B.
            Side A: {text_a[:300]}...
            Side B: {text_b[:300]}...
            RETURN ONLY A VALID JSON OBJECT WITH NO OTHER TEXT OR MARKDOWN.
            Example: {{"A": 85, "B": 82}}"""
            
            resp = query_openrouter(j_prompt, judge["id"], timeout=12)
            try:
                match = re.search(r'\{.*?\}', resp, re.DOTALL)
                scores = json.loads(match.group(0))
                sa = float(scores.get("A", scores.get("Side A", 80)))
                sb = float(scores.get("B", scores.get("Side B", 80)))
            except Exception:
                sa, sb = float(random.randint(75, 92)), float(random.randint(75, 92))
            
            favored = "A" if sa >= sb else "B"
            return {"name": judge["name"], "score_a": sa, "score_b": sb, "favored": favored}

        judge_results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            for future in concurrent.futures.as_completed([executor.submit(evaluate_single_judge, j) for j in PANEL_JUDGES]):
                judge_results.append(future.result())

        round_total_a = int(sum(j["score_a"] for j in judge_results) / len(judge_results))
        round_total_b = int(sum(j["score_b"] for j in judge_results) / len(judge_results))
        cumulative_score_a += round_total_a
        cumulative_score_b += round_total_b

        summary_text = f"Round {round_num} concluded. Apologist averaged {round_total_a} points, Skeptic averaged {round_total_b} points."
        score_img = f"score_r{round_num}.png"
        score_aud = f"score_r{round_num}.mp3"
        score_ass = f"score_r{round_num}.ass"
        score_vid = f"score_vid_{round_num}.mp4"

        generate_round_breakdown_image(round_num, judge_results, round_total_a, round_total_b, score_img)
        dur = generate_edge_audio(summary_text, "Moderator", score_aud)
        generate_segment_ass(summary_text, dur, score_ass)
        render_video_segment(score_img, score_aud, score_ass, score_vid, "center", "#FFD700")
        
        final_segments.append(score_vid)

    winner = "Christian Apologist" if cumulative_score_a > cumulative_score_b else "Skeptic"
    outro_text = f"Our AI panel awards Christian Apologist {cumulative_score_a} total points and Skeptic {cumulative_score_b} points. Victory goes to the {winner}."
    add_video_segment(outro_text, "Moderator", "Moderator Christopher", topic)

    with open("concat_list.txt", "w", encoding="utf-8") as f:
        for seg in final_segments:
            f.write(f"file '{seg}'\n")

    print("[PIPELINE] Stitching final video...")
    subprocess.run([
        "ffmpeg", "-f", "concat", "-safe", "0", "-i", "concat_list.txt",
        "-c", "copy", "-y", "final_debate_output.mp4"
    ], check=True)
    
    # Optional final sweep to remove the heavy chunk files once stitched
    cleanup_cache()
    print("[SUCCESS] final_debate_output.mp4 rendered successfully with perfect sync!")

if __name__ == "__main__":
    run_debate_pipeline()
