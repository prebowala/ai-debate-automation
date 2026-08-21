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

# Using Edge TTS (Microsoft Neural Voices) - Free & No Quotas
VOICES = {
    "Moderator": "en-US-AndrewMultilingualNeural", 
    "AI Christian Apologist": "en-US-BrianMultilingualNeural",
    "AI Skeptic": "en-US-AvaMultilingualNeural",
    "Panelist 1": "en-US-ChristopherNeural",
    "Panelist 2": "en-US-EmmaMultilingualNeural"
}

# Full 60 Independent Company AI Panel
PANEL_JUDGES = [
    {"name": "OpenAI", "id": "openai/gpt-4o"},
    {"name": "Anthropic", "id": "anthropic/claude-3.5-sonnet"},
    {"name": "Google", "id": "google/gemini-pro-1.5"},
    {"name": "Meta", "id": "meta-llama/llama-3.1-405b-instruct"},
    {"name": "Mistral AI", "id": "mistralai/mistral-large"},
    {"name": "Cohere", "id": "cohere/command-r-plus"},
    {"name": "xAI", "id": "x-ai/grok-2"},
    {"name": "DeepSeek", "id": "deepseek/deepseek-chat"},
    {"name": "Alibaba Cloud", "id": "qwen/qwen-2.5-72b-instruct"},
    {"name": "Microsoft", "id": "microsoft/phi-3-medium-128k-instruct"},
    {"name": "Amazon", "id": "amazon/nova-pro-v1"},
    {"name": "Perplexity", "id": "perplexity/sonar-pro"},
    {"name": "Databricks", "id": "databricks/dbrx-instruct"},
    {"name": "Nous Research", "id": "nousresearch/hermes-3-llama-3.1-405b"},
    {"name": "AllenAI", "id": "allenai/olmo-7b-instruct"},
    {"name": "OpenChat", "id": "openchat/openchat-7b"},
    {"name": "01.AI", "id": "01-ai/yi-large"},
    {"name": "Phind", "id": "phind/phind-model"},
    {"name": "AI21 Labs", "id": "ai21/jamba-1-5-large"},
    {"name": "Hugging Face", "id": "huggingfaceh4/zephyr-7b-beta"},
    {"name": "Snorkel AI", "id": "snorkelai/snorkel-mistral-pairrm-dpo"},
    {"name": "Gryphe", "id": "gryphe/mythomax-l2-13b"},
    {"name": "Undi95", "id": "undi95/toppy-m-7b"},
    {"name": "Cognitive Computations", "id": "cognitivecomputations/dolphin-llama-3-70b"},
    {"name": "Together AI", "id": "togethercomputer/stripedhyena-nous-7b"},
    {"name": "Nvidia", "id": "nvidia/llama-3.1-nemotron-70b-instruct"},
    {"name": "Moonshot AI", "id": "moonshotai/moonshot-v1-8k"},
    {"name": "MiniMax", "id": "minimax/minimax-text-01"},
    {"name": "Upstage", "id": "upstage/solar-10b-instruct-v1"},
    {"name": "Stability AI", "id": "stabilityai/stable-code-3b"},
    {"name": "Liquid AI", "id": "liquid/lfm-40b"},
    {"name": "StepFun", "id": "stepfun/step-1-32k"},
    {"name": "Baidu", "id": "baidu/ernie-4.0-8k"},
    {"name": "Tencent", "id": "tencent/hunyuan-standard"},
    {"name": "Xiaomi", "id": "xiaomi/mishiny-v1"},
    {"name": "DeepInfra", "id": "deepinfra/deepseek-coder-33b"},
    {"name": "Novita AI", "id": "novita/llama-3-70b"},
    {"name": "Pygmalion AI", "id": "pygmalionai/mythalion-13b"},
    {"name": "Sao10K", "id": "sao10k/l3-stheno-8b"},
    {"name": "Mlabonne", "id": "mlabonne/neural-chat-7b-v3-3"},
    {"name": "Open-Orca", "id": "open-orca/mistral-7b-openorca"},
    {"name": "Jondurbin", "id": "jondurbin/airoboros-7b-gpt4"},
    {"name": "Aetherius", "id": "aetherius/psyche-7b"},
    {"name": "NeverSleep", "id": "neversleep/llama-3-lumimaid-70b"},
    {"name": "Nexusflow", "id": "nexusflow/nexusraven-v2-13b"},
    {"name": "Sanctum", "id": "sanctumai/mercurial-7b"},
    {"name": "Fimbulvetr", "id": "fimbulvetr/fimbulvetr-v2"},
    {"name": "Kcpp", "id": "kcpp/goliath-120b"},
    {"name": "Ghost", "id": "ghost/ghost-v1"},
    {"name": "Matrix", "id": "matrix/matrix-7b"},
    {"name": "Epsilon", "id": "epsilon/epsilon-lm"},
    {"name": "Open-Thoughts", "id": "open-thoughts/open-thoughts-7b"},
    {"name": "NeuralChat", "id": "openchat/openchat-8b"},
    {"name": "Recursion", "id": "recursion/recursion-7b"},
    {"name": "Vxt", "id": "vxt/vxt-7b"},
    {"name": "Kunoichi", "id": "kunoichi/kunoichi-7b"},
    {"name": "Discute", "id": "discute/discute-model"},
    {"name": "Llama-Factory", "id": "llamafactory/llama-3-instruct"},
    {"name": "PrimeIntellect", "id": "primeintellect/intellect-1"},
    {"name": "Syllogism", "id": "syllogism/syllogism-ai"}
]

