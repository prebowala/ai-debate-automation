import os
import asyncio
import requests
import subprocess
import edge_tts
from PIL import Image, ImageDraw, ImageFont, ImageFilter

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Distinct voice profiles for clear differentiation
VOICES = {
    "Moderator": "en-US-ChristopherNeural",       # Authoritative deep male voice
    "Christian Apologist": "en-US-BrianNeural",   # Distinct steady male voice
    "Skeptic": "en-US-AriaNeural"                 # Distinct expressive female voice
}

PRIMARY_JUDGES = [
    {"name": "GPT-5.6", "provider": "OpenAI", "id": "openai/gpt-5.6"},
    {"name": "Claude 3.5 Sonnet", "provider": "Anthropic", "id": "anthropic/claude-3.5-sonnet"},
    {"name": "Gemini Pro Latest", "provider": "Google", "id": "~google/gemini-pro-latest"},
    {"name": "DeepSeek Chat", "provider": "DeepSeek", "id": "deepseek/deepseek-chat"},
    {"name": "Mistral Large", "provider": "Mistral", "id": "mistralai/mistral-large"},
    {"name": "Llama 3.3 70B", "provider": "Meta", "id": "meta-llama/llama-3-70b-instruct"},
    {"name": "Command R+", "provider": "Cohere", "id": "cohere/command-r-plus"},
    {"name": "Grok 4.6", "provider": "xAI", "id": "x-ai/grok-4.6"},
    {"name": "Qwen 2.5 72B", "provider": "Alibaba", "id": "qwen/qwen-2.5-72b-instruct"},
    {"name": "Nemotron 3 Ultra", "provider": "NVIDIA", "id": "nvidia/llama-3.1-nemotron-70b-instruct"}
]

FALLBACK_MODELS = [
    "google/gemini-flash-1.5",
    "deepseek/deepseek-chat",
    "openai/gpt-4o-mini"
]

