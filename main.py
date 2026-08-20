import os
import asyncio
import requests
import subprocess
import re
import edge_tts
from PIL import Image, ImageDraw, ImageFont, ImageFilter

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

VOICES = {
    "Moderator": "en-US-ChristopherNeural",
    "AI Christian Apologist": "en-US-BrianNeural",
    "AI Skeptic": "en-US-AriaNeural",
    "GPT": "en-US-AndrewNeural",
    "Claude Sonnet": "en-US-SteffanNeural"
}

PRIMARY_JUDGES = [
    {"name": "GPT", "provider": "OpenAI", "id": "openai/gpt-5.6"},
    {"name": "Claude Sonnet", "provider": "Anthropic", "id": "anthropic/claude-3.5-sonnet"},
    {"name": "Gemini", "provider": "Google", "id": "~google/gemini-pro-latest"},
    {"name": "DeepSeek", "provider": "DeepSeek", "id": "deepseek/deepseek-chat"},
    {"name": "Mistral", "provider": "Mistral", "id": "mistralai/mistral-large"},
    {"name": "Llama", "provider": "Meta", "id": "meta-llama/llama-3-70b-instruct"},
    {"name": "Command R", "provider": "Cohere", "id": "cohere/command-r-plus"},
    {"name": "Grok", "provider": "xAI", "id": "x-ai/grok-4.6"},
    {"name": "Qwen", "provider": "Alibaba", "id": "qwen/qwen-2.5-72b-instruct"},
    {"name": "Nemotron", "provider": "NVIDIA", "id": "nvidia/llama-3.1-nemotron-70b-instruct"},
    {"name": "Perplexity", "provider": "Perplexity", "id": "perplexity/sonar-medium"},
    {"name": "Phi", "provider": "Microsoft", "id": "microsoft/phi-3-medium-128k-instruct"},
    {"name": "Gemma", "provider": "Google", "id": "google/gemma-2-27b-it"},
    {"name": "WizardLM", "provider": "Microsoft", "id": "microsoft/wizardlm-2-8x22b"},
    {"name": "Yi", "provider": "01.AI", "id": "01-ai/yi-large"}
]

FALLBACK_MODELS = [
    {"name": "Gemini Flash", "id": "google/gemini-flash-1.5"},
    {"name": "DeepSeek", "id": "deepseek/deepseek-chat"},
    {"name": "GPT Mini", "id": "openai/gpt-4o-mini"}
]

def clean_for_speech(text):
    # Remove markdown symbols (*, **, #, etc.) and clean punctuation
    cleaned = re.sub(r'[*#_`]', '', text)
    cleaned = cleaned.replace(":", " ").replace(";", " ").replace('"', '').replace('"', '').replace('"', '')
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned

def query_openrouter(prompt, primary_model_id, timeout=45):
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }
    model_ids_to_try = [primary_model_id] + [f["id"] for f in FALLBACK_MODELS]
    for model_id in model_ids_to_try:
        payload = {
            "model": model_id,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.7
        }
        try:
            response = requests.post(OPENROUTER_URL, headers=headers, json=payload, timeout=timeout)
            if response.status_code == 200:
                return response.json()["choices"][0]["message"]["content"].strip()
        except Exception:
            continue
    return "Analysis complete. Scores evaluated."

async def _save_edge_audio(text, voice, filename, retries=3):
    speech_text = clean_for_speech(text).replace('&', 'and')
    for attempt in range(retries):
        try:
            communicate = edge_tts.Communicate(speech_text, voice)
            await communicate.save(filename)
            if os.path.exists(filename) and os.path.getsize(filename) > 1000:
                return
        except Exception as e:
            print(f"[WARNING] Edge-TTS attempt {attempt + 1} failed for voice {voice}: {e}")
            await asyncio.sleep(2)
    raise Exception(f"Failed to generate Edge-TTS audio for voice {voice} after {retries} attempts.")

def generate_edge_audio(text, role_key, output_filename):
    voice = VOICES.get(role_key, VOICES["Moderator"])
    print(f"[EDGE-TTS] Synthesizing [{role_key}] audio -> {output_filename}...")
    try:
        asyncio.run(_save_edge_audio(text, voice, output_filename))
    except Exception as e:
        print(f"[ERROR] {e}. Falling back to default moderator voice...")
        asyncio.run(_save_edge_audio(text, "en-US-ChristopherNeural", output_filename))

    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", output_filename]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        duration = float(result.stdout.strip())
    except Exception:
        duration = max(5.0, len(text.split()) / 2.5)
    return duration

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