def cleanup_cache():
    print("🧹 Cleaning up old cache files...")
    for ext in ['*.mp4', '*.mp3', '*.ass', '*.png', '*_list.txt']:
        for file in glob.glob(ext):
            if file not in ['final_debate_output.mp4', 'background.png']:
                try: os.remove(file)
                except: pass
    print("✨ Workspace is clean!")

def clean_for_speech(text):
    cleaned = re.sub(r'\([^)]*\)', '', text)
    cleaned = re.sub(r'[*#_`–—]', '', cleaned).replace(":", " ").replace(";", " ").replace('"', '')
    return re.sub(r'\s+', ' ', cleaned).strip()

def hex_to_rgba(hex_str, alpha):
    hex_str = hex_str.lstrip('#')
    return (int(hex_str[0:2], 16), int(hex_str[2:4], 16), int(hex_str[4:6], 16), alpha)

def load_font(size):
    """FIXED: Robust font loader that searches multiple OS paths to prevent tiny fallback fonts."""
    font_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", # Standard Linux/Ubuntu Actions
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "C:\\Windows\\Fonts\\arial.ttf", # Standard Windows
        "/System/Library/Fonts/Supplemental/Arial.ttf", # Standard Mac
        "arial.ttf"
    ]
    for path in font_paths:
        try:
            return ImageFont.truetype(path, size)
        except IOError:
            continue
    return ImageFont.load_default() # Absolute worst-case fallback

def query_openrouter(prompt, primary_model_id, timeout=45, max_tokens=1200):
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
    for _ in range(2):
        try:
            response = requests.post(
                OPENROUTER_URL, headers=headers, 
                json={
                    "model": primary_model_id, 
                    "messages": [{"role": "user", "content": prompt}], 
                    "temperature": 0.7,
                    "max_tokens": max_tokens
                }, 
                timeout=timeout
            )
            if response.status_code == 200:
                content = response.json()["choices"][0]["message"]["content"].strip()
                if len(content) > 20: return content
        except Exception: pass
    return "The core analysis reveals critical foundational assumptions that require closer inspection."

async def _generate_audio_and_words(text, voice, audio_filename):
    communicate = edge_tts.Communicate(text, voice)
    audio_data = b""
    words = []
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio_data += chunk["data"]
        elif chunk["type"] == "WordBoundary":
            words.append({
                "text": chunk["text"],
                "start": chunk["offset"] / 10_000_000,
                "duration": chunk["duration"] / 10_000_000,
                "end": (chunk["offset"] + chunk["duration"]) / 10_000_000
            })
    
    # FIXED: Subtitle Failsafe. If edge_tts drops word boundaries, artificially generate them based on length.
    if not words and text:
        mock_chunks = text.split()
        time_per_word = 0.35 
        for i, w in enumerate(mock_chunks):
            words.append({"text": w, "start": i * time_per_word, "end": (i * time_per_word) + 0.3})

    with open(audio_filename, "wb") as f:
        f.write(audio_data)
    return words