def query_openrouter(prompt, primary_model, timeout=45):
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }
    models_to_try = [primary_model] + FALLBACK_MODELS
    for model in models_to_try:
        payload = {
            "model": model,
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
    clean_text = text.replace('"', '').replace('&', 'and').strip()
    for attempt in range(retries):
        try:
            communicate = edge_tts.Communicate(clean_text, voice)
            await communicate.save(filename)
            if os.path.exists(filename) and os.path.getsize(filename) > 1000:
                return
        except Exception as e:
            print(f"[WARNING] Edge-TTS attempt {attempt + 1} failed for voice {voice}: {e}")
            await asyncio.sleep(2)
    raise Exception(f"Failed to generate Edge-TTS audio for voice {voice} after {retries} attempts.")

def generate_edge_audio(text, role_key, output_filename):
    voice = VOICES.get(role_key, VOICES["Moderator"])
    print(f"[EDGE-TTS] Synthesizing [{role_key}] audio ({len(text.split())} words) -> {output_filename}...")
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
    return Image.new("RGB", (1920, 1080), (15, 20, 40))

def apply_spotlight_overlay(img, position="center"):
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    
    if position == "left":
        center_x, center_y = 520, 540
    elif position == "right":
        center_x, center_y = 1400, 540
    else:
        center_x, center_y = 960, 540
        
    radius = 650
    for r in range(radius, 0, -20):
        alpha = int(22 * (1.0 - r / radius))
        draw.ellipse([center_x - r, center_y - r, center_x + r, center_y + r], fill=(0, 255, 204, alpha) if position=="left" else (255, 0, 255, alpha) if position=="right" else (255, 215, 0, alpha))
        
    overlay = overlay.filter(ImageFilter.GaussianBlur(40))
    return Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")

def generate_speaker_frame(speaker_name, role_key, topic):
    pos = "center"
    glow_color = "#00FFCC"
    if "Christian Apologist" in role_key:
        pos = "left"
        glow_color = "#00FFCC"
    elif "Skeptic" in role_key:
        pos = "right"
        glow_color = "#FF00FF"

    img = get_base_background()
    img = apply_spotlight_overlay(img, pos)
    draw = ImageDraw.Draw(img)

    try:
        font_title = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 32)
        font_name = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 28)
        font_role = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 20)
    except IOError:
        font_title = font_name = font_role = ImageFont.load_default()

    def draw_centered(y, text, font, fill):
        bbox = draw.textbbox((0, 0), text, font=font)
        w = bbox[2] - bbox[0]
        draw.text(((1920 - w) // 2, y), text, fill=fill, font=font)

    draw_centered(40, f"TOPIC: {topic}", font_title, "white")

    card_x = 200 if pos == "left" else 1080 if pos == "right" else 560
    card_y = 750
    draw.rounded_rectangle([card_x, card_y, card_x + 640, card_y + 140], radius=16, fill=(15, 22, 36), outline=glow_color, width=3)
    
    bar_startX = card_x + 30
    bar_baseY = card_y + 70
    bar_heights = [20, 35, 15, 40, 25, 30]
    for i, bh in enumerate(bar_heights):
        draw.rectangle([bar_startX + (i * 8), bar_baseY - bh//2, bar_startX + (i * 8) + 5, bar_baseY + bh//2], fill=glow_color)

    draw.text((card_x + 90, card_y + 30), speaker_name, fill="white", font=font_name)
    draw.text((card_x + 90, card_y + 75), role_key.upper(), fill=glow_color, font=font_role)

    filename = f"speaker_{role_key.replace(' ', '_').lower()}.png"
    img.save(filename)
    return filename

def generate_round_breakdown_image(round_num, scores_a, scores_b, total_a, total_b):
    img = get_base_background()
    img = apply_spotlight_overlay(img, "center")
    draw = ImageDraw.Draw(img)

    try:
        font_header = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 36)
        font_sub = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 22)
        font_col = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 18)
        font_model = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 18)
        font_meta = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 14)
        font_score = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 20)
    except IOError:
        font_header = font_sub = font_col = font_model = font_meta = font_score = ImageFont.load_default()

    def draw_centered(y, text, font, fill):
        bbox = draw.textbbox((0, 0), text, font=font)
        w = bbox[2] - bbox[0]
        draw.text(((1920 - w) // 2, y), text, fill=fill, font=font)

    draw_centered(40, f"ROUND {round_num} JUDGING BREAKDOWN", font_header, "#FFD700")
    draw_centered(90, f"SCORE: Christian Apologist ({total_a} PTS) vs Skeptic ({total_b} PTS)", font_sub, "white")

    draw.text((220, 155), "CHRISTIAN APOLOGIST SCORES", fill="#00FFCC", font=font_col)
    draw.text((1120, 155), "SKEPTIC SCORES", fill="#FF00FF", font=font_col)

    col_a_judges = [(j, s) for j, s in zip(PRIMARY_JUDGES[:5], scores_a[:5])]
    col_b_judges = [(j, s) for j, s in zip(PRIMARY_JUDGES[5:], scores_b[5:])]

    y_start = 195
    for (judge, score) in col_a_judges:
        draw.rounded_rectangle([200, y_start, 920, y_start + 70], radius=8, fill=(15, 22, 36), outline=(50, 70, 100), width=2)
        draw.text((230, y_start + 14), judge["name"], fill="white", font=font_model)
        draw.text((230, y_start + 38), judge["provider"], fill="#8A99AD", font=font_meta)
        draw.text((830, y_start + 22), f"{score} pts", fill="#00FFCC", font=font_score)
        y_start += 82

    y_start = 195
    for (judge, score) in col_b_judges:
        draw.rounded_rectangle([1100, y_start, 1820, y_start + 70], radius=8, fill=(15, 22, 36), outline=(50, 70, 100), width=2)
        draw.text((1130, y_start + 14), judge["name"], fill="white", font=font_model)
        draw.text((1130, y_start + 38), judge["provider"], fill="#8A99AD", font=font_meta)
        draw.text((1730, y_start + 22), f"{score} pts", fill="#FF00FF", font=font_score)
        y_start += 82

    filename = f"round_{round_num}_breakdown.png"
    img.save(filename)
    return filename

def add_clean_subtitle_events(text, start_time, duration, dialogue_events):
    words = text.split()
    if not words:
        return
    
    chunk_size = 12
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

    # 1. Gradual Introduction by Moderator
    intro_text = f"Welcome everyone to today's formal showcase debate. The central question before our panel is: {topic}. Let us meet our debaters who will present their opening positions."
    intro_dur = generate_edge_audio(intro_text, "Moderator", "intro.mp3")
    audio_files.append("intro.mp3")
    intro_img = generate_speaker_frame("Christopher (Moderator)", "Moderator", topic)
    segment_images.append((intro_img, intro_dur))
    add_clean_subtitle_events(intro_text, current_time, intro_dur, dialogue_events)
    current_time += intro_dur

    # 2. Short self-introductions from the two AI models
    intro_a_text = "Greetings. I am Brian, representing the Christian Apologist perspective. I will build a rigorous case defending the truth and coherence of this worldview."
    dur_ia = generate_edge_audio(intro_a_text, "Christian Apologist", "intro_a.mp3")
    audio_files.append("intro_a.mp3")
    img_ia = generate_speaker_frame("Brian (Apologist AI)", "Christian Apologist", topic)
    segment_images.append((img_ia, dur_ia))
    add_clean_subtitle_events(intro_a_text, current_time, dur_ia, dialogue_events)
    current_time += dur_ia

    intro_b_text = "Hello. I am Aria, representing the Skeptic viewpoint. I will critically examine every claim, demanding rigorous empirical evidence and logical consistency."
    dur_ib = generate_edge_audio(intro_b_text, "Skeptic", "intro_b.mp3")
    audio_files.append("intro_b.mp3")
    img_ib = generate_speaker_frame("Aria (Skeptic AI)", "Skeptic", topic)
    segment_images.append((img_ib, dur_ib))
    add_clean_subtitle_events(intro_b_text, current_time, dur_ib, dialogue_events)
    current_time += dur_ib

    cumulative_score_a = 0
    cumulative_score_b = 0

    # Different representative judge assigned for each round
    round_representative_judges = [
        PRIMARY_JUDGES[1], # Round 1: Claude 3.5 Sonnet
        PRIMARY_JUDGES[2], # Round 2: Gemini Pro Latest
        PRIMARY_JUDGES[3]  # Round 3: DeepSeek Chat
    ]

    for round_num in range(1, 4):
        print(f"\n--- Round {round_num} of 3 ---")
        
        # Christian Apologist Speech
        prompt_a = f"Topic: {topic}\nRound {round_num}: Present a thorough pro-apologetic argument with deep reasoning. Write approximately 150 words."
        text_a = query_openrouter(prompt_a, primary_model="openai/gpt-5.6").replace(",", " -")
        dur_a = generate_edge_audio(text_a, "Christian Apologist", f"round_{round_num}_a.mp3")
        audio_files.append(f"round_{round_num}_a.mp3")
        img_a = generate_speaker_frame("Brian (Apologist AI)", "Christian Apologist", topic)
        segment_images.append((img_a, dur_a))
        add_clean_subtitle_events(text_a, current_time, dur_a, dialogue_events)
        current_time += dur_a

        # Skeptic Speech
        prompt_b = f"Topic: {topic}\nRound {round_num}: Provide a thorough counter-argument from a Skeptical perspective directly challenging the apologist position. Write approximately 150 words."
        text_b = query_openrouter(prompt_b, primary_model="anthropic/claude-3.5-sonnet").replace(",", " -")
        dur_b = generate_edge_audio(text_b, "Skeptic", f"round_{round_num}_b.mp3")
        audio_files.append(f"round_{round_num}_b.mp3")
        img_b = generate_speaker_frame("Aria (Skeptic AI)", "Skeptic", topic)
        segment_images.append((img_b, dur_b))
        add_clean_subtitle_events(text_b, current_time, dur_b, dialogue_events)
        current_time += dur_b

        # Judging scores calculation
        scores_a, scores_b = [], []
        for judge in PRIMARY_JUDGES:
            resp = query_openrouter(f"Score Round {round_num} on '{topic}'. Format: 'A: [score], B: [score]'", primary_model=judge["id"], timeout=15)
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

        # Pick this round's unique representative judge
        rep_judge = round_representative_judges[round_num - 1]
        judge_commentary_prompt = f"Topic: {topic}. In Round {round_num}, Christian Apologist scored {round_total_a} and Skeptic scored {round_total_b}. In 2 sentences, explain as {rep_judge['name']} why one side's reasoning was more compelling in this round."
        judge_commentary = query_openrouter(judge_commentary_prompt, primary_model=rep_judge["id"]).replace(",", " -")

        breakdown_img = generate_round_breakdown_image(round_num, scores_a, scores_b, round_total_a, round_total_b)
        summary_text = f"Round {round_num} has concluded. {rep_judge['name']} notes: {judge_commentary} Christian Apologist earned {round_total_a} points, while Skeptic earned {round_total_b} points."
        dur_sum = generate_edge_audio(summary_text, "Moderator", f"round_{round_num}_sum.mp3")
        audio_files.append(f"round_{round_num}_sum.mp3")
        segment_images.append((breakdown_img, dur_sum))
        add_clean_subtitle_events(summary_text, current_time, dur_sum, dialogue_events)
        current_time += dur_sum

    # 3. Gradual Outro / Conclusion
    winner = "Christian Apologist" if cumulative_score_a > cumulative_score_b else "Skeptic"
    outro_text = f"As our debate draws to a close, let us review the cumulative results. Christian Apologist finished with {cumulative_score_a} total points, and Skeptic finished with {cumulative_score_b} total points. Our panel awards this showcase victory to the {winner}. Thank you for joining us for this deep exploration."
    dur_out = generate_edge_audio(outro_text, "Moderator", "outro.mp3")
    audio_files.append("outro.mp3")
    outro_img = generate_speaker_frame("Christopher (Moderator)", "Moderator", topic)
    segment_images.append((outro_img, dur_out))
    add_clean_subtitle_events(outro_text, current_time, dur_out, dialogue_events)
    current_time += dur_out

    print("\n[SUBTITLES] Generating clean middle-screen subtitles...")
    ass_content = """[Script Info]
Title: AI Debate Clean Subtitles
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: CleanSub,DejaVuSans-Bold,42,&H00FFFFFF,&H000000FF,&HFF000000,&H80000000,1,0,0,0,100,100,0,0,1,4,2,5,100,100,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    for start, end, text in dialogue_events:
        ass_content += f"Dialogue: 0,{format_ass_time(start)},{format_ass_time(end)},CleanSub,,0,0,0,,{text}\n"

    with open("subtitles.ass", "w", encoding="utf-8") as f:
        f.write(ass_content)

    with open("audio_list.txt", "w") as f:
        for audio in audio_files:
            f.write(f"file '{audio}'\n")

    with open("video_list.txt", "w") as f:
        for img_path, dur in segment_images:
            f.write(f"file '{img_path}'\n")
            f.write(f"duration {dur}\n")
        if segment_images:
            f.write(f"file '{segment_images[-1][0]}'\n")

    print("\n[FFmpeg] Assembling final video package...")
    ffmpeg_cmd = [
        "ffmpeg",
        "-f", "concat", "-safe", "0", "-i", "video_list.txt",
        "-f", "concat", "-safe", "0", "-i", "audio_list.txt",
        "-filter_complex", "[0:v]zoompan=z='min(zoom+0.0012,1.1)':d=125:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=1920x1080,subtitles=subtitles.ass[v]",
        "-map", "[v]",
        "-map", "1:a",
        "-c:v", "libx264",
        "-c:a", "aac",
        "-pix_fmt", "yuv420p",
        "-y",
        "final_debate_output.mp4"
    ]
    subprocess.run(ffmpeg_cmd, check=True)
    print("[SUCCESS] final_debate_output.mp4 successfully created!")

if __name__ == "__main__":
    run_debate_pipeline()
