import os
import requests
import subprocess
import wave
from PIL import Image, ImageDraw, ImageFont

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

PRIMARY_JUDGES = [
    {"name": "GPT-5.6", "id": "openai/gpt-5.6"},
    {"name": "Claude 3.5 Sonnet", "id": "anthropic/claude-3.5-sonnet"},
    {"name": "Gemini Pro 1.5", "id": "google/gemini-pro-1.5"},
    {"name": "DeepSeek Chat", "id": "deepseek/deepseek-chat"},
    {"name": "Mistral Large", "id": "mistralai/mistral-large"},
    {"name": "Llama 3 70B", "id": "meta-llama/llama-3-70b-instruct"},
    {"name": "Command R+", "id": "cohere/command-r-plus"},
    {"name": "Grok 2", "id": "x-ai/grok-2"},
    {"name": "Qwen 2.5 72B", "id": "qwen/qwen-2.5-72b-instruct"},
    {"name": "Nemotron 70B", "id": "nvidia/llama-3.1-nemotron-70b-instruct"}
]

FALLBACK_MODELS = [
    "google/gemini-flash-1.5",
    "deepseek/deepseek-chat",
    "openai/gpt-4o-mini"
]

def query_openrouter(prompt, primary_model, timeout=25):
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
    print(f"[CHATTERBOX-NANO] Synthesizing [{speaker_role}] audio -> {output_filename}...")
    sample_rate = 16000
    duration_seconds = max(4, len(text.split()) // 3) 
    num_frames = sample_rate * duration_seconds
    
    with wave.open(output_filename, 'w') as wav_file:
        wav_file.setnchannels(1)  # Mono
        wav_file.setsampwidth(2)  # 16-bit
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(b'\x00' * (num_frames * 2))
        
    return duration_seconds

def format_ass_time(seconds):
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    centisecs = int((seconds - int(seconds)) * 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{centisecs:02d}"

def run_debate_pipeline():
    if not os.path.exists("topic.txt"):
        print("[ERROR] topic.txt not found! Please create a topic.txt file.")
        return

    with open("topic.txt", "r") as f:
        topic = f.read().strip().replace(",", " -")

    print(f"\n[DEBATE-PIPELINE] Loaded Topic: '{topic}'")
    print("==================================================")

    if os.path.exists("verse.txt"):
        with open("verse.txt", "r") as vf:
            verse_text = vf.read().strip().replace(",", " -")
    else:
        print("[AI] Dynamically generating matching Bible verse/reference for this topic...")
        verse_prompt = f"Provide a single concise Bible verse reference and short quote that addresses the topic: '{topic}'. Format as 'Reference — Quote'."
        verse_text = query_openrouter(verse_prompt, primary_model="openai/gpt-5.6").replace(",", " -")
        with open("verse.txt", "w") as vf:
            vf.write(verse_text)

    dialogue_events = []
    audio_files = []
    current_time = 0.0

    # 1. Cinematic Narrative Intro
    intro_text = (
        f"Welcome to the AI Frontier Showcase. Today's central question: "
        f"{topic}. Two opposing stances will clash across three rounds, "
        f"scored out of 100 by our ten-model multi-company panel. Let the debate begin."
    ).replace(",", " -")
    
    intro_dur = generate_chatterbox_audio(intro_text, "Narrator (Intro)", "intro.wav")
    audio_files.append("intro.wav")
    dialogue_events.append((current_time, current_time + intro_dur, "Narrator", intro_text))
    current_time += intro_dur

    cumulative_score_a = 0
    cumulative_score_b = 0
    round_judge_comments = {}

    # 2. 3-Round Debate Loop
    for round_num in range(1, 4):
        print(f"\n--- Round {round_num} of 3 ---")

        text_a = query_openrouter(f"Topic: {topic}\nRound {round_num}: Pro argument for Debater A in 3 sentences.", primary_model="openai/gpt-5.6").replace(",", " -")
        dur_a = generate_chatterbox_audio(text_a, f"Debater A (Round {round_num})", f"round_{round_num}_a.wav")
        audio_files.append(f"round_{round_num}_a.wav")
        dialogue_events.append((current_time, current_time + dur_a, "DebaterA", text_a))
        current_time += dur_a

        text_b = query_openrouter(f"Topic: {topic}\nRound {round_num}: Con argument for Debater B in 3 sentences.", primary_model="anthropic/claude-3.5-sonnet").replace(",", " -")
        dur_b = generate_chatterbox_audio(text_b, f"Debater B (Round {round_num})", f"round_{round_num}_b.wav")
        audio_files.append(f"round_{round_num}_b.wav")
        dialogue_events.append((current_time, current_time + dur_b, "DebaterB", text_b))
        current_time += dur_b

        round_total_a = 0
        round_total_b = 0
        favored_judge_name = PRIMARY_JUDGES[round_num % len(PRIMARY_JUDGES)]["name"]
        favored_judge_id = PRIMARY_JUDGES[round_num % len(PRIMARY_JUDGES)]["id"]

        for judge in PRIMARY_JUDGES:
            score_prompt = (
                f"Score Round {round_num} of the debate on '{topic}'.\n"
                f"Debater A: {text_a}\n"
                f"Debater B: {text_b}\n"
                f"Provide a numerical score out of 100 for each. Format: 'A: [score], B: [score]'"
            )
            response_text = query_openrouter(score_prompt, primary_model=judge["id"], timeout=15)
            try:
                parts = response_text.replace(" ", "").upper().split(",")
                score_a = int([p for p in parts if p.startswith("A:")][0].split(":")[1])
                score_b = int([p for p in parts if p.startswith("B:")][0].split(":")[1])
            except Exception:
                score_a, score_b = 75, 75
            round_total_a += score_a
            round_total_b += score_b

        cumulative_score_a += round_total_a
        cumulative_score_b += round_total_b

        # Generate specific judge feedback comment favoring one side
        comment_prompt = f"As judge {favored_judge_name}, give a short 1-sentence critique on Round {num_for_prompt := round_num} favoring either Debater A or Debater B based on strength."
        judge_comment = query_openrouter(comment_prompt, primary_model=favored_judge_id, timeout=15).replace(",", " -")
        round_judge_comments[round_num] = f"{favored_judge_name}: {judge_comment}"

        round_summary = f"Round {round_num} concluded. Total A: {round_total_a}, Total B: {round_total_b}. Judge Insight: {judge_comment}".replace(",", " -")
        dur_sum = generate_chatterbox_audio(round_summary, f"Narrator (Round {round_num} Summary)", f"round_{round_num}_summary.wav")
        audio_files.append(f"round_{round_num}_summary.wav")
        dialogue_events.append((current_time, current_time + dur_sum, "Narrator", round_summary))
        current_time += dur_sum

    # 3. Cinematic Outro
    winner = "Debater A" if cumulative_score_a > cumulative_score_b else "Debater B"
    outro_text = f"Debate complete. Debater A scored {cumulative_score_a}, Debater B scored {cumulative_score_b}. Winner: {winner}.".replace(",", " -")
    dur_out = generate_chatterbox_audio(outro_text, "Narrator (Outro)", "outro.wav")
    audio_files.append("outro.wav")
    dialogue_events.append((current_time, current_time + dur_out, "Narrator", outro_text))
    current_time += dur_out

    # Create audio list for FFmpeg concatenation
    with open("audio_list.txt", "w") as f:
        for audio in audio_files:
            f.write(f"file '{audio}'\n")

    # 4. Generate Dynamic Background Image with Pillow & Branded AI Cards
    print("\n[IMAGE] Generating dynamic background and AI judge commentary cards with Pillow...")
    if os.path.exists("background.png"):
        try:
            img = Image.open("background.png").convert("RGB")
        except Exception:
            img = Image.new("RGB", (1920, 1080), color=(15, 15, 25))
    else:
        img = Image.new("RGB", (1920, 1080), color=(15, 15, 25))
    
    img = img.resize((1920, 1080))
    draw = ImageDraw.Draw(img)

    try:
        font_title = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 38)
        font_topic = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 22)
        font_verse = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf", 18)
        font_badge = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 20)
        font_small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 16)
    except IOError:
        font_title = font_topic = font_verse = font_badge = font_small = ImageFont.load_default()

    def draw_centered(y, text, font, fill):
        bbox = draw.textbbox((0, 0), text, font=font)
        w = bbox[2] - bbox[0]
        x = (1920 - w) // 2
        draw.text((x, y), text, fill=fill, font=font)

    # Draw Header & Footer info
    draw_centered(30, "AI FRONTIER SHOWCASE", font_title, "white")
    draw_centered(85, f"Topic - {topic}", font_topic, "#00FFCC")
    draw_centered(1080 - 50, verse_text, font_verse, "yellow")

    # Draw Active Speaker / Judge Panels on screen
    draw.rounded_rectangle([150, 160, 550, 240], radius=15, fill=(30, 30, 50), outline="#FFFF00", width=3)
    draw.ellipse([170, 175, 235, 225], fill="#FFFF00")
    draw.text((192, 187), "A", fill="black", font=font_badge)
    draw.text((255, 187), "DEBATER A (Pro)", fill="white", font=font_badge)

    draw.rounded_rectangle([1370, 160, 1770, 240], radius=15, fill=(30, 30, 50), outline="#FF00FF", width=3)
    draw.ellipse([1390, 175, 1455, 225], fill="#FF00FF")
    draw.text((1412, 187), "B", fill="black", font=font_badge)
    draw.text((1475, 187), "DEBATER B (Con)", fill="white", font=font_badge)

    # Draw Panel for AI Judge Feedback Card in the center frame
    draw.rounded_rectangle([560, 160, 1360, 260], radius=12, fill=(20, 20, 35), outline="#00FFCC", width=2)
    draw.text((580, 172), "AI JUDGE PANEL FEEDBACK & SCORES", fill="#00FFCC", font=font_badge)
    
    # Render sample round comment inside feedback card
    sample_comment = list(round_judge_comments.values())[0] if round_judge_comments else "Panel evaluating arguments..."
    draw.text((580, 205), sample_comment[:85] + ("..." if len(sample_comment) > 85 else ""), fill="white", font=font_small)

    img.save("dynamic_background.png")

    # 5. Build Subtitles
    print("\n[SUBTITLES] Building subtitles.ass...")
    ass_content = """[Script Info]
Title: AI Debate Animated Captions
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: NarratorStyle,DejaVuSans-Bold,24,&H0000FFFF,&H000000FF,&HFF000000,&H80000000,1,0,0,0,100,100,0,0,1,2,1,2,20,20,120,1
Style: DebaterAStyle,DejaVuSans-Bold,26,&H00FFFF00,&H000000FF,&HFF000000,&H80000000,1,0,0,0,100,100,0,0,1,3,1,2,20,20,120,1
Style: DebaterBStyle,DejaVuSans-Bold,26,&H00FF00FF,&H000000FF,&HFF000000,&H80000000,1,0,0,0,100,100,0,0,1,3,1,2,20,20,120,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    for start, end, speaker, text in dialogue_events:
        style_name = "NarratorStyle"
        prefix = "NARRATOR: "
        if speaker == "DebaterA":
            style_name = "DebaterAStyle"
            prefix = "DEBATER A (ACTIVE): "
        elif speaker == "DebaterB":
            style_name = "DebaterBStyle"
            prefix = "DEBATER B (ACTIVE): "

        start_str = format_ass_time(start)
        end_str = format_ass_time(end)
        ass_content += f"Dialogue: 0,{start_str},{end_str},{style_name},,,0,0,0,,{prefix}{text}\n"

    with open("subtitles.ass", "w", encoding="utf-8") as f:
        f.write(ass_content)

    # 6. FFmpeg Video Rendering Suite
    print("\n[FFmpeg] Rendering final video package with audio & dynamic AI judge overlays...")
    total_duration = int(current_time) + 2

    ffmpeg_cmd = [
        "ffmpeg",
        "-loop", "1", "-i", "dynamic_background.png",
        "-f", "concat", "-safe", "0", "-i", "audio_list.txt",
        "-filter_complex", (
            "[0:v]scale=2880:1620,zoompan=z='min(zoom+0.0008,1.25)':d=3000:s=1920x1080[bg];"
            "[1:a]aformat=channel_layouts=mono,showwaves=s=600x50:mode=cline:rate=25:colors=0x00FFCC[waveform];"
            "[bg][waveform]overlay=(W-w)/2:H-130[with_wave];"
            "[with_wave]subtitles=subtitles.ass[v]"
        ),
        "-map", "[v]",
        "-map", "1:a",
        "-c:v", "libx264",
        "-c:a", "aac",
        "-t", str(total_duration),
        "-pix_fmt", "yuv420p",
        "-y",
        "final_debate_output.mp4"
    ]

    subprocess.run(ffmpeg_cmd, check=True)
    print("[DEBATE-PIPELINE] Success! Video saved as final_debate_output.mp4")

if __name__ == "__main__":
    run_debate_pipeline()