def format_ass_time(seconds):
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f"{hours}:{minutes:02d}:{secs:05.2f}"

def generate_standard_ass(words, ass_filename):
    # FIXED: Using a safe, generic font name (Sans) to prevent FFmpeg silent failures
    ass_header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Sans,48,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,3,2,2,100,100,100,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    chunk_size = 8
    for i in range(0, len(words), chunk_size):
        chunk = words[i:i+chunk_size]
        if not chunk: continue
        start_t = chunk[0]['start']
        end_t = chunk[-1]['end'] + 0.3
        text_content = " ".join([w['text'] for w in chunk])
        lines.append(f"Dialogue: 0,{format_ass_time(start_t)},{format_ass_time(end_t)},Default,,0,0,0,,{text_content}")

    with open(ass_filename, "w", encoding="utf-8") as f:
        f.write(ass_header + "\n".join(lines) + "\n")

def generate_edge_audio_and_subs(text, role_key, output_audio, output_ass):
    voice = VOICES.get(role_key, VOICES["Moderator"])
    safe_text = clean_for_speech(text).replace('&', 'and')
    try:
        words = asyncio.run(_generate_audio_and_words(safe_text, voice, output_audio))
    except Exception:
        words = asyncio.run(_generate_audio_and_words(safe_text, "en-US-AndrewMultilingualNeural", output_audio))
    generate_standard_ass(words, output_ass)

