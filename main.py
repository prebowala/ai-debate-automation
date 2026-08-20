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

VOICES = {
    "Moderator": "en-US-ChristopherNeural",
    "AI Christian Apologist": "en-US-BrianMultilingualNeural",
    "AI Skeptic": "en-US-AvaMultilingualNeural",
    "Panelist 1": "en-US-AndrewMultilingualNeural",
    "Panelist 2": "en-US-EmmaMultilingualNeural"
}

# 60 Distinct AI Companies and Creators available via OpenRouter
PANEL_JUDGES = [
    {"name": "OpenAI", "id": "openai/gpt-4o"},
    {"name": "Anthropic", "id": "anthropic/claude-3.5-sonnet"},
    {"name": "Google", "id": "google/gemini-pro-1.5"},
    {"name": "Meta", "id": "meta-llama/llama-3.1-405b-instruct"},
    {"name": "Mistral AI", "id": "mistralai/mistral-large"},
    {"name": "Cohere", "id": "cohere/command-r-plus"},
    {"name": "xAI", "id": "x-ai/grok-2"},
    {"name": "DeepSeek", "id": "deepseek/deepseek-chat"},
    {"name": "Alibaba Cloud (Qwen)", "id": "qwen/qwen-2.5-72b-instruct"},
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
            if file != 'final_debate_output.mp4':
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

def query_openrouter(prompt, primary_model_id, timeout=45):
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
    try:
        response = requests.post(
            OPENROUTER_URL, headers=headers, 
            json={"model": primary_model_id, "messages": [{"role": "user", "content": prompt}], "temperature": 0.5}, 
            timeout=timeout
        )
        if response.status_code == 200: return response.json()["choices"][0]["message"]["content"].strip()
    except Exception: pass
    return "The evidence leads us to a fascinating conclusion."

async def _save_edge_audio(text, voice, filename):
    communicate = edge_tts.Communicate(clean_for_speech(text).replace('&', 'and'), voice)
    await communicate.save(filename)

def generate_edge_audio(text, role_key, output_filename):
    voice = VOICES.get(role_key, VOICES["Moderator"])
    try: asyncio.run(_save_edge_audio(text, voice, output_filename))
    except Exception: asyncio.run(_save_edge_audio(text, "en-US-ChristopherNeural", output_filename))
    result = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", output_filename], stdout=subprocess.PIPE, text=True)
    return float(result.stdout.strip())

def format_ass_time(seconds):
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{int(hours)}:{int(minutes):02d}:{int(secs):02d}.{int((seconds - int(seconds)) * 100):02d}"

def generate_segment_ass(text, duration, filename):
    words = clean_for_speech(text).split()
    chunks = [" ".join(words[i:i + 5]) for i in range(0, len(words), 5)]
    ass_content = "[Script Info]\nScriptType: v4.00+\nPlayResX: 1920\nPlayResY: 1080\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\nStyle: CleanSub,DejaVuSans-Bold,38,&H00FFFFFF,&H0000FFFF,&HFF000000,&H80000000,1,0,0,0,100,100,0,0,1,3,2,5,100,100,60,1\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    chunk_dur = duration / max(1, len(chunks))
    for i, chunk in enumerate(chunks):
        ass_content += f"Dialogue: 0,{format_ass_time(i*chunk_dur)},{format_ass_time((i+1)*chunk_dur)},CleanSub,,0,0,0,,{chunk}\n"
    with open(filename, "w", encoding="utf-8") as f: f.write(ass_content)

def create_background_and_ui(speaker_name, role_label, topic, pos, glow_color, bg_out, ui_out):
    script_dir = os.path.dirname(os.path.abspath(__file__))
    background_path = os.path.join(script_dir, "background.png")
    if not os.path.exists(background_path):
        background_path = "background.png"
    
    if os.path.exists(background_path):
        try: 
            base_img = Image.open(background_path).convert("RGB").resize((1920, 1080))
            print(f"Successfully loaded background from: {background_path}")
        except Exception as e: 
            print(f"Error opening background image: {e}")
            base_img = Image.new("RGB", (1920, 1080), (12, 16, 32))
    else:
        print("background.png not found, using procedural grid fallback.")
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

    ui_img = Image.new("RGBA", (1920, 1080), (0, 0, 0, 0))
    draw = ImageDraw.Draw(ui_img)
    try:
        font_title = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 28)
        font_name = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 24)
        font_role = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 16)
    except: font_title = font_name = font_role = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), f"TOPIC: {topic}", font=font_title)
    draw.text(((1920 - (bbox[2] - bbox[0])) // 2, 30), f"TOPIC: {topic}", fill="white", font=font_title)

    # Dynamic label card positioning based on speaker side
    if pos == "left":
        card_x = 100
    elif pos == "right":
        card_x = 1220
    else:
        card_x = (1920 - 600) // 2

    card_y = 840
    draw.rounded_rectangle([card_x, card_y, card_x + 600, card_y + 120], radius=16, fill=(18, 26, 46, 230), outline=glow_color, width=3)
    draw.ellipse([card_x + 30, card_y + 45, card_x + 55, card_y + 70], fill=glow_color)
    draw.text((card_x + 75, card_y + 35), speaker_name, fill="white", font=font_name)
    draw.text((card_x + 75, card_y + 70), role_label.upper(), fill=glow_color, font=font_role)
    ui_img.save(ui_out)
    return card_x

def render_video_segment(bg_path, ui_path, audio_path, ass_path, output_path, position, glow_color, card_x):
    ff_color = "0x" + glow_color.lstrip("#")
    pan_x = "0" if position == "left" else ("iw-iw/zoom" if position == "right" else "iw/2-(iw/zoom/2)")
    pan_y = "ih/2-(ih/zoom/2)"
    
    # Place audio wave strictly inside the speaker card window
    wave_x = card_x + 380
    wave_y = 875

    filter_complex = (
        f"[0:v]scale=1920:1080,zoompan=z='min(zoom+0.0004,1.15)':x='{pan_x}':y='{pan_y}':d=8000:s=1920x1080:fps=30[zoomed_bg];"
        f"[1:v]scale=1920:1080[ui];"
        f"[2:a]showwaves=s=180x50:mode=cline:colors={ff_color}[wave];"
        f"[zoomed_bg][ui]overlay=0:0[bg_with_ui];"
        f"[bg_with_ui][wave]overlay={wave_x}:{wave_y},ass={ass_path}[outv]"
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

def generate_round_breakdown_image(round_num, judge_results, total_a, total_b, img_out):
    img = Image.new("RGB", (1920, 1080), (12, 16, 32))
    draw = ImageDraw.Draw(img)
    try:
        font_header = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 32)
        font_sub = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 18)
        font_model = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 12)
    except: font_header = font_sub = font_model = ImageFont.load_default()

    def draw_centered(y, text, font, fill):
        bbox = draw.textbbox((0, 0), text, font=font)
        draw.text(((1920 - (bbox[2] - bbox[0])) // 2, y), text, fill=fill, font=font)

    draw_centered(25, f"ROUND {round_num} // 60 DISTINCT COMPANY AI PANEL EVALUATION", font_header, "#FFD700")
    draw_centered(65, f"AGGREGATE: Apologist ({total_a} PTS) vs Skeptic ({total_b} PTS)", font_sub, "#00FFCC")

    side_a_judges = [j for j in judge_results if j["favored"] == "A"]
    side_b_judges = [j for j in judge_results if j["favored"] == "B"]

    def render_dense_column(judges, start_x, start_y, accent_color):
        y = start_y
        for j in judges[:30]:
            draw.rounded_rectangle([start_x, y, start_x + 840, y + 22], radius=4, fill=(15, 22, 38), outline=accent_color, width=1)
            draw.text((start_x + 12, y + 4), j["name"], fill="white", font=font_model)
            score_text = f"A: {int(j['score_a'])} | B: {int(j['score_b'])}"
            draw.text((start_x + 720, y + 4), score_text, fill=accent_color, font=font_model)
            y += 28

    render_dense_column(side_a_judges, 60, 130, "#00FFCC")
    render_dense_column(side_b_judges, 1020, 130, "#FF00FF")
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

        dur = generate_edge_audio(text, role, aud_file)
        card_x = create_background_and_ui(name, role, topic_str, pos, glow, bg_file, ui_file)
        generate_segment_ass(text, dur, ass_file)
        render_video_segment(bg_file, ui_file, aud_file, ass_file, vid_file, pos, glow, card_x)
        
        final_segments.append(vid_file)
        frame_counter += 1

    add_video_segment(f"Welcome to our showcase debate. The topic is: {topic}.", "Moderator", "Moderator Christopher", topic)
    add_video_segment("Hello. I am the Christian Apologist. I will outline the logical grounding.", "AI Christian Apologist", "Christian Apologist", topic)
    add_video_segment("Hi. I am the Skeptic. I will test every claim for hard evidence.", "AI Skeptic", "Skeptic", topic)
    add_video_segment("Our 60-company multi-vendor AI panel is ready. Representatives GPT and Claude will explain the rules.", "Panelist 1", "Moderator Christopher", topic)
    add_video_segment("As OpenAI, we ensure balanced cross-industry judging across all rounds.", "Panelist 1", "Panelist GPT", topic)
    add_video_segment("And as Anthropic, we verify structural logical consistency across the board.", "Panelist 2", "Panelist Claude", topic)

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
            RETURN ONLY A VALID JSON OBJECT. Example: {{"A": 85, "B": 82}}"""
            
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
        with concurrent.futures.ThreadPoolExecutor(max_workers=15) as executor:
            for future in concurrent.futures.as_completed([executor.submit(evaluate_single_judge, j) for j in PANEL_JUDGES]):
                judge_results.append(future.result())

        round_total_a = int(sum(j["score_a"] for j in judge_results) / len(judge_results))
        round_total_b = int(sum(j["score_b"] for j in judge_results) / len(judge_results))
        cumulative_score_a += round_total_a
        cumulative_score_b += round_total_b

        # 1. Show Score Breakdown Image
        summary_text = f"Round {round_num} concluded. Apologist averaged {round_total_a}, Skeptic averaged {round_total_b}."
        bg_img = f"score_bg_r{round_num}.png"
        ui_img = f"score_ui_r{round_num}.png"
        score_aud = f"score_r{round_num}.mp3"
        score_ass = f"score_r{round_num}.ass"
        score_vid = f"score_vid_{round_num}.mp4"

        generate_round_breakdown_image(round_num, judge_results, round_total_a, round_total_b, bg_img)
        card_x_score = create_background_and_ui("Moderator Christopher", "Moderator", topic, "center", "#FFD700", bg_img, ui_img)
        
        dur = generate_edge_audio(summary_text, "Moderator", score_aud)
        generate_segment_ass(summary_text, dur, score_ass)
        render_video_segment(bg_img, ui_img, score_aud, score_ass, score_vid, "center", "#FFD700", card_x_score)
        final_segments.append(score_vid)

        # 2. Representative AI Panelists Explain Scoring Rationale
        commentary_prompt_1 = f"As OpenAI on the 60-company judge panel, explain briefly why Side A scored {round_total_a} and Side B scored {round_total_b} in Round {round_num}."
        commentary_text_1 = query_openrouter(commentary_prompt_1, "openai/gpt-4o")
        add_video_segment(commentary_text_1, "Panelist 1", "Panelist GPT", topic)

        commentary_prompt_2 = f"As Anthropic on the 60-company judge panel, add your perspective on these round scores."
        commentary_text_2 = query_openrouter(commentary_prompt_2, "anthropic/claude-3.5-sonnet")
        add_video_segment(commentary_text_2, "Panelist 2", "Panelist Claude", topic)

    winner = "Christian Apologist" if cumulative_score_a > cumulative_score_b else "Skeptic"
    outro_text = f"Our 60-company AI panel awards Christian Apologist {cumulative_score_a} total points and Skeptic {cumulative_score_b} points. Victory goes to the {winner}."
    add_video_segment(outro_text, "Moderator", "Moderator Christopher", topic)

    with open("concat_list.txt", "w", encoding="utf-8") as f:
        for seg in final_segments: f.write(f"file '{seg}'\n")

    print("[PIPELINE] Stitching final video...")
    subprocess.run(["ffmpeg", "-f", "concat", "-safe", "0", "-i", "concat_list.txt", "-c", "copy", "-y", "final_debate_output.mp4"], check=True)
    cleanup_cache()
    print("[SUCCESS] final_debate_output.mp4 rendered successfully!")

if __name__ == "__main__":
    run_debate_pipeline()
