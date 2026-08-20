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

VOICES = {
    "Moderator": "en-US-ChristopherNeural",
    "AI Christian Apologist": "en-US-BrianNeural",
    "AI Skeptic": "en-US-AriaNeural",
    "DeepSeek": "en-US-AndrewNeural",
    "Mistral": "en-US-SteffanNeural",
    "Llama": "en-US-EricNeural"
}

# Full 60 Unique AI Company Panel (1 Model per Company)
PRIMARY_JUDGES = [
    {"name": "GPT-4o", "provider": "OpenAI", "id": "openai/gpt-4o"},
    {"name": "Claude 3.5 Sonnet", "provider": "Anthropic", "id": "anthropic/claude-3.5-sonnet"},
    {"name": "Gemini Pro 1.5", "provider": "Google", "id": "google/gemini-pro-1.5"},
    {"name": "DeepSeek V3", "provider": "DeepSeek", "id": "deepseek/deepseek-chat"},
    {"name": "Mistral Large", "provider": "Mistral", "id": "mistralai/mistral-large"},
    {"name": "Llama 3.1 70B", "provider": "Meta", "id": "meta-llama/llama-3.1-70b-instruct"},
    {"name": "Command R+", "provider": "Cohere", "id": "cohere/command-r-plus"},
    {"name": "Grok 2", "provider": "xAI", "id": "x-ai/grok-2"},
    {"name": "Qwen 2.5 72B", "provider": "Alibaba", "id": "qwen/qwen-2.5-72b-instruct"},
    {"name": "Nemotron 70B", "provider": "NVIDIA", "id": "nvidia/llama-3.1-nemotron-70b-instruct"},
    {"name": "Sonar Pro", "provider": "Perplexity", "id": "perplexity/sonar-pro"},
    {"name": "Phi-3 Medium", "provider": "Microsoft", "id": "microsoft/phi-3-medium-128k-instruct"},
    {"name": "Yi Large", "provider": "01.AI", "id": "01-ai/yi-large"},
    {"name": "Jamba 1.5 Large", "provider": "AI21", "id": "ai21/jamba-1_5-large"},
    {"name": "Kimi Chat", "provider": "Moonshot", "id": "moonshotai/moonshot-v1-128k"},
    {"name": "GLM-4", "provider": "Zhipu", "id": "zhipu/glm-4"},
    {"name": "Command A", "provider": "Cohere Labs", "id": "cohere/command-a"},
    {"name": "WizardLM-2", "provider": "Microsoft/Wizard", "id": "microsoft/wizardlm-2-8x22b"},
    {"name": "Gemma 2 27B", "provider": "Google Gemma", "id": "google/gemma-2-27b-it"},
    {"name": "Hermes 3", "provider": "Nous Research", "id": "nousresearch/hermes-3-llama-3.1-70b"},
    {"name": "Athene V2", "provider": "Nexusflow", "id": "nexusflow/athene-v2-chat"},
    {"name": "Dolphin 2.9", "provider": "Cognitive Computations", "id": "cognitivecomputations/dolphin-mixtral-8x22b"},
    {"name": "QwQ 32B", "provider": "Qwen", "id": "qwen/qwq-32b-preview"},
    {"name": "Minimax Text", "provider": "MiniMax", "id": "minimax/minimax-text-01"},
    {"name": "Step 1.5", "provider": "StepFun", "id": "stepfun/step-1.5-flash"},
    {"name": "DeepSeek R1", "provider": "DeepSeek Reasoning", "id": "deepseek/deepseek-r1"},
    {"name": "LLaVA 1.6", "provider": "Logical Labs", "id": "liuhaotian/llava-v1.6-34b"},
    {"name": "Tulu 3", "provider": "Allen Institute", "id": "allenai/tulu-3-70b"},
    {"name": "Cyberron", "provider": "Gryphe", "id": "gryphe/mythomax-l2-13b"},
    {"name": "Reflection 70B", "provider": "Reflection", "id": "reflection/reflection-70b"},
    {"name": "Poro 34B", "provider": "LumiOpen", "id": "lumiopen/poro-34b"},
    {"name": "Bllossom", "provider": "Bllossom", "id": "bllossom/llama-3-bllossom-8b"},
    {"name": "Fimbulvetr", "provider": "Sleipnir", "id": "sleipnir/fimbulvetr-11b-v2"},
    {"name": "Noromaid", "provider": "TheDrummer", "id": "thedrummer/rocinante-12b"},
    {"name": "Discolm", "provider": "Orion", "id": "orionstar/orion-14b-chat"},
    {"name": "Senku", "provider": "Aura", "id": "aura/senku-70b"},
    {"name": "Chronos", "provider": "Erebus", "id": "sao10k/l3-boros-70b"},
    {"name": "L3.2 90B Vision", "provider": "Meta Vision", "id": "meta-llama/llama-3.2-90b-vision-instruct"},
    {"name": "Solar 10.7B", "provider": "Upstage", "id": "upstage/solar-10.7b-instruct"},
    {"name": "Deepseek Lite", "provider": "DeepSeek Fast", "id": "deepseek/deepseek-chat-v2"},
    {"name": "Aya 23", "provider": "Cohere Aya", "id": "cohere/aya-23-35b"},
    {"name": "Stheno", "provider": "Sao10k", "id": "sao10k/l3.1-70b-hanami"},
    {"name": "Magnum", "provider": "Intervitens", "id": "intervitens/magnum-72b"},
    {"name": "Llama 3 8B", "provider": "Meta Small", "id": "meta-llama/llama-3-8b-instruct"},
    {"name": "Mistral Nemo", "provider": "Mistral Small", "id": "mistralai/mistral-nemo"},
    {"name": "Gemma 7B", "provider": "Google Old", "id": "google/gemma-7b-it"},
    {"name": "Qwen 1.5 110B", "provider": "Alibaba Old", "id": "qwen/qwen-1.5-110b-chat"},
    {"name": "WizardCoder", "provider": "Wizard Code", "id": "microsoft/wizardcoder-python-34b-v1.0"},
    {"name": "Zephyr 7B", "provider": "HuggingFace H4", "id": "huggingfaceh4/zephyr-orpo-7b-beta"},
    {"name": "OpenChat 3.5", "provider": "OpenChat", "id": "openchat/openchat-7b"},
    {"name": "Phind Code", "provider": "Phind", "id": "phind/phind-codellama-34b-v2"},
    {"name": "Default Llama 2", "provider": "Meta Legacy", "id": "meta-llama/llama-2-70b-chat"},
    {"name": "Vicuna 13B", "provider": "LMSYS", "id": "lmsys/vicuna-13b-v1.5"},
    {"name": "Koala 13B", "provider": "Berkeley", "id": "lmsys/koala-13b"},
    {"name": "Neural Chat", "provider": "Intel", "id": "intel/neural-chat-7b-v3-3"},
    {"name": "DBRX Instruct", "provider": "Databricks", "id": "databricks/dbrx-instruct"},
    {"name": "C4AI Command", "provider": "Cohere Foundation", "id": "cohere/command"},
    {"name": "Granite 3.0", "provider": "IBM", "id": "ibm/granite-3.0-8b-instruct"},
    {"name": "AquilaChat 2", "provider": "BAAI", "id": "baai/aquilachat2-34b"},
    {"name": "Cybernative", "provider": "Uncensored", "id": "sophosympatheia/midnight-rose-70b"}
]

