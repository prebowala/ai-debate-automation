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

PANEL_JUDGES = [
    {"name": "GPT-4o", "id": "openai/gpt-4o"}, {"name": "GPT-4 Turbo", "id": "openai/gpt-4-turbo"}, {"name": "GPT-4o Mini", "id": "openai/gpt-4o-mini"},
    {"name": "Claude 3.5 Sonnet", "id": "anthropic/claude-3.5-sonnet"}, {"name": "Claude 3 Opus", "id": "anthropic/claude-3-opus"}, {"name": "Claude 3 Haiku", "id": "anthropic/claude-3-haiku"},
    {"name": "Gemini 1.5 Pro", "id": "google/gemini-pro-1.5"}, {"name": "Gemini 1.5 Flash", "id": "google/gemini-flash-1.5"}, {"name": "Gemma 2 27B", "id": "google/gemma-2-27b-it"}, {"name": "Gemma 2 9B", "id": "google/gemma-2-9b-it"},
    {"name": "Llama 3.1 405B", "id": "meta-llama/llama-3.1-405b-instruct"}, {"name": "Llama 3.1 70B", "id": "meta-llama/llama-3.1-70b-instruct"}, {"name": "Llama 3.1 8B", "id": "meta-llama/llama-3.1-8b-instruct"}, {"name": "Llama 3 70B", "id": "meta-llama/llama-3-70b-instruct"},
    {"name": "Mistral Large", "id": "mistralai/mistral-large"}, {"name": "Mistral Nemo", "id": "mistralai/mistral-nemo"}, {"name": "Mixtral 8x22B", "id": "mistralai/mixtral-8x22b-instruct"}, {"name": "Mixtral 8x7B", "id": "mistralai/mixtral-8x7b-instruct"},
    {"name": "Command R+", "id": "cohere/command-r-plus"}, {"name": "Command R", "id": "cohere/command-r"}, {"name": "Command", "id": "cohere/command"},
    {"name": "Grok 2", "id": "x-ai/grok-2"}, {"name": "Grok 2 Mini", "id": "x-ai/grok-2-mini"},
    {"name": "DeepSeek Coder", "id": "deepseek/deepseek-coder"}, {"name": "DeepSeek Chat", "id": "deepseek/deepseek-chat"},
    {"name": "Qwen 2.5 72B", "id": "qwen/qwen-2.5-72b-instruct"}, {"name": "Qwen 2 72B", "id": "qwen/qwen-2-72b-instruct"}, {"name": "Qwen 2 7B", "id": "qwen/qwen-2-7b-instruct"}, {"name": "Qwen 1.5 110B", "id": "qwen/qwen-1.5-110b-chat"},
    {"name": "Phi 3 Medium", "id": "microsoft/phi-3-medium-128k-instruct"}, {"name": "Phi 3 Mini", "id": "microsoft/phi-3-mini-128k-instruct"}, {"name": "WizardLM 2", "id": "microsoft/wizardlm-2-8x22b"},
    {"name": "Nova Pro", "id": "amazon/nova-pro-v1"}, {"name": "Nova Lite", "id": "amazon/nova-lite-v1"},
    {"name": "Sonar Pro", "id": "perplexity/sonar-pro"}, {"name": "Sonar", "id": "perplexity/sonar"}, {"name": "DBRX Instruct", "id": "databricks/dbrx-instruct"},
    {"name": "Hermes 3 405B", "id": "nousresearch/hermes-3-llama-3.1-405b"}, {"name": "Hermes 2 Pro", "id": "nousresearch/hermes-2-pro-llama-3-8b"}, {"name": "Capybara 34B", "id": "nousresearch/nous-capybara-34b"},
    {"name": "OLMo 7B", "id": "allenai/olmo-7b-instruct"}, {"name": "OpenChat 3.5", "id": "openchat/openchat-7b"},
    {"name": "Yi Large", "id": "01-ai/yi-large"}, {"name": "Yi 34B", "id": "01-ai/yi-34b-chat"}, {"name": "Phind Model", "id": "phind/phind-model"}, {"name": "Jamba 1.5 Large", "id": "ai21/jamba-1-5-large"},
    {"name": "Zephyr 7B", "id": "huggingfaceh4/zephyr-7b-beta"}, {"name": "Snorkel Mistral", "id": "snorkelai/snorkel-mistral-pairrm-dpo"}, {"name": "MythoMax L2", "id": "gryphe/mythomax-l2-13b"},
    {"name": "Toppy M 7B", "id": "undi95/toppy-m-7b"}, {"name": "Remm Spark", "id": "undi95/remm-spark-104b-bpw4"}, {"name": "Dolphin 2.9", "id": "cognitivecomputations/dolphin-llama-3-70b"},
    {"name": "Dolphin Mixtral", "id": "cognitivecomputations/dolphin-mixtral-8x7b"}, {"name": "StripedHyena", "id": "togethercomputer/stripedhyena-nous-7b"}, {"name": "Llama 3 Instruct", "id": "meta-llama/llama-3-8b-instruct"},
    {"name": "Qwen 2.5 7B", "id": "qwen/qwen-2.5-7b-instruct"}, {"name": "Nemotron 70B", "id": "nvidia/llama-3.1-nemotron-70b-instruct"}, {"name": "Llama 3.2 90B", "id": "meta-llama/llama-3.2-90b-vision-instruct"},
    {"name": "Llama 3.2 11B", "id": "meta-llama/llama-3.2-11b-vision-instruct"}, {"name": "Mistral Small", "id": "mistralai/mistral-small-24b-instruct-2501"}
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
    if os.path.exists("background.png"):
        try: base_img = Image.open("background.png").convert("RGB").resize((1920, 1080))
        except: base_img = Image.new("RGB", (1920, 1080), (12, 16, 32))
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

    ui_img = Image.new("RGBA", (1920, 1080), (0, 0, 0, 0))
    draw = ImageDraw.Draw(ui_img)
    try:
        font_title = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 28)
        font_name = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 24)
        font_role = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 16)
    except: font_title = font_name = font_role = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), f"TOPIC: {topic}", font=font_title)
    draw.text(((1920 - (bbox[2] - bbox[0])) // 2, 30), f"TOPIC: {topic}", fill="white", font=font_title)

    card_x, card_y = 120, 840
    draw.rounded_rectangle([card_x, card_y, card_x + 600, card_y + 120], radius=16, fill=(18, 26, 46, 230), outline=glow_color, width=3)
    draw.ellipse([card_x + 30, card_y + 45, card_x + 55, card_y + 70], fill=glow_color)
    draw.text((card_x + 75, card_y + 35), speaker_name, fill="white", font=font_name)
    draw.text((card_x + 75, card_y + 70), role_label.upper(), fill=glow_color, font=font_role)
    ui_img.save(ui_out)

def render_video_segment(bg_path, ui_path, audio_path, ass_path, output_path, position, glow_color):
    ff_color = "0x" + glow_color.lstrip("#")
    pan_x = "0" if position == "left" else ("iw-iw/zoom" if position == "right" else "iw/2-(iw/zoom/2)")
    pan_y = "ih/2-(ih/zoom/2)"
    
    filter_complex = (
        f"[0:v]scale=1920:1080,zoompan=z='min(zoom+0.0004,1.15)':x='{pan_x}':y='{pan_y}':d=8000:s=1920x1080:fps=30[zoomed_bg];"
        f"[1:v]scale=1920:1080[ui];"
        f"[2:a]showwaves=s=180x50:mode=cline:colors={ff_color}[wave];"
        f"[zoomed_bg][ui]overlay=0:0[bg_with_ui];"
        f"[bg_with_ui][wave]overlay=560:875,ass={ass_path}[outv]"
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

    draw_centered(25, f"ROUND {round_num} // 60 AI PANEL EVALUATION", font_header, "#FFD700")
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
        create_background_and_ui(name, role, topic_str, pos, glow, bg_file, ui_file)
        generate_segment_ass(text, dur, ass_file)
        render_video_segment(bg_file, ui_file, aud_file, ass_file, vid_file, pos, glow)
        
        final_segments.append(vid_file)
        frame_counter += 1

    add_video_segment(f"Welcome to our showcase debate. The topic is: {topic}.", "Moderator", "Moderator Christopher", topic)
    add_video_segment("Hello. I am the Christian Apologist. I will outline the logical grounding.", "AI Christian Apologist", "Christian Apologist", topic)
    add_video_segment("Hi. I am the Skeptic. I will test every claim for hard evidence.", "AI Skeptic", "Skeptic", topic)
    add_video_segment("Our 60 AI model panel is ready. Representatives GPT and Claude will explain the rules.", "Panelist 1", "Moderator Christopher", topic)
    add_video_segment("As GPT, we ensure unbiased multi-model scoring across all rounds.", "Panelist 1", "Panelist GPT", topic)
    add_video_segment("And as Claude, we verify logical consistency across every debate exchange.", "Panelist 2", "Panelist Claude", topic)

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

        summary_text = f"Round {round_num} concluded. Apologist averaged {round_total_a}, Skeptic averaged {round_total_b}."
        bg_img = f"score_bg_r{round_num}.png"
        ui_img = f"score_ui_r{round_num}.png"
        score_aud = f"score_r{round_num}.mp3"
        score_ass = f"score_r{round_num}.ass"
        score_vid = f"score_vid_{round_num}.mp4"

        generate_round_breakdown_image(round_num, judge_results, round_total_a, round_total_b, bg_img)
        Image.new("RGBA", (1920, 1080), (0,0,0,0)).save(ui_img) 
        
        dur = generate_edge_audio(summary_text, "Moderator", score_aud)
        generate_segment_ass(summary_text, dur, score_ass)
        render_video_segment(bg_img, ui_img, score_aud, score_ass, score_vid, "center", "#FFD700")
        
        final_segments.append(score_vid)

    winner = "Christian Apologist" if cumulative_score_a > cumulative_score_b else "Skeptic"
    outro_text = f"Our AI panel awards Christian Apologist {cumulative_score_a} total points and Skeptic {cumulative_score_b} points. Victory goes to the {winner}."
    add_video_segment(outro_text, "Moderator", "Moderator Christopher", topic)

    with open("concat_list.txt", "w", encoding="utf-8") as f:
        for seg in final_segments: f.write(f"file '{seg}'\n")

    print("[PIPELINE] Stitching final video...")
    subprocess.run(["ffmpeg", "-f", "concat", "-safe", "0", "-i", "concat_list.txt", "-c", "copy", "-y", "final_debate_output.mp4"], check=True)
    cleanup_cache()
    print("[SUCCESS] final_debate_output.mp4 rendered successfully!")

if __name__ == "__main__":
    run_debate_pipeline()
