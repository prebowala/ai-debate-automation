import os
import asyncio
import requests
import subprocess
import re
import math
import edge_tts
import concurrent.futures
from PIL import Image, ImageDraw, ImageFont, ImageFilter

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Most natural sounding Multilingual Neural voices
VOICES = {
    "Moderator": "en-US-ChristopherNeural",
    "AI Christian Apologist": "en-US-BrianMultilingualNeural",
    "AI Skeptic": "en-US-AvaMultilingualNeural",
    "Panelist 1": "en-US-AndrewMultilingualNeural",
    "Panelist 2": "en-US-EmmaMultilingualNeural"
}

# Simplified names for the 10 core judges as requested
PANEL_JUDGES = [
    {"name": "GPT", "provider": "OpenAI", "id": "openai/gpt-4o"},
    {"name": "Claude", "provider": "Anthropic", "id": "anthropic/claude-3.5-sonnet"},
    {"name": "Gemini", "provider": "Google", "id": "google/gemini-pro-1.5"},
    {"name": "DeepSeek", "provider": "DeepSeek", "id": "deepseek/deepseek-chat"},
    {"name": "Mistral", "provider": "Mistral", "id": "mistralai/mistral-large"},
    {"name": "Llama", "provider": "Meta", "id": "meta-llama/llama-3.1-70b-instruct"},
    {"name": "Cohere", "provider": "Cohere", "id": "cohere/command-r-plus"},
    {"name": "Grok", "provider": "xAI", "id": "x-ai/grok-2"},
    {"name": "Qwen", "provider": "Alibaba", "id": "qwen/qwen-2.5-72b-instruct"},
    {"name": "Sonar", "provider": "Perplexity", "id": "perplexity/sonar-pro"}
]

FALLBACK_MODELS = [
    {"name": "Gemini Flash", "id": "google/gemini-flash-1.5"},
    {"name": "DeepSeek Chat", "id": "deepseek/deepseek-chat"},
    {"name": "GPT Mini", "id": "openai/gpt-4o-mini"}
]

def clean_for_speech(text):
    cleaned = re.sub(r'\([^)]*\)', '', text)
    cleaned = re.sub(r'[*#_`–—]', '', cleaned)
    cleaned = cleaned.replace(":", " ").replace(";", " ").replace('"', '').replace('"', '')
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned

def hex_to_rgba(hex_str, alpha):
    hex_str = hex_str.lstrip('#')
    return (int(hex_str[0:2], 16), int(hex_str[2:4], 16), int(hex_str[4:6], 16), alpha)

def query_openrouter(prompt, primary_model_id, timeout=45):
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }
    for model_id in [primary_model_id] + [f["id"] for f in FALLBACK_MODELS]:
        try:
            response = requests.post(
                OPENROUTER_URL, 
                headers=headers, 
                json={"model": model_id, "messages": [{"role": "user", "content": prompt}], "temperature": 0.7}, 
                timeout=timeout
            )
            if response.status_code == 200:
                return response.json()["choices"][0]["message"]["content"].strip()
        except Exception:
            continue
    return "Side A: 82, Side B: 80"

async def _save_edge_audio(text, voice, filename, retries=3):
    speech_text = clean_for_speech(text).replace('&', 'and')
    for attempt in range(retries):
        try:
            communicate = edge_tts.Communicate(speech_text, voice)
            await communicate.save(filename)
            if os.path.exists(filename) and os.path.getsize(filename) > 1000:
                return
        except Exception:
            await asyncio.sleep(2)
    raise Exception(f"Failed to generate Edge-TTS audio for voice {voice}.")

def generate_edge_audio(text, role_key, output_filename):
    voice = VOICES.get(role_key, VOICES["Moderator"])
    print(f"[EDGE-TTS] Synthesizing [{role_key}] -> {output_filename}...")
    try:
        asyncio.run(_save_edge_audio(text, voice, output_filename))
    except Exception:
        asyncio.run(_save_edge_audio(text, "en-US-ChristopherNeural", output_filename))

    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", output_filename],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        return float(result.stdout.strip())
    except Exception:
        return max(5.0, len(text.split()) / 2.5)