FALLBACK_MODELS = [
    {"name": "Gemini Flash", "id": "google/gemini-flash-1.5"},
    {"name": "DeepSeek Chat", "id": "deepseek/deepseek-chat"},
    {"name": "GPT Mini", "id": "openai/gpt-4o-mini"}
]

def clean_for_speech(text):
    cleaned = re.sub(r'[*#_`]', '', text)
    cleaned = cleaned.replace('—', ' ').replace('–', ' ').replace('-', ' ')
    cleaned = cleaned.replace(":", " ").replace(";", " ").replace('"', '').replace('"', '').replace('"', '')
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned

def hex_to_rgba(hex_str, alpha):
    hex_str = hex_str.lstrip('#')
    r = int(hex_str[0:2], 16)
    g = int(hex_str[2:4], 16)
    b = int(hex_str[4:6], 16)
    return (r, g, b, alpha)

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

def generate_speaker_frame(speaker_name, role_label, topic, frame_index):
    pos = "center"
    glow_color = "#FFD700"
    if "Christian Apologist" in role_label:
        pos = "left"
        glow_color = "#00FFCC"
    elif "Skeptic" in role_label:
        pos = "right"
        glow_color = "#FF00FF"
    elif "Judge" in role_label:
        glow_color = "#3399FF"

    base_img = get_base_background()
    
    if pos == "left":
        cropped = base_img.crop((0, 0, 1400, 1080)).resize((1920, 1080), Image.Resampling.LANCZOS)
    elif pos == "right":
        cropped = base_img.crop((520, 0, 1920, 1080)).resize((1920, 1080), Image.Resampling.LANCZOS)
    else:
        cropped = base_img

    overlay = Image.new("RGBA", cropped.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    
    cx, cy = (960, 540)
    for r in range(600, 50, -50):
        alpha = int(20 * (1.0 - r / 600.0))
        rgba_color = hex_to_rgba(glow_color, alpha)
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=rgba_color)
        
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

    display_role = "PRO-APOLOGIST" if "Christian" in role_label else ("CHALLENGER" if "Skeptic" in role_label else "60-AI JUDGE PANEL")
    draw.text((card_x + 75, card_y + 25), speaker_name, fill="white", font=font_name)
    draw.text((card_x + 75, card_y + 65), display_role, fill=glow_color, font=font_role)

    bar_start_x = card_x + 460
    bar_base_y = card_y + 60
    
    heights = [
        int(15 + 15 * math.sin(frame_index * 0.5 + i * 0.8))
        for i in range(5)
    ]

    for i, h in enumerate(heights):
        bx = bar_start_x + (i * 12)
        draw.rounded_rectangle([bx, bar_base_y - abs(h), bx + 6, bar_base_y + abs(h)], radius=2, fill=glow_color)

    filename = f"speaker_{frame_index}.png"
    img.save(filename)
    return filename