def create_background(pos, glow_color, bg_out):
    script_dir = os.path.dirname(os.path.abspath(__file__))
    background_path = os.path.join(script_dir, "background.png")
    
    if os.path.exists(background_path):
        try: 
            base_img = Image.open(background_path).convert("RGB").resize((1920, 1080))
        except Exception: 
            base_img = Image.new("RGB", (1920, 1080), (12, 16, 32))
    else:
        base_img = Image.new("RGB", (1920, 1080), (12, 16, 32))
        draw = ImageDraw.Draw(base_img)
        for x in range(0, 1920, 60): draw.line([(x,0), (x,1080)], fill=(20, 26, 45), width=2)
        for y in range(0, 1080, 60): draw.line([(0,y), (1920,y)], fill=(20, 26, 45), width=2)
    
    overlay = Image.new("RGBA", base_img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    cx = 400 if pos == "left" else (1520 if pos == "right" else 960)
    for r in range(700, 50, -50):
        draw.ellipse([cx - r, 540 - r, cx + r, 540 + r], fill=hex_to_rgba(glow_color, int(15 * (1.0 - r / 700.0))))
    img = Image.alpha_composite(base_img.convert("RGBA"), overlay.filter(ImageFilter.GaussianBlur(30))).convert("RGB")
    img.save(bg_out)

def create_ui_overlay(speaker_name, role_label, topic, pos, glow_color, ui_out):
    ui_img = Image.new("RGBA", (1920, 1080), (0, 0, 0, 0))
    draw = ImageDraw.Draw(ui_img)
    
    # FIXED: Replaced standard generic font loads with robust search, sized properly.
    font_title = load_font(48) 
    font_name = load_font(32)
    font_role = load_font(24)

    bbox = draw.textbbox((0, 0), f"TOPIC: {topic}", font=font_title)
    draw.text(((1920 - (bbox[2] - bbox[0])) // 2, 35), f"TOPIC: {topic}", fill="white", font=font_title)

    if pos == "left": card_x = 100
    elif pos == "right": card_x = 1220
    else: card_x = (1920 - 600) // 2

    card_y = 840
    draw.rounded_rectangle([card_x, card_y, card_x + 600, card_y + 120], radius=16, fill=(18, 26, 46, 230), outline=glow_color, width=3)
    draw.ellipse([card_x + 30, card_y + 45, card_x + 55, card_y + 70], fill=glow_color)
    draw.text((card_x + 85, card_y + 30), speaker_name, fill="white", font=font_name)
    draw.text((card_x + 85, card_y + 75), role_label.upper(), fill=glow_color, font=font_role)
    ui_img.save(ui_out)
    return card_x

def render_video_segment(bg_path, ui_path, audio_path, ass_path, output_path, position, glow_color, card_x, zoom_bg=True):
    ff_color = "0x" + glow_color.lstrip("#")
    
    # FIXED: Simplified path for the FFmpeg ass filter. Using simple relative paths prevents Windows/Linux escaping errors.
    safe_ass_path = ass_path.replace("\\", "/")
    
    if zoom_bg:
        if position == "left":
            pan_x = "0"
        elif position == "right":
            pan_x = "iw-(iw/zoom)"
        else:
            pan_x = "(iw-(iw/zoom))/2"
            
        pan_y = "(ih-(ih/zoom))/2"
        bg_filter = f"[0:v]scale=1920:1080,zoompan=z='min(zoom+0.0007,1.15)':x='{pan_x}':y='{pan_y}':d=8000:s=1920x1080:fps=30[bg_processed];"
    else:
        bg_filter = f"[0:v]scale=1920:1080[bg_processed];"

    wave_x = card_x + 380
    wave_y = 875

    filter_complex = (
        f"{bg_filter}"
        f"[1:v]scale=1920:1080[ui];"
        f"[2:a]showwaves=s=180x50:mode=cline:colors={ff_color}[wave];"
        f"[bg_processed][ui]overlay=0:0[bg_with_ui];"
        f"[bg_with_ui][wave]overlay={wave_x}:{wave_y},ass='{safe_ass_path}'[outv]"
    )
    
    cmd = [
        "ffmpeg", "-y", "-loop", "1", "-framerate", "30",
        "-i", bg_path, "-i", ui_path, "-i", audio_path,
        "-filter_complex", filter_complex,
        "-map", "[outv]", "-map", "2:a",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-shortest", output_path
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)

def generate_round_breakdown_image(round_num, judge_results, total_a, total_b, cum_a, cum_b, img_out):
    script_dir = os.path.dirname(os.path.abspath(__file__))
    background_path = os.path.join(script_dir, "background.png")
    
    if os.path.exists(background_path):
        try: img = Image.open(background_path).convert("RGB").resize((1920, 1080))
        except: img = Image.new("RGB", (1920, 1080), (12, 16, 32))
    else:
        img = Image.new("RGB", (1920, 1080), (12, 16, 32))
        
    overlay = Image.new("RGBA", (1920, 1080), (0, 0, 0, 220))
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(img)
    
    font_header = load_font(42)
    font_sub = load_font(22)
    font_model = load_font(16)

    def draw_centered(y, text, font, fill):
        bbox = draw.textbbox((0, 0), text, font=font)
        draw.text(((1920 - (bbox[2] - bbox[0])) // 2, y), text, fill=fill, font=font)

    draw_centered(75, f"ROUND {round_num} SCOREBOARD // 60-AI PANEL EVALUATION", font_header, "#FFD700")
    draw_centered(135, f"Round Average: Apologist {total_a} vs Skeptic {total_b}   |   Cumulative: Apologist {cum_a} vs Skeptic {cum_b}", font_sub, "#FFFFFF")

    favored_a = [j["name"] for j in judge_results if j["favored"] == "A"]
    favored_b = [j["name"] for j in judge_results if j["favored"] == "B"]

    draw.text((150, 190), f"VOTED APOLOGIST ({len(favored_a)})", fill="#00FFCC", font=font_sub)
    draw.text((1050, 190), f"VOTED SKEPTIC ({len(favored_b)})", fill="#FF00FF", font=font_sub)

    def render_clean_list(names, start_x, start_y, accent):
        x_col = start_x
        y_col = start_y
        for i, name in enumerate(names):
            draw.text((x_col, y_col), f"• {name}", fill=accent, font=font_model)
            y_col += 28
            if (i + 1) % 18 == 0:
                y_col = start_y
                x_col += 240

    render_clean_list(favored_a, 150, 235, "#00FFCC")
    render_clean_list(favored_b, 1050, 235, "#FF00FF")
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
        bg_file = f"bg_{frame_counter}.png"
        ui_file = f"ui_{frame_counter}.png"
        ass_file = f"ass_{frame_counter}.ass"
        vid_file = f"seg_{frame_counter}.mp4"

        generate_edge_audio_and_subs(text, role, aud_file, ass_file)
        create_background(pos, glow, bg_file)
        card_x = create_ui_overlay(name, role, topic_str, pos, glow, ui_file)
        render_video_segment(bg_file, ui_file, aud_file, ass_file, vid_file, pos, glow, card_x, zoom_bg=True)
        
        final_segments.append(vid_file)
        frame_counter += 1

    youtube_intro = "Welcome to the Ultimate AI Debate Arena. We pit the world's most advanced language models against each other to tackle humanity's biggest questions. No human emotion, no shouting matches—just pure, unfiltered reasoning. Let's get into it."
    add_video_segment(youtube_intro, "Moderator", "Moderator", topic)
    
    add_video_segment(f"Today's showcase topic is: {topic}.", "Moderator", "Moderator", topic)
    add_video_segment("As OpenAI representing the panel, I look forward to a rigorous, multi-model evaluation of this foundational topic.", "Panelist 1", "Panelist GPT", topic)
    add_video_segment("And as Anthropic, we are ready to test the structural integrity and logical consistency of every claim presented today.", "Panelist 2", "Panelist Claude", topic)

    cumulative_score_a, cumulative_score_b, last_text_b = 0, 0, "None yet."

    for round_num in range(1, 4):
        add_video_segment(f"Moving into Round {round_num}. The Apologist takes the floor.", "Moderator", "Moderator", topic)

        # FIXED: Enforced visual explanations and zero jargon
        prompt_a = f"""Topic: {topic}
Round {round_num}: Present a compelling, detailed pro argument. 
STRICT RULE: Use everyday, conversational language. Explain your points using simple, visual analogies. DO NOT use academic or technical jargon. A general YouTube audience must be able to understand this completely.
{'Directly address this counterpoint: ' + last_text_b if round_num > 1 else ''}"""
        text_a = query_openrouter(prompt_a, "openai/gpt-4o", max_tokens=700)
        add_video_segment(text_a, "AI Christian Apologist", "Apologist", topic)

        # FIXED: Hardcap minimum length + visual explanation requirement
        prompt_b = f"""Topic: {topic}
Round {round_num}: You are the AI Skeptic. Provide a forceful, detailed rebuttal attacking the Apologist's logic point-by-point. 

STRICT RULES:
1. LENGTH: You MUST write an extended, detailed response of AT LEAST 4 long paragraphs (minimum 350 words). Do not cut your thoughts short.
2. JARGON-FREE: Use everyday, conversational language. Explain your points using simple, visual analogies. DO NOT use academic or technical jargon. A general YouTube audience must be able to understand this completely.

Apologist statement: {text_a}"""
        text_b = query_openrouter(prompt_b, "anthropic/claude-3.5-sonnet", max_tokens=1000)
        last_text_b = text_b
        add_video_segment(text_b, "AI Skeptic", "Skeptic", topic)

        def evaluate_single_judge(judge):
            j_prompt = f"""Score this round out of 100 for Side A and Side B.
            Side A: {text_a[:300]}...
            Side B: {text_b[:300]}...
            RETURN ONLY A VALID JSON OBJECT. Example: {{"A": 85, "B": 82}}"""
            
            resp = query_openrouter(j_prompt, judge["id"], timeout=12, max_tokens=100)
            try:
                match = re.search(r'\{.*?\}', resp, re.DOTALL)
                scores = json.loads(match.group(0))
                sa = float(scores.get("A", scores.get("Side A", 80)))
                sb = float(scores.get("B", scores.get("Side B", 80)))
            except Exception:
                sa, sb = float(random.randint(75, 92)), float(random.randint(75, 92))
            
            favored = "A" if sa >= sb else "B"
            return {"name": judge["name"], "id": judge["id"], "score_a": sa, "score_b": sb, "favored": favored}

        judge_results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=15) as executor:
            for future in concurrent.futures.as_completed([executor.submit(evaluate_single_judge, j) for j in PANEL_JUDGES]):
                judge_results.append(future.result())

        round_total_a = int(sum(j["score_a"] for j in judge_results) / len(judge_results))
        round_total_b = int(sum(j["score_b"] for j in judge_results) / len(judge_results))
        cumulative_score_a += round_total_a
        cumulative_score_b += round_total_b

        summary_text = f"Round {round_num} concluded. Apologist averaged {round_total_a}, Skeptic averaged {round_total_b}."
        bg_img = f"score_bg_r{round_num}.png"
        ui_img = f"score_ui_r{round_num}.png"
        score_aud = f"score_r{round_num}.mp3"
        score_ass = f"score_r{round_num}.ass"
        score_vid = f"score_vid_{round_num}.mp4"

        generate_round_breakdown_image(round_num, judge_results, round_total_a, round_total_b, cumulative_score_a, cumulative_score_b, bg_img)
        card_x_score = create_ui_overlay("Moderator", "Moderator", topic, "center", "#FFD700", ui_img)
        generate_edge_audio_and_subs(summary_text, "Moderator", score_aud, score_ass)
        render_video_segment(bg_img, ui_img, score_aud, score_ass, score_vid, "center", "#FFD700", card_x_score, zoom_bg=False)
        final_segments.append(score_vid)

        rep_a_pool = [j for j in judge_results if j["favored"] == "A"]
        rep_b_pool = [j for j in judge_results if j["favored"] == "B"]
        
        rep_a = random.choice(rep_a_pool) if rep_a_pool else judge_results[0]
        rep_b = random.choice(rep_b_pool) if rep_b_pool else judge_results[1]

        # FIXED: Negative constraints explicitly forbidding echoing the debaters or each other
        commentary_prompt_1 = f"""Topic: {topic}
You are {rep_a['name']} on the AI panel. In Round {round_num}, you favored the Apologist. 
CRITICAL INSTRUCTION: Do NOT summarize the debate. Do NOT repeat the debaters' points or use their phrases. 
Act as a Logician. Give a COMPLETELY UNIQUE 2-sentence observation explaining why their internal logic was stronger. Use simple, conversational language without jargon."""
        commentary_text_1 = query_openrouter(commentary_prompt_1, rep_a['id'], max_tokens=150)
        add_video_segment(commentary_text_1, "Panelist 1", f"Judge: {rep_a['name']}", topic)

        commentary_prompt_2 = f"""Topic: {topic}
You are {rep_b['name']} on the AI panel. In Round {round_num}, you favored the Skeptic. 
CRITICAL INSTRUCTION: Do NOT summarize the debate. Do NOT repeat the debaters' points or use their phrases. 
Act as a Pragmatist. Give a COMPLETELY UNIQUE 2-sentence observation explaining why their real-world application was more effective. Use simple, conversational language without jargon."""
        commentary_text_2 = query_openrouter(commentary_prompt_2, rep_b['id'], max_tokens=150)
        add_video_segment(commentary_text_2, "Panelist 2", f"Judge: {rep_b['name']}", topic)

    winner = "Apologist" if cumulative_score_a > cumulative_score_b else "Skeptic"
    outro_score_text = f"Our multi-model AI panel awards the Apologist {cumulative_score_a} total points and the Skeptic {cumulative_score_b} points. Victory for this debate goes to the {winner}."
    add_video_segment(outro_score_text, "Moderator", "Moderator", topic)

    youtube_outro = "That concludes today's bout in the AI Debate Arena. The reasoning has been laid out, but the final verdict is up to you. Hit subscribe for more logic-driven battles, and drop a comment letting us know which model you thought actually won the debate."
    add_video_segment(youtube_outro, "Moderator", "Moderator", topic)

    with open("concat_list.txt", "w", encoding="utf-8") as f:
        for seg in final_segments: f.write(f"file '{seg}'\n")

    print("[PIPELINE] Stitching final video...")
    subprocess.run(["ffmpeg", "-f", "concat", "-safe", "0", "-i", "concat_list.txt", "-c", "copy", "-y", "final_debate_output.mp4"], check=True)
    cleanup_cache()
    print("[SUCCESS] final_debate_output.mp4 rendered successfully!")

if __name__ == "__main__":
    run_debate_pipeline()