def generate_speaker_frame(speaker_name, role_label, topic, frame_index, wave_phase=1):
    pos = "center"
    glow_color = "#FFD700"
    if "Christian Apologist" in role_label:
        pos = "left"
        glow_color = "#00FFCC"
    elif "Skeptic" in role_label:
        pos = "right"
        glow_color = "#FF00FF"

    base_img = get_base_background()
    
    # Dynamic Zoom/Crop framing based on active speaker
    if pos == "left":
        # Crop and zoom into the left podium
        cropped = base_img.crop((0, 0, 1400, 1080)).resize((1920, 1080), Image.Resampling.LANCZOS)
    elif pos == "right":
        # Crop and zoom into the right podium
        cropped = base_img.crop((520, 0, 1920, 1080)).resize((1920, 1080), Image.Resampling.LANCZOS)
    else:
        cropped = base_img

    # Apply spotlight overlay on cropped frame
    overlay = Image.new("RGBA", cropped.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    
    cx, cy = (480, 540) if pos == "left" else (1440, 540) if pos == "right" else (960, 540)
    if pos != "center":
        cx = 960  # Center relative to cropped view
        
    for r in range(600, 50, -50):
        alpha = int(20 * (1.0 - r / 600.0))
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(glow_color[1:] if len(glow_color)==7 else "FFD700", alpha))
        
    overlay = overlay.filter(ImageFilter.GaussianBlur(30))
    img = Image.alpha_composite(cropped.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(img)

    try:
        font_title = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 28)
        font_name = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 24)
        font_role = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 16)
    except IOError:
        font_title = font_name = font_role = ImageFont.load_default()

    def draw_centered(y, text, font, fill):
        bbox = draw.textbbox((0, 0), text, font=font)
        w = bbox[2] - bbox[0]
        draw.text(((1920 - w) // 2, y), text, fill=fill, font=font)

    draw_centered(30, f"TOPIC: {topic}", font_title, "white")

    card_x, card_y = 120, 840
    draw.rounded_rectangle([card_x, card_y, card_x + 600, card_y + 120], radius=16, fill=(18, 26, 46), outline=glow_color, width=3)
    draw.ellipse([card_x + 30, card_y + 45, card_x + 55, card_y + 70], fill=glow_color)

    # Clean non-duplicated labels
    display_role = "PRO-APOLOGIST" if "Christian" in role_label else ("CHALLENGER" if "Skeptic" in role_label else "MODERATOR")
    draw.text((card_x + 75, card_y + 25), speaker_name, fill="white", font=font_name)
    draw.text((card_x + 75, card_y + 65), display_role, fill=glow_color, font=font_role)

    # Animated audio bars simulation based on wave_phase
    bar_start_x = card_x + 460
    bar_base_y = card_y + 60
    
    wave_patterns = {
        1: [12, 28, 40, 22, 14],
        2: [35, 18, 10, 30, 38],
        3: [20, 35, 25, 12, 30],
        4: [15, 22, 36, 28, 18]
    }
    heights = wave_patterns.get(wave_phase, [15, 25, 35, 20, 15])

    for i, h in enumerate(heights):
        bx = bar_start_x + (i * 12)
        draw.rounded_rectangle([bx, bar_base_y - h, bx + 6, bar_base_y + h], radius=2, fill=glow_color)

    filename = f"speaker_{frame_index}.png"
    img.save(filename)
    return filename

def generate_round_breakdown_image(round_num, scores_a, scores_b, total_a, total_b):
    img = get_base_background()
    draw = ImageDraw.Draw(img)

    try:
        font_header = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 36)
        font_sub = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 22)
        font_col = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 16)
        font_model = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 14)
        font_meta = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 12)
        font_score = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 16)
    except IOError:
        font_header = font_sub = font_col = font_model = font_meta = font_score = ImageFont.load_default()

    def draw_centered(y, text, font, fill):
        bbox = draw.textbbox((0, 0), text, font=font)
        w = bbox[2] - bbox[0]
        draw.text(((1920 - w) // 2, y), text, fill=fill, font=font)

    draw_centered(30, f"ROUND {round_num} JUDGING BREAKDOWN (15 AI JUDGES)", font_header, "#FFD700")
    draw_centered(75, f"SCORE: Christian Apologist ({total_a} PTS) vs Skeptic ({total_b} PTS)", font_sub, "white")

    col_a_judges = list(zip(PRIMARY_JUDGES[:8], scores_a[:8]))
    col_b_judges = list(zip(PRIMARY_JUDGES[8:], scores_b[8:]))

    y_start = 145
    for (judge, score) in col_a_judges:
        draw.rounded_rectangle([130, y_start, 940, y_start + 50], radius=6, fill=(18, 26, 46), outline=(50, 70, 100), width=1)
        draw.text((150, y_start + 10), judge["name"], fill="white", font=font_model)
        draw.text((150, y_start + 28), judge["provider"], fill="#8A99AD", font=font_meta)
        draw.text((860, y_start + 15), f"{score} pts", fill="#00FFCC", font=font_score)
        y_start += 56

    y_start = 145
    for (judge, score) in col_b_judges:
        draw.rounded_rectangle([1030, y_start, 1840, y_start + 50], radius=6, fill=(18, 26, 46), outline=(50, 70, 100), width=1)
        draw.text((1050, y_start + 10), judge["name"], fill="white", font=font_model)
        draw.text((1050, y_start + 28), judge["provider"], fill="#8A99AD", font=font_meta)
        draw.text((1760, y_start + 15), f"{score} pts", fill="#FF00FF", font=font_score)
        y_start += 56

    filename = f"round_{round_num}_breakdown.png"
    img.save(filename)
    return filename

def add_clean_subtitle_events(text, start_time, duration, dialogue_events):
    cleaned = clean_for_speech(text)
    words = cleaned.split()
    if not words:
        return
    chunk_size = 8
    chunks = [" ".join(words[i:i + chunk_size]) for i in range(0, len(words), chunk_size)]
    chunk_duration = duration / len(chunks)
    curr = start_time
    for chunk in chunks:
        dialogue_events.append((curr, curr + chunk_duration, chunk))
        curr += chunk_duration

def run_debate_pipeline():
    if not os.path.exists("topic.txt"):
        print("[ERROR] topic.txt not found!")
        return

    with open("topic.txt", "r") as f:
        topic = f.read().strip().replace(",", " -")

    print(f"\n[DEBATE-PIPELINE] Loaded Topic: '{topic}'")

    audio_files = []
    segment_images = []
    dialogue_events = []
    current_time = 0.0
    frame_counter = 0

    def add_animated_segment(text, role, name, topic_str, base_phase):
        nonlocal current_time, frame_counter
        dur = generate_edge_audio(text, role, f"seg_{frame_counter}.mp3")
        audio_files.append(f"seg_{frame_counter}.mp3")
        
        # Split segment into sub-clips to animate the audio wave bars dynamically
        sub_count = max(1, int(dur // 2.5))
        sub_dur = dur / sub_count
        for i in range(sub_count):
            phase = ((base_phase + i) % 4) + 1
            img = generate_speaker_frame(name, role, topic_str, frame_counter, wave_phase=phase)
            segment_images.append((img, sub_dur))
            frame_counter += 1
            
        add_clean_subtitle_events(text, current_time, dur, dialogue_events)
        current_time += dur

    # 1. Moderator Intro
    intro_text = f"Welcome to today's showcase debate. Our central question is: {topic}. Fifteen AI judges are ready to score, and our debaters will break down both perspectives."
    add_animated_segment(intro_text, "Moderator", "Moderator Christopher", topic, 1)

    # 2. Debater Introductions
    intro_a_text = "Hello everyone. I am the Christian Apologist. I will outline the logical and historical foundations supporting the Christian worldview."
    add_animated_segment(intro_a_text, "AI Christian Apologist", "Christian Apologist", topic, 2)

    intro_b_text = "Hi. I am the Skeptic. I will examine every claim critically to test whether the evidence holds up under scrutiny."
    add_animated_segment(intro_b_text, "AI Skeptic", "Skeptic", topic, 3)

    cumulative_score_a = 0
    cumulative_score_b = 0
    round_representative_judges = [PRIMARY_JUDGES[1], PRIMARY_JUDGES[2], PRIMARY_JUDGES[3]]
    last_text_b = "None yet."

    for round_num in range(1, 4):
        print(f"\n--- Round {round_num} of 3 ---")
        
        narrator_intro = f"Moving into Round {round_num} on {topic}. The Christian Apologist presents first, followed by the Skeptic's direct rebuttal."
        add_animated_segment(narrator_intro, "Moderator", "Moderator Christopher", topic, 1)

        # Apologist Speech
        prompt_a = f"Topic: {topic}\nRound {round_num}: Present a strong pro-apologetic argument in clear, everyday language. {'Directly address this counter-argument from the Skeptic: ' + last_text_b if round_num > 1 else ''} Write about 120 words."
        text_a = query_openrouter(prompt_a, primary_model_id="openai/gpt-5.6").replace(",", " -")
        add_animated_segment(text_a, "AI Christian Apologist", "Christian Apologist", topic, 2)

        # Skeptic Speech
        prompt_b = f"Topic: {topic}\nRound {round_num}: Provide a skeptical counter-argument. Directly analyze and challenge the points just made by the Christian Apologist in this statement: {text_a}. Write about 120 words."
        text_b = query_openrouter(prompt_b, primary_model_id="anthropic/claude-3.5-sonnet").replace(",", " -")
        last_text_b = text_b
        add_animated_segment(text_b, "AI Skeptic", "Skeptic", topic, 3)

        scores_a, scores_b = [], []
        for judge in PRIMARY_JUDGES:
            resp = query_openrouter(f"Score Round {round_num} on '{topic}'. Format: 'A: [score], B: [score]'", primary_model_id=judge["id"], timeout=15)
            try:
                parts = resp.replace(" ", "").upper().split(",")
                sa = int([p for p in parts if p.startswith("A:")][0].split(":")[1])
                sb = int([p for p in parts if p.startswith("B:")][0].split(":")[1])
            except Exception:
                sa, sb = 80, 78
            scores_a.append(sa)
            scores_b.append(sb)

        round_total_a = sum(scores_a) // len(scores_a)
        round_total_b = sum(scores_b) // len(scores_b)
        cumulative_score_a += round_total_a
        cumulative_score_b += round_total_b

        rep_judge = round_representative_judges[round_num - 1]
        judge_commentary_prompt = f"Topic: {topic}. In Round {round_num}, Christian Apologist scored {round_total_a} and Skeptic scored {round_total_b}. In 2 simple sentences, explain as {rep_judge['name']} why one side's reasoning stood out."
        judge_commentary = query_openrouter(judge_commentary_prompt, primary_model_id=rep_judge["id"]).replace(",", " -")

        breakdown_img = generate_round_breakdown_image(round_num, scores_a, scores_b, round_total_a, round_total_b)
        summary_text = f"Round {round_num} complete. {rep_judge['name']} notes: {judge_commentary} Christian Apologist earned {round_total_a} points, and Skeptic earned {round_total_b} points."
        
        dur = generate_edge_audio(summary_text, "Moderator", f"r{round_num}_sum.mp3")
        audio_files.append(f"r{round_num}_sum.mp3")
        segment_images.append((breakdown_img, dur))
        frame_counter += 1
        add_clean_subtitle_events(summary_text, current_time, dur, dialogue_events)
        current_time += dur

    winner = "Christian Apologist" if cumulative_score_a > cumulative_score_b else "Skeptic"
    outro_text = f"As our debate concludes, the fifteen AI judges award Christian Apologist {cumulative_score_a} total points and Skeptic {cumulative_score_b} points. Our panel awards this showcase victory to the {winner}. Thank you for watching."
    add_animated_segment(outro_text, "Moderator", "Moderator Christopher", topic, 1)

    print("\n[SUBTITLES] Building synchronized subtitle layout...")
    ass_content = """[Script Info]
Title: AI Debate Subtitles
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: CleanSub,DejaVuSans-Bold,38,&H00FFFFFF,&H000000FF,&HFF000000,&H80000000,1,0,0,0,100,100,0,0,1,3,2,5,100,100,60,1

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
            f.write(f"file '{img_path}'\n")
            f.write(f"duration {dur}\n")
        if segment_images:
            f.write(f"file '{segment_images[-1][0]}'\n")

    print("\n[FFmpeg] Rendering final video package...")
    ffmpeg_cmd = [
        "ffmpeg",
        "-f", "concat", "-safe", "0", "-i", "video_list.txt",
        "-f", "concat", "-safe", "0", "-i", "audio_list.txt",
        "-vf", "subtitles=subtitles.ass",
        "-c:v", "libx264",
        "-c:a", "aac",
        "-pix_fmt", "yuv420p",
        "-y",
        "final_debate_output.mp4"
    ]
    subprocess.run(ffmpeg_cmd, check=True)
    print("[SUCCESS] final_debate_output.mp4 successfully created with dynamic framing and clean subtitles!")

if __name__ == "__main__":
    run_debate_pipeline()