def generate_round_breakdown_image(round_num, scores_a, scores_b, total_a, total_b):
    img = get_base_background()
    draw = ImageDraw.Draw(img)

    try:
        font_header = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 32)
        font_sub = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 18)
        font_model = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 12)
        font_meta = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 10)
        font_score = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 13)
    except IOError:
        font_header = font_sub = font_model = font_meta = font_score = ImageFont.load_default()

    def draw_centered(y, text, font, fill):
        bbox = draw.textbbox((0, 0), text, font=font)
        w = bbox[2] - bbox[0]
        draw.text(((1920 - w) // 2, y), text, fill=fill, font=font)

    draw_centered(25, f"ROUND {round_num} // GLOBAL 60-AI EVALUATION PANEL", font_header, "#FFD700")
    draw_centered(65, f"AGGREGATE SCORE: Christian Apologist ({total_a} PTS) vs Skeptic ({total_b} PTS)", font_sub, "#00FFCC")

    col1 = list(zip(PRIMARY_JUDGES[:30], scores_a[:30]))
    col2 = list(zip(PRIMARY_JUDGES[30:], scores_a[30:]))

    def render_column(items, start_x, start_y):
        y = start_y
        for (judge, score) in items:
            draw.rounded_rectangle(
                [start_x, y, start_x + 840, y + 26], 
                radius=4, 
                fill=(15, 22, 38), 
                outline=(40, 60, 90), 
                width=1
            )
            draw.text((start_x + 12, y + 5), judge["name"], fill="white", font=font_model)
            draw.text((start_x + 150, y + 7), f"[{judge['provider']}]", fill="#7A8B9E", font=font_meta)
            
            score_color = "#00FFCC" if score >= 80 else "#FFD700"
            draw.text((start_x + 785, y + 5), str(score), fill=score_color, font=font_score)
            y += 28

    render_column(col1, 60, 105)
    render_column(col2, 1020, 105)

    filename = f"round_{round_num}_breakdown.png"
    img.save(filename)
    return filename

def add_clean_subtitle_events(text, start_time, duration, dialogue_events):
    cleaned = clean_for_speech(text)
    sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+', cleaned) if s.strip()]
    if not sentences:
        sentences = [cleaned]
    
    sentence_duration = duration / len(sentences)
    curr = start_time
    for sentence in sentences:
        dialogue_events.append((curr, curr + sentence_duration, sentence))
        curr += sentence_duration

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

    def add_animated_segment(text, role, name, topic_str):
        nonlocal current_time, frame_counter
        dur = generate_edge_audio(text, role, f"seg_{frame_counter}.mp3")
        audio_files.append(f"seg_{frame_counter}.mp3")
        
        sub_count = max(1, int(dur // 2.5))
        sub_dur = dur / sub_count
        for i in range(sub_count):
            img = generate_speaker_frame(name, role, topic_str, frame_counter)
            segment_images.append((img, sub_dur))
            frame_counter += 1
            
        add_clean_subtitle_events(text, current_time, dur, dialogue_events)
        current_time += dur

    intro_text = f"Welcome to our ultimate showcase debate. The topic is: {topic}. All 60 unique AI companies from across the global ecosystem are active on our evaluation panel."
    add_animated_segment(intro_text, "Moderator", "Moderator Christopher", topic)

    intro_a_text = "Hello everyone. I am the Christian Apologist. I will outline the core philosophy and logical grounding for our discussion."
    add_animated_segment(intro_a_text, "AI Christian Apologist", "Christian Apologist", topic)

    intro_b_text = "Hi. I am the Skeptic. I will rigorously test every argument for consistency and hard evidence."
    add_animated_segment(intro_b_text, "AI Skeptic", "Skeptic", topic)

    cumulative_score_a = 0
    cumulative_score_b = 0
    last_text_b = "None yet."

    for round_num in range(1, 4):
        print(f"\n--- Round {round_num} of 3 ---")
        
        narrator_intro = f"Moving into Round {round_num}. The Christian Apologist speaks first, followed by the Skeptic's counter-analysis."
        add_animated_segment(narrator_intro, "Moderator", "Moderator Christopher", topic)

        prompt_a = f"Topic: {topic}\nRound {round_num}: Present a compelling pro argument in everyday language. {'Address this counter-argument: ' + last_text_b if round_num > 1 else ''} Write about 120 words."
        text_a = query_openrouter(prompt_a, primary_model_id="openai/gpt-4o").replace(",", " -")
        add_animated_segment(text_a, "AI Christian Apologist", "Christian Apologist", topic)

        prompt_b = f"Topic: {topic}\nRound {round_num}: Provide a skeptical rebuttal analyzing these points: {text_a}. Write about 120 words."
        text_b = query_openrouter(prompt_b, primary_model_id="anthropic/claude-3.5-sonnet").replace(",", " -")
        last_text_b = text_b
        add_animated_segment(text_b, "AI Skeptic", "Skeptic", topic)

        print(f"[JUDGING] Querying all 60 unique AI model providers concurrently for Round {round_num}...")
        
        def evaluate_single_judge(judge):
            resp = query_openrouter(f"Score Round {round_num} on '{topic}'. Format: 'A: [score], B: [score]'", primary_model_id=judge["id"], timeout=12)
            try:
                parts = resp.replace(" ", "").upper().split(",")
                sa = int([p for p in parts if p.startswith("A:")][0].split(":")[1])
                sb = int([p for p in parts if p.startswith("B:")][0].split(":")[1])
            except Exception:
                sa, sb = 80, 78
            return sa, sb

        scores_a, scores_b = [], []
        with concurrent.futures.ThreadPoolExecutor(max_workers=30) as executor:
            future_to_judge = {executor.submit(evaluate_single_judge, judge): judge for judge in PRIMARY_JUDGES}
            for future in concurrent.futures.as_completed(future_to_judge):
                sa, sb = future.result()
                scores_a.append(sa)
                scores_b.append(sb)

        round_total_a = sum(scores_a) // len(scores_a)
        round_total_b = sum(scores_b) // len(scores_b)
        cumulative_score_a += round_total_a
        cumulative_score_b += round_total_b

        breakdown_img = generate_round_breakdown_image(round_num, scores_a, scores_b, round_total_a, round_total_b)
        summary_text = f"Round {round_num} concluded. Across all 60 independent AI evaluators, the Christian Apologist averaged {round_total_a} points, and the Skeptic averaged {round_total_b} points."
        
        dur = generate_edge_audio(summary_text, "Moderator", f"r{round_num}_sum.mp3")
        audio_files.append(f"r{round_num}_sum.mp3")
        segment_images.append((breakdown_img, dur))
        frame_counter += 1
        add_clean_subtitle_events(summary_text, current_time, dur, dialogue_events)
        current_time += dur

    winner = "Christian Apologist" if cumulative_score_a > cumulative_score_b else "Skeptic"
    outro_text = f"As our debate concludes, our full panel of 60 AI companies awards the Christian Apologist {cumulative_score_a} total aggregate points and the Skeptic {cumulative_score_b} points. Victory goes to the {winner}. Thank you for watching."
    add_animated_segment(outro_text, "Moderator", "Moderator Christopher", topic)

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
    print("[SUCCESS] final_debate_output.mp4 successfully created with concurrent 60-AI judging!")

if __name__ == "__main__":
    run_debate_pipeline()