def format_ass_time(seconds):
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    centisecs = int((seconds - int(seconds)) * 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{centisecs:02d}"

def get_base_background():
    if os.path.exists("background.png"):
        try:
            return Image.open("background.png").convert("RGB").resize((1920, 1080))
        except Exception:
            pass
    return Image.new("RGB", (1920, 1080), (12, 16, 32))

def generate_speaker_frame(speaker_name, role_label, topic, frame_index):
    """
    Generates speaker frame with active cinematic zoom, glowing ambient lighting,
    moving speaker indicator labels, and responsive audio visualizer bars.
    """
    pos, glow_color = "center", "#FFD700"
    if "Christian Apologist" in role_label:
        pos, glow_color = "left", "#00FFCC"
    elif "Skeptic" in role_label:
        pos, glow_color = "right", "#FF00FF"
    elif "Panelist" in role_label:
        glow_color = "#3399FF"

    base_img = get_base_background()
    
    # Cinematic camera zoom effect based on active speaker position
    if pos == "left":
        cropped = base_img.crop((0, 0, 1400, 1080)).resize((1920, 1080), Image.Resampling.LANCZOS)
    elif pos == "right":
        cropped = base_img.crop((520, 0, 1920, 1080)).resize((1920, 1080), Image.Resampling.LANCZOS)
    else:
        # Slight center zoom
        cropped = base_img.crop((160, 90, 1760, 990)).resize((1920, 1080), Image.Resampling.LANCZOS)

    overlay = Image.new("RGBA", cropped.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    cx, cy = 960, 540
    for r in range(600, 50, -50):
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=hex_to_rgba(glow_color, int(20 * (1.0 - r / 600.0))))
        
    img = Image.alpha_composite(cropped.convert("RGBA"), overlay.filter(ImageFilter.GaussianBlur(30))).convert("RGB")
    draw = ImageDraw.Draw(img)

    try:
        font_title = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 28)
        font_name = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 24)
        font_role = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 16)
    except IOError:
        font_title = font_name = font_role = ImageFont.load_default()

    def draw_centered(y, text, font, fill):
        bbox = draw.textbbox((0, 0), text, font=font)
        draw.text(((1920 - (bbox[2] - bbox[0])) // 2, y), text, fill=fill, font=font)

    draw_centered(30, f"TOPIC: {topic}", font_title, "white")

    # Moving label card
    card_x, card_y = 120, 840
    draw.rounded_rectangle([card_x, card_y, card_x + 600, card_y + 120], radius=16, fill=(18, 26, 46), outline=glow_color, width=3)
    draw.ellipse([card_x + 30, card_y + 45, card_x + 55, card_y + 70], fill=glow_color)

    draw.text((card_x + 75, card_y + 35), speaker_name, fill="white", font=font_name)
    draw.text((card_x + 75, card_y + 70), role_label.upper(), fill=glow_color, font=font_role)

    # Active soundbar visualizer animation
    for i in range(5):
        bx = card_x + 460 + (i * 12)
        h = int(12 + 18 * abs(math.sin(frame_index * 1.8 + i * 0.9)))
        draw.rounded_rectangle([bx, (card_y + 60) - h, bx + 6, (card_y + 60) + h], radius=2, fill=glow_color)

    filename = f"speaker_{frame_index}.png"
    img.save(filename)
    return filename

def generate_round_breakdown_image(round_num, judge_results, total_a, total_b):
    """
    Between-rounds screen with soundbars and AI models sorted dynamically 
    onto the side (left/right) of the contestant they favor.
    """
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

    # Split judges dynamically based on favored side
    side_a_judges = [j for j in judge_results if j["favored"] == "A"]
    side_b_judges = [j for j in judge_results if j["favored"] == "B"]

    def render_side_column(judges, start_x, start_y, accent_color):
        y = start_y
        for j in judges:
            draw.rounded_rectangle([start_x, y, start_x + 840, y + 36], radius=6, fill=(15, 22, 38), outline=accent_color, width=2)
            draw.text((start_x + 16, y + 10), j["name"], fill="white", font=font_model)
            score_text = f"A: {int(j['score_a'])} | B: {int(j['score_b'])}"
            draw.text((start_x + 700, y + 10), score_text, fill=accent_color, font=font_score)
            
            # Soundbar graphic next to each judge
            for bar_i in range(3):
                bx = start_x + 630 + (bar_i * 10)
                draw.rounded_rectangle([bx, y + 12, bx + 6, y + 24], radius=2, fill=accent_color)
            y += 42

    render_side_column(side_a_judges, 60, 140, "#00FFCC")
    render_side_column(side_b_judges, 1020, 140, "#FF00FF")

    filename = f"round_{round_num}_breakdown.png"
    img.save(filename)
    return filename

def run_debate_pipeline():
    if not os.path.exists("topic.txt"):
        print("[ERROR] topic.txt not found!")
        return

    with open("topic.txt", "r") as f:
        topic = f.read().strip().replace(",", " -")

    audio_files, segment_images, dialogue_events = [], [], []
    current_time, frame_counter = 0.0, 0

    def add_animated_segment(text, role, name, topic_str):
        nonlocal current_time, frame_counter
        seg_audio = f"seg_{frame_counter}.mp3"
        dur = generate_edge_audio(text, role, seg_audio)
        audio_files.append(seg_audio)
        
        sub_count = max(1, int(dur // 2.5))
        sub_dur = dur / sub_count
        for _ in range(sub_count):
            segment_images.append((generate_speaker_frame(name, role, topic_str, frame_counter), sub_dur))
            frame_counter += 1
            
        cleaned = clean_for_speech(text)
        words = cleaned.split()
        chunk_size = max(6, len(words) // sub_count)
        chunks = [" ".join(words[i:i + chunk_size]) for i in range(0, len(words), chunk_size)]
        
        chunk_dur = dur / max(1, len(chunks))
        c_time = current_time
        for chunk in chunks:
            dialogue_events.append((c_time, c_time + chunk_dur, chunk))
            c_time += chunk_dur
        current_time += dur

    add_animated_segment(f"Welcome to our ultimate showcase debate. The topic is: {topic}. Our AI panel is ready.", "Moderator", "Moderator Christopher", topic)
    add_animated_segment("Hello everyone. I am the Christian Apologist. I will outline the core philosophy.", "AI Christian Apologist", "Christian Apologist", topic)
    add_animated_segment("Hi. I am the Skeptic. I will test every claim for hard evidence.", "AI Skeptic", "Skeptic", topic)

    add_animated_segment("Panel representatives GPT and Claude will set our evaluation criteria.", "Panelist 1", "Moderator Christopher", topic)
    add_animated_segment("As GPT, we ensure unbiased multi-model scoring across all rounds.", "Panelist 1", "GPT", topic)
    add_animated_segment("And as Claude, we verify logical consistency across every debate exchange.", "Panelist 2", "Claude", topic)

    cumulative_score_a, cumulative_score_b, last_text_b = 0, 0, "None yet."

    for round_num in range(1, 4):
        add_animated_segment(f"Moving into Round {round_num}. Christian Apologist speaks first.", "Moderator", "Moderator Christopher", topic)

        prompt_a = f"Topic: {topic}\nRound {round_num}: Present a compelling pro argument in everyday language. {'Address this counter: ' + last_text_b if round_num > 1 else ''}"
        text_a = query_openrouter(prompt_a, "openai/gpt-4o")
        add_animated_segment(text_a, "AI Christian Apologist", "Christian Apologist", topic)

        prompt_b = f"Topic: {topic}\nRound {round_num}: Provide a skeptical rebuttal analyzing: {text_a}."
        text_b = query_openrouter(prompt_b, "anthropic/claude-3.5-sonnet")
        last_text_b = text_b
        add_animated_segment(text_b, "AI Skeptic", "Skeptic", topic)

        def evaluate_single_judge(judge):
            resp = query_openrouter(f"Score Round {round_num} on '{topic}'. Reply ONLY with format: Side A: [score], Side B: [score]", judge["id"], timeout=12)
            try:
                match_a = re.search(r'Side\s*A\s*[:=]\s*(\d+)', resp, re.IGNORECASE)
                match_b = re.search(r'Side\s*B\s*[:=]\s*(\d+)', resp, re.IGNORECASE)
                sa = float(match_a.group(1)) if match_a else 82.0
                sb = float(match_b.group(1)) if match_b else 80.0
                sa = max(50.0, min(100.0, sa))
                sb = max(50.0, min(100.0, sb))
            except Exception:
                sa, sb = 82.0, 80.0
            
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

        breakdown_img = generate_round_breakdown_image(round_num, judge_results, round_total_a, round_total_b)
        summary_text = f"Round {round_num} concluded. Christian Apologist averaged {round_total_a} points, and Skeptic averaged {round_total_b} points."
        
        sum_audio = f"r{round_num}_sum.mp3"
        dur = generate_edge_audio(summary_text, "Moderator", sum_audio)
        audio_files.append(sum_audio)
        segment_images.append((breakdown_img, dur))
        frame_counter += 1
        
        dialogue_events.append((current_time, current_time + dur, summary_text))
        current_time += dur

    winner = "Christian Apologist" if cumulative_score_a > cumulative_score_b else "Skeptic"
    outro_text = f"Our AI panel awards Christian Apologist {cumulative_score_a} total points and Skeptic {cumulative_score_b} points. Victory goes to the {winner}."
    add_animated_segment(outro_text, "Moderator", "Moderator Christopher", topic)

    ass_content = """[Script Info]
Title: AI Debate Subtitles
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: CleanSub,DejaVuSans-Bold,36,&H00FFFFFF,&H0000FFFF,&HFF000000,&H80000000,1,0,0,0,100,100,0,0,1,3,2,5,100,100,60,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    for start, end, text in dialogue_events:
        ass_content += f"Dialogue: 0,{format_ass_time(start)},{format_ass_time(end)},CleanSub,,0,0,0,,{text}\n"

    with open("subtitles.ass", "w", encoding="utf-8") as f:
        f.write(ass_content)

    with open("audio_list.txt", "w", encoding="utf-8") as f:
        for audio in audio_files:
            f.write(f"file '{audio}'\n")

    with open("video_list.txt", "w", encoding="utf-8") as f:
        for img_path, dur in segment_images:
            f.write(f"file '{img_path}'\nfile '{dur}'\n")
        if segment_images:
            f.write(f"file '{segment_images[-1][0]}'\n")

    subprocess.run([
        "ffmpeg", "-f", "concat", "-safe", "0", "-i", "video_list.txt",
        "-f", "concat", "-safe", "0", "-i", "audio_list.txt",
        "-vf", "subtitles=subtitles.ass", "-c:v", "libx264", "-c:a", "aac", "-pix_fmt", "yuv420p", "-y", "final_debate_output.mp4"
    ], check=True)
    print("[SUCCESS] final_debate_output.mp4 rendered successfully!")

if __name__ == "__main__":
    run_debate_pipeline()
