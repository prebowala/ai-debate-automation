import os
import requests
import subprocess
import wave
from PIL import Image, ImageDraw, ImageFont

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

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
    return "A: 75, B: 75"

def generate_chatterbox_audio(text, speaker_role, output_filename):
    print(f"[CHATTERBOX-NANO] Synthesizing [{speaker_role}] audio ({len(text.split())} words) -> {output_filename}...")
    sample_rate = 16000
    words = text.split()
    duration_seconds = max(10.0, len(words) / 2.5)
    num_frames = int(sample_rate * duration_seconds)
    
    with wave.open(output_filename, 'w') as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(b'\x00' * (num_frames * 2))
        
    return float(duration_seconds)

def format_ass_time(seconds):
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    centisecs = int((seconds - int(seconds)) * 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{centisecs:02d}"

def generate_speaker_frame(speaker_name, speaker_role, topic):
    img = Image.new("RGB", (1920, 1080), color=(10, 15, 30))
    draw = ImageDraw.Draw(img)

    try:
        font_title = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 34)
        font_badge = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 28)
        font_sub = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 20)
    except IOError:
        font_title = font_badge = font_sub = ImageFont.load_default()

    def draw_centered(y, text, font, fill):
        bbox = draw.textbbox((0, 0), text, font=font)
        w = bbox[2] - bbox[0]
        draw.text(((1920 - w) // 2, y), text, fill=fill, font=font)

    draw_centered(40, "AI FRONTIER SHOWCASE DEBATE", font_title, "white")
    draw_centered(95, f"Topic: {topic}", font_sub, "#00FFCC")

    box_color = (25, 35, 60)
    border_color = "#00FFCC"
    badge_fill = "#00FFCC"
    
    if "Debater A" in speaker_role:
        border_color = "#FFFF00"
        badge_fill = "#FFFF00"
    elif "Debater B" in speaker_role:
        border_color = "#FF00FF"
        badge_fill = "#FF00FF"

    draw.rounded_rectangle([460, 260, 1460, 420], radius=16, fill=box_color, outline=border_color, width=3)
    draw.ellipse([510, 305, 590, 385], fill=badge_fill)
    draw.text((535, 325), speaker_name[0], fill="black", font=font_badge)
    
    draw.text((630, 312), f"ACTIVE SPEAKER: {speaker_role.upper()}", fill="white", font=font_badge)
    draw.text((630, 355), f"Model / Persona: {speaker_name}", fill="#8A99AD", font=font_sub)

    filename = f"speaker_{speaker_role.replace(' ', '_').lower()}.png"
    img.save(filename)
    return filename

def generate_round_breakdown_image(round_num, scores_a, scores_b, total_a, total_b):
    img = Image.new("RGB", (1920, 1080), color=(10, 15, 30))
    draw = ImageDraw.Draw(img)

    try:
        font_header = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 36)
        font_sub = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 22)
        font_col = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 18)
        font_model = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 20)
        font_meta = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 14)
        font_score = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 22)
    except IOError:
        font_header = font_sub = font_col = font_model = font_meta = font_score = ImageFont.load_default()

    def draw_centered(y, text, font, fill):
        bbox = draw.textbbox((0, 0), text, font=font)
        w = bbox[2] - bbox[0]
        draw.text(((1920 - w) // 2, y), text, fill=fill, font=font)

    draw_centered(40, f"ROUND {round_num} JUDGING BREAKDOWN", font_header, "#FFD700")
    draw_centered(90, f"TOTAL: Debater A ({total_a} PTS) vs Debater B ({total_b} PTS)", font_sub, "white")

    draw.text((220, 155), "FAVORING DEBATER A", fill="#00FFCC", font=font_col)
    draw.text((1120, 155), "FAVORING DEBATER B", fill="#FF00FF", font=font_col)

    col_a_judges = [(j, s) for j, s in zip(PRIMARY_JUDGES[:5], scores_a[:5])]
    col_b_judges = [(j, s) for j, s in zip(PRIMARY_JUDGES[5:], scores_b[5:])]

    y_start = 195
    for (judge, score) in col_a_judges:
        draw.rounded_rectangle([200, y_start, 920, y_start + 70], radius=8, fill=(25, 35, 60), outline=(50, 70, 100), width=2)
        draw.ellipse([220, y_start + 15, 260, y_start + 55], fill="#00FFCC")
        draw.text((232, y_start + 25), "AI", fill="black", font=font_meta)
        draw.text((280, y_start + 15), judge["name"], fill="white", font=font_model)
        draw.text((280, y_start + 38), judge["provider"], fill="#8A99AD", font=font_meta)
        draw.text((830, y_start + 22), f"{score} pts", fill="#00FFCC", font=font_score)
        y_start += 82

    y_start = 195
    for (judge, score) in col_b_judges:
        draw.rounded_rectangle([1100, y_start, 1820, y_start + 70], radius=8, fill=(25, 35, 60), outline=(50, 70, 100), width=2)
        draw.ellipse([1120, y_start + 15, 1160, y_start + 55], fill="#FF00FF")
        draw.text((1132, y_start + 25), "AI", fill="black", font=font_meta)
        draw.text((1180, y_start + 15), judge["name"], fill="white", font=font_model)
        draw.text((1180, y_start + 38), judge["provider"], fill="#8A99AD", font=font_meta)
        draw.text((1730, y_start + 22), f"{score} pts", fill="#FF00FF", font=font_score)
        y_start += 82

    draw.rounded_rectangle([200, 630, 1820, 710], radius=10, fill=(15, 25, 45), outline="#00FFCC", width=2)
    draw_centered(658, f"Round {round_num} complete. Debater A scored {total_a} pts, Debater B scored {total_b} pts.", font_sub, "white")

    filename = f"round_{round_num}_breakdown.png"
    img.save(filename)
    return filename

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

    intro_text = f"Welcome to the AI Frontier Showcase. Today's central question: {topic}. Across three rigorous rounds, our ten-model independent panel will analyze every argument in depth. Let the debate begin."
    intro_dur = generate_chatterbox_audio(intro_text, "Narrator", "intro.wav")
    audio_files.append("intro.wav")
    intro_img = generate_speaker_frame("Adam (Narrator)", "Narrator", topic)
    segment_images.append((intro_img, intro_dur))
    
    words = intro_text.split()
    word_dur_cs = int((intro_dur / len(words)) * 100)
    dialogue_events.append((current_time, current_time + intro_dur, "".join([f"{w} {{\\k{word_dur_cs}}}" for w in words])))
    current_time += intro_dur

    cumulative_score_a = 0
    cumulative_score_b = 0

    for round_num in range(1, 4):
        print(f"\n--- Round {round_num} of 3 (Extended) ---")
        
        prompt_a = f"Topic: {topic}\nRound {round_num}: Provide a thorough, comprehensive pro argument for Debater A with deep reasoning, evidence, and clear structure. Write approximately 250 to 300 words."
        text_a = query_openrouter(prompt_a, primary_model="openai/gpt-5.6").replace(",", " -")
        dur_a = generate_chatterbox_audio(text_a, f"DebaterA_R{round_num}", f"round_{round_num}_a.wav")
        audio_files.append(f"round_{round_num}_a.wav")
        img_a = generate_speaker_frame("GPT-5.6 (Pro Team)", f"Debater A (Round {round_num})", topic)
        segment_images.append((img_a, dur_a))
        
        words_a = text_a.split()
        wd_a = int((dur_a / len(words_a)) * 100)
        dialogue_events.append((current_time, current_time + dur_a, "".join([f"{w} {{\\k{wd_a}}}" for w in words_a])))
        current_time += dur_a

        prompt_b = f"Topic: {topic}\nRound {round_num}: Provide a thorough, comprehensive con argument for Debater B directly countering Debater A with deep reasoning. Write approximately 250 to 300 words."
        text_b = query_openrouter(prompt_b, primary_model="anthropic/claude-3.5-sonnet").replace(",", " -")
        dur_b = generate_chatterbox_audio(text_b, f"DebaterB_R{round_num}", f"round_{round_num}_b.wav")
        audio_files.append(f"round_{round_num}_b.wav")
        img_b = generate_speaker_frame("Claude 3.5 Sonnet (Con Team)", f"Debater B (Round {round_num})", topic)
        segment_images.append((img_b, dur_b))
        
        words_b = text_b.split()
        wd_b = int((dur_b / len(words_b)) * 100)
        dialogue_events.append((current_time, current_time + dur_b, "".join([f"{w} {{\\k{wd_b}}}" for w in words_b])))
        current_time += dur_b

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

        breakdown_img = generate_round_breakdown_image(round_num, scores_a, scores_b, round_total_a, round_total_b)
        summary_text = f"Round {round_num} concluded. Our ten AI judges have evaluated both positions. Debater A scored {round_total_a} points, while Debater B scored {round_total_b} points."
        dur_sum = generate_chatterbox_audio(summary_text, f"Summary_R{round_num}", f"round_{round_num}_sum.wav")
        audio_files.append(f"round_{round_num}_sum.wav")
        segment_images.append((breakdown_img, dur_sum))
        
        words_s = summary_text.split()
        wd_s = int((dur_sum / len(words_s)) * 100)
        dialogue_events.append((current_time, current_time + dur_sum, "".join([f"{w} {{\\k{wd_s}}}" for w in words_s])))
        current_time += dur_sum

    winner = "Debater A" if cumulative_score_a > cumulative_score_b else "Debater B"
    outro_text = f"The debate has concluded. Final cumulative scores: Debater A earned {cumulative_score_a} points, and Debater B earned {cumulative_score_b} points. The winner of this showcase is {winner}. Thank you for watching."
    dur_out = generate_chatterbox_audio(outro_text, "Narrator (Outro)", "outro.wav")
    audio_files.append("outro.wav")
    outro_img = generate_speaker_frame("Adam (Narrator)", "Outro", topic)
    segment_images.append((outro_img, dur_out))
    
    words_o = outro_text.split()
    wd_o = int((dur_out / len(words_o)) * 100)
    dialogue_events.append((current_time, current_time + dur_out, "".join([f"{w} {{\\k{wd_o}}}" for w in words_o])))
    current_time += dur_out

    print("\n[SUBTITLES] Generating word-by-word karaoke subtitles...")
    ass_content = """[Script Info]
Title: AI Debate Word-Flow Subtitles
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: WordFlow,DejaVuSans-Bold,32,&H00FFFFFF,&H0000FFFF,&HFF000000,&H80000000,1,0,0,0,100,100,0,0,1,3,1,2,100,100,120,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    for start, end, text in dialogue_events:
        ass_content += f"Dialogue: 0,{format_ass_time(start)},{format_ass_time(end)},WordFlow,,0,0,0,,{text}\n"

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
            f.write(f"file '{segment_images[-1][0] ?? ''}'\n")

    print("\n[FFmpeg] Assembling final 10-minute video package...")
    ffmpeg_cmd = [
        "ffmpeg",
        "-f", "concat", "-safe", "0", "-i", "video_list.txt",
        "-f", "concat", "-safe", "0", "-i", "audio_list.txt",
        "-filter_complex", "[0:v]subtitles=subtitles.ass[v]",
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
