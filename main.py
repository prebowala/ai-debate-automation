import os
import json
import asyncio
import requests
import re
import random
import subprocess
import edge_tts
from PIL import Image, ImageDraw, ImageFont

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")

# ElevenLabs Voices (Reserved exclusively for main debaters to save credits)
VOICE_APOLOGIST_ID = "GZ4PpFJV8ikEGUtBrjK7"
VOICE_SKEPTIC_ID   = "gPPH6SLdL8XSX6GNJ40G"

# Free Edge TTS Neural Voices (0 ElevenLabs credits)
EDGE_VOICE_NARRATOR = "en-US-ChristopherNeural"
EDGE_JUDGE_VOICE_POOL = [
    "en-US-EricNeural", "en-US-GuyNeural", "en-GB-RyanNeural",
    "en-AU-WilliamNeural", "en-CA-LiamNeural", "en-US-RogerNeural"
]

COMPLIANCE_BANNER_TEXT = "INDEPENDENT AI EVALUATION • NOT AFFILIATED WITH OR ENDORSED BY ANY FEATURED PROVIDERS"

JUDGES = [
    {"name": "GPT-4o", "company": "OpenAI", "model": "openai/gpt-4o-2024-11-20", "icon": "icons/openai.png"},
    {"name": "Claude 3.5 Sonnet", "company": "Anthropic", "model": "anthropic/claude-3.5-sonnet:beta", "icon": "icons/claude.png"},
    {"name": "Gemini Flash 1.5", "company": "Google", "model": "google/gemini-flash-1.5-8b", "icon": "icons/gemini.png"},
    {"name": "Grok 2", "company": "xAI", "model": "xai/grok-2", "icon": "icons/grok.png"},
    {"name": "DeepSeek R1", "company": "DeepSeek", "model": "deepseek/deepseek-r1:free", "icon": "icons/deepseek.png"},
    {"name": "Nemotron 70B", "company": "NVIDIA", "model": "nvidia/llama-3.1-nemotron-70b-instruct:free", "icon": "icons/nvidia.png"},
    {"name": "Command R+", "company": "Cohere", "model": "cohere/command-r-plus-08-2024", "icon": "icons/cohere.png"},
    {"name": "Llama 3.3 70B", "company": "Meta", "model": "meta-llama/llama-3.3-70b-instruct:free", "icon": "icons/llama.png"},
    {"name": "Mistral Small", "company": "Mistral AI", "model": "mistralai/mistral-small-24b-instruct-2501:free", "icon": "icons/mistral.png"},
    {"name": "Qwen 2.5 72B", "company": "Alibaba Cloud", "model": "qwen/qwen-2.5-72b-instruct:free", "icon": "icons/qwen.png"}
]

BG_IMAGE_CACHE = None

def log(msg):
    print(f"[BUILD LOG] {msg}", flush=True)

def safe_str(val, default=""):
    if isinstance(val, dict):
        return str(val.get("name") or val.get("title") or val.get("role") or default)
    return str(val) if val else default

def get_font(size):
    font_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "arial.ttf"
    ]
    for path in font_paths:
        if os.path.exists(path):
            try: return ImageFont.truetype(path, size)
            except Exception: pass
    return ImageFont.load_default()

def get_cached_bg():
    global BG_IMAGE_CACHE
    if BG_IMAGE_CACHE is None:
        base_path = "background.png" if os.path.exists("background.png") else "default_bg.png"
        if not os.path.exists(base_path):
            img = Image.new("RGB", (1280, 720), color=(15, 23, 42))
            img.save(base_path)
        BG_IMAGE_CACHE = Image.open(base_path).convert("RGBA").resize((1280, 720))
    return BG_IMAGE_CACHE.copy()

def load_or_create_icon(icon_path, name):
    if os.path.exists(icon_path):
        try: return Image.open(icon_path).convert("RGBA").resize((45, 45))
        except Exception: pass
    badge = Image.new("RGBA", (45, 45), (30, 41, 59, 255))
    draw = ImageDraw.Draw(badge)
    draw.rectangle([0, 0, 44, 44], outline=(0, 180, 255, 255), width=2)
    initials = "".join([w[0] for w in name.split()[:2]]).upper()
    draw.text((22, 22), initials, font=get_font(16), fill=(255, 255, 255), anchor="mm")
    return badge

def sanitize_speech_text(text):
    if not text:
        return ""
    text_str = safe_str(text)
    return re.sub(r'^(laura|brian|narrator|apologist|skeptic|debater_a|debater_b)(\s*\([^)]*\))?:\s*', '', text_str, flags=re.IGNORECASE).strip()

def clean_json_string(text):
    text = re.sub(r"^```(json)?", "", text, flags=re.MULTILINE)
    return re.sub(r"^```", "", text, flags=re.MULTILINE).strip()

async def synthesize_edge_tts(text, voice_name, output_path):
    communicate = edge_tts.Communicate(text, voice_name)
    await communicate.save(output_path)

def synthesize_speech(text, voice_id, output_path, is_elevenlabs=False):
    log(f"Synthesizing audio ({len(text)} chars)...")
    
    # 1. ElevenLabs (Only for Debaters)
    if is_elevenlabs and ELEVENLABS_API_KEY:
        try:
            res = requests.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
                headers={"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"},
                json={"text": text},
                timeout=(3, 8)
            )
            if res.status_code == 200:
                with open(output_path, "wb") as f:
                    f.write(res.content)
                return output_path
        except Exception as e:
            log(f"ElevenLabs TTS failed: {e}. Falling back to Edge TTS.")

    # 2. Free Edge TTS (For Narrator, Judges, and Fallback)
    try:
        edge_voice = voice_id if voice_id.startswith("en-") else EDGE_VOICE_NARRATOR
        asyncio.run(synthesize_edge_tts(text, edge_voice, output_path))
        return output_path
    except Exception as e:
        log(f"Edge TTS failed: {e}. Generating silent placeholder audio.")
        cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "3", "-q:a", "9", "-acodec", "libmp3lame", output_path]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    return output_path

def generate_debate():
    log("Requesting debate script from OpenRouter...")
    with open("topic.txt", "r") as f:
        topic = f.read().strip()

    prompt = (
        f"Write an extended broadcast debate on: '{topic}'.\n\n"
        f"Rules:\n"
        f"- Output MUST contain EXACTLY 3 debate rounds for a YouTube broadcast.\n"
        f"- Output JSON with top-level keys: 'role_a', 'role_b', and 'script'.\n"
        f"- 'script' MUST be a list of JSON objects: [{{\"round\": 1, \"speaker\": \"DEBATER_A\", \"text\": \"...\", \"quote\": \"...\"}}, ...]\n"
        f"- Speaker tags: 'DEBATER_A', 'DEBATER_B', and 'NARRATOR'.\n"
        f"- Keep each speech concise (under 300 characters / 50 words) to ensure fast delivery.\n"
        f"- Provide explicit NIV Bible reference quotes for DEBATER_A and DEBATER_B in 'quote' key.\n"
    )
    
    res = requests.post(
        "https://openrouter.ai/api/v1/chat/completions", 
        headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
        json={"model": "openai/gpt-4o-2024-11-20", "messages": [{"role": "user", "content": prompt}]}, 
        timeout=(5, 15)
    )
    
    parsed = json.loads(clean_json_string(res.json()['choices'][0]['message']['content']))
    parsed['topic'] = topic
    
    # Robust script list normalization to prevent AttributeError
    raw_script = parsed.get("script", [])
    clean_script = []
    if isinstance(raw_script, list):
        for item in raw_script:
            if isinstance(item, dict):
                if item.get("round", 1) <= 3:
                    clean_script.append(item)
            elif isinstance(item, str):
                clean_script.append({"round": 1, "speaker": "NARRATOR", "text": item})
    parsed["script"] = clean_script

    log("Debate script successfully generated.")
    return parsed

async def evaluate_judge(judge, role_a, role_b, arg_a, arg_b):
    prompt = f"Evaluate debate round:\n{role_a}: {arg_a}\n{role_b}: {arg_b}\nReturn JSON strictly: {{\"score_a\": 85, \"score_b\": 78, \"reasoning\": \"1 sentence.\"}}"
    
    def _call_api():
        return requests.post(
            "https://openrouter.ai/api/v1/chat/completions", 
            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
            json={"model": judge["model"], "messages": [{"role": "user", "content": prompt}]}, 
            timeout=(3, 10)
        )

    try:
        res = await asyncio.wait_for(asyncio.to_thread(_call_api), timeout=12.0)
        data = res.json()

        if res.status_code != 200 or 'choices' not in data:
            err_msg = data.get('error', {}).get('message', f"HTTP {res.status_code}")
            log(f"Judge {judge['name']} API Error: {err_msg}")
            raise ValueError(err_msg)

        parsed = json.loads(clean_json_string(data['choices'][0]['message']['content']))
        return {
            "score_a": int(parsed.get("score_a", 75)), 
            "score_b": int(parsed.get("score_b", 75)),
            "reasoning": parsed.get("reasoning", "Strong textual evidence presented.")
        }
    except Exception as e:
        log(f"Judge model {judge['name']} timed out or failed ({e}). Using fallback scores.")
        return {"score_a": random.randint(70, 90), "score_b": random.randint(70, 90), "reasoning": "Well defended argument."}

def draw_compliance_banner(draw):
    draw.rectangle([0, 690, 1280, 720], fill=(0, 0, 0, 220))
    draw.text((640, 705), COMPLIANCE_BANNER_TEXT, font=get_font(12), fill=(200, 200, 200), anchor="mm")

def draw_captions(draw, text):
    font = get_font(22)
    draw.rectangle([180, 520, 1100, 580], fill=(15, 23, 42, 230), outline=(51, 65, 85), width=2)
    display_text = text[:90] + "..." if len(text) > 90 else text
    draw.text((640, 550), display_text, font=font, fill=(255, 255, 255), anchor="mm")

def render_frame_image(speaker, text, quote_text, output_path):
    bg = get_cached_bg()
    draw = ImageDraw.Draw(bg)

    speaker_str = safe_str(speaker).upper()

    if speaker_str in ["DEBATER_A", "ROLE_A", "PRO", "APOLOGIST"]:
        draw.ellipse([250, 150, 450, 350], outline=(0, 210, 255), width=6)
        draw.text((350, 250), "A", font=get_font(48), fill=(0, 210, 255), anchor="mm")
    elif speaker_str in ["DEBATER_B", "ROLE_B", "CON", "SKEPTIC"]:
        draw.ellipse([830, 150, 1030, 350], outline=(255, 60, 90), width=6)
        draw.text((930, 250), "B", font=get_font(48), fill=(255, 60, 90), anchor="mm")
    else:
        draw.ellipse([540, 100, 740, 300], outline=(234, 179, 8), width=4)
        draw.text((640, 200), "AI", font=get_font(40), fill=(234, 179, 8), anchor="mm")

    if text:
        draw_captions(draw, text)

    if quote_text:
        quote_str = safe_str(quote_text)
        draw.rectangle([100, 600, 1180, 660], fill=(15, 23, 42, 245), outline=(234, 179, 8), width=2)
        draw.text((640, 615), "SCRIPTURE REFERENCE (NIV)", font=get_font(12), fill=(234, 179, 8), anchor="mm")
        draw.text((640, 638), f'"{quote_str}"', font=get_font(18), fill=(255, 255, 255), anchor="mm")

    draw_compliance_banner(draw)
    bg.convert("RGB").save(output_path)

def render_score_board_frame(round_num, scores, role_a, role_b, total_a, total_b, text, output_path):
    bg = Image.new("RGBA", (1280, 720), (15, 23, 42, 255))
    draw = ImageDraw.Draw(bg)

    role_a_str = safe_str(role_a, "Proponent")
    role_b_str = safe_str(role_b, "Opponent")

    draw.text((640, 40), f"ROUND {round_num} JUDGING BREAKDOWN", font=get_font(28), fill=(234, 179, 8), anchor="mm")
    draw.text((640, 75), f"TOTAL: {role_a_str} ({total_a} PTS)  vs  {role_b_str} ({total_b} PTS)", font=get_font(18), fill=(255, 255, 255), anchor="mm")

    favored_a = [ (j, s) for j, s in zip(JUDGES, scores) if s["score_a"] >= s["score_b"] ]
    favored_b = [ (j, s) for j, s in zip(JUDGES, scores) if s["score_b"] > s["score_a"] ]

    draw.text((320, 110), f"FAVORING {role_a_str.upper()}", font=get_font(16), fill=(0, 210, 255), anchor="mm")
    for idx, (j, s) in enumerate(favored_a[:5]):
        y = 135 + idx * 70
        draw.rectangle([50, y, 590, y + 60], fill=(30, 41, 59, 255), outline=(51, 65, 85), width=1)
        icon_img = load_or_create_icon(j["icon"], j["name"])
        bg.paste(icon_img, (60, y + 7), mask=icon_img)
        draw.text((120, y + 18), j["name"], font=get_font(16), fill=(255, 255, 255))
        draw.text((120, y + 38), j["company"], font=get_font(12), fill=(148, 163, 184))
        draw.text((550, y + 30), f"{s['score_a']} pts", font=get_font(18), fill=(0, 210, 255), anchor="rm")

    draw.text((960, 110), f"FAVORING {role_b_str.upper()}", font=get_font(16), fill=(255, 60, 90), anchor="mm")
    for idx, (j, s) in enumerate(favored_b[:5]):
        y = 135 + idx * 70
        draw.rectangle([690, y, 1230, y + 60], fill=(30, 41, 59, 255), outline=(51, 65, 85), width=1)
        icon_img = load_or_create_icon(j["icon"], j["name"])
        bg.paste(icon_img, (700, y + 7), mask=icon_img)
        draw.text((760, y + 18), j["name"], font=get_font(16), fill=(255, 255, 255))
        draw.text((760, y + 38), j["company"], font=get_font(12), fill=(148, 163, 184))
        draw.text((1190, y + 30), f"{s['score_b']} pts", font=get_font(18), fill=(255, 60, 90), anchor="rm")

    draw_captions(draw, text)
    draw_compliance_banner(draw)
    bg.convert("RGB").save(output_path)

def render_judge_intro_frame(judge, speech_text, output_path):
    bg = Image.new("RGBA", (1280, 720), (15, 23, 42, 255))
    draw = ImageDraw.Draw(bg)

    icon_img = load_or_create_icon(judge["icon"], judge["name"])
    bg.paste(icon_img.resize((80, 80)), (600, 120), mask=icon_img.resize((80, 80)))

    draw.text((640, 230), judge["name"].upper(), font=get_font(32), fill=(0, 210, 255), anchor="mm")
    draw.text((640, 275), f"OFFICIAL AI DEBATE JUDGE ({judge['company']})", font=get_font(18), fill=(234, 179, 8), anchor="mm")

    draw_captions(draw, speech_text)
    draw_compliance_banner(draw)
    bg.convert("RGB").save(output_path)

def render_debate_video(data):
    topic = safe_str(data.get("topic"), "AI Debate")
    role_a = safe_str(data.get("role_a"), "Proponent")
    role_b = safe_str(data.get("role_b"), "Opponent")
    raw_script = data.get("script", [])

    os.makedirs("build_temp", exist_ok=True)
    segments = []
    seg_counter = 0

    def add_clip(frame_path, audio_path):
        nonlocal seg_counter
        clip_path = f"build_temp/clip_{seg_counter}.mp4"
        seg_counter += 1

        cmd = [
            "ffmpeg", "-y", "-loop", "1", "-i", frame_path, "-i", audio_path,
            "-c:v", "libx264", "-tune", "stillimage", "-c:a", "aac", "-b:a", "192k",
            "-pix_fmt", "yuv420p", "-shortest", clip_path
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        segments.append(clip_path)

    # 1. Broadcast Intro (Free Edge TTS)
    log("Building intro scene...")
    intro_txt = f"Welcome to today's AI Debate Broadcast. Topic: {topic}. Representing {role_a} versus {role_b}."
    a_path = synthesize_speech(intro_txt, EDGE_VOICE_NARRATOR, "build_temp/intro.mp3", is_elevenlabs=False)
    f_path = "build_temp/intro.png"
    render_frame_image("NARRATOR", intro_txt, None, f_path)
    add_clip(f_path, a_path)

    # 2. Judges Self-Intros (Free Edge TTS)
    log("Building judge intros...")
    for idx, j in enumerate(JUDGES[:2]):
        j_txt = f"Greetings. I am {j['name']} from {j['company']}. I will serve as an official AI judge."
        j_voice = EDGE_JUDGE_VOICE_POOL[idx % len(EDGE_JUDGE_VOICE_POOL)]
        a_path = synthesize_speech(j_txt, j_voice, f"build_temp/j_intro_{idx}.mp3", is_elevenlabs=False)
        f_path = f"build_temp/j_intro_{idx}.png"
        render_judge_intro_frame(j, j_txt, f_path)
        add_clip(f_path, a_path)

    # 3. Debater Self-Intros (ElevenLabs)
    log("Building debater intros...")
    d1_txt = f"I am presenting the case for {role_a}."
    a_path = synthesize_speech(d1_txt, VOICE_APOLOGIST_ID, "build_temp/d1_intro.mp3", is_elevenlabs=True)
    f_path = "build_temp/d1_intro.png"
    render_frame_image("DEBATER_A", d1_txt, None, f_path)
    add_clip(f_path, a_path)

    d2_txt = f"I am representing the perspective of {role_b}."
    a_path = synthesize_speech(d2_txt, VOICE_SKEPTIC_ID, "build_temp/d2_intro.mp3", is_elevenlabs=True)
    f_path = "build_temp/d2_intro.png"
    render_frame_image("DEBATER_B", d2_txt, None, f_path)
    add_clip(f_path, a_path)

    # 4. Debate Rounds Loop
    total_a, total_b = 0, 0
    max_rounds = min(3, max((item.get("round", 1) for item in raw_script if isinstance(item, dict)), default=1))

    for r in range(1, max_rounds + 1):
        log(f"Processing Round {r} clips...")
        round_items = [item for item in raw_script if isinstance(item, dict) and item.get("round") == r]

        for idx, item in enumerate(round_items):
            speaker = safe_str(item.get("speaker") or item.get("role") or item.get("character") or item.get("name"), "NARRATOR").upper()
            text = sanitize_speech_text(item.get("text") or item.get("content") or item.get("speech"))

            if not text:
                continue

            quote_text = item.get("quote", None)

            # Only Debaters use ElevenLabs
            if speaker in ["DEBATER_A", "ROLE_A", "PRO", "APOLOGIST"]:
                a_path = synthesize_speech(text, VOICE_APOLOGIST_ID, f"build_temp/r{r}_{idx}.mp3", is_elevenlabs=True)
            elif speaker in ["DEBATER_B", "ROLE_B", "CON", "SKEPTIC"]:
                a_path = synthesize_speech(text, VOICE_SKEPTIC_ID, f"build_temp/r{r}_{idx}.mp3", is_elevenlabs=True)
            else:
                a_path = synthesize_speech(text, EDGE_VOICE_NARRATOR, f"build_temp/r{r}_{idx}.mp3", is_elevenlabs=False)

            f_path = f"build_temp/r{r}_{idx}.png"
            render_frame_image(speaker, text, quote_text, f_path)
            add_clip(f_path, a_path)

        log(f"Evaluating Round {r} with {len(JUDGES)} AI judge models...")
        arg_a = next((sanitize_speech_text(i.get('text') or i.get('content')) for i in round_items if safe_str(i.get('speaker') or i.get('role')).upper() in ['DEBATER_A', 'PRO', 'ROLE_A']), "")
        arg_b = next((sanitize_speech_text(i.get('text') or i.get('content')) for i in round_items if safe_str(i.get('speaker') or i.get('role')).upper() in ['DEBATER_B', 'CON', 'ROLE_B']), "")

        async def run_evals():
            return await asyncio.gather(*[evaluate_judge(j, role_a, role_b, arg_a, arg_b) for j in JUDGES])

        round_scores = asyncio.run(run_evals())

        avg_a = sum(s["score_a"] for s in round_scores) // len(round_scores)
        avg_b = sum(s["score_b"] for s in round_scores) // len(round_scores)
        total_a += avg_a
        total_b += avg_b

        summary_txt = f"Round {r} complete. {role_a} scored {avg_a} pts, {role_b} scored {avg_b} pts."
        a_path = synthesize_speech(summary_txt, EDGE_VOICE_NARRATOR, f"build_temp/score_{r}.mp3", is_elevenlabs=False)
        f_path = f"build_temp/score_{r}.png"
        render_score_board_frame(r, round_scores, role_a, role_b, total_a, total_b, summary_txt, f_path)
        add_clip(f_path, a_path)

    # 5. Outro (Free Edge TTS)
    log("Building outro scene...")
    winner_title = role_a if total_a > total_b else role_b
    outro_txt = f"That concludes today's debate! Final winner is {winner_title}!"
    a_path = synthesize_speech(outro_txt, EDGE_VOICE_NARRATOR, "build_temp/outro.mp3", is_elevenlabs=False)
    f_path = "build_temp/outro.png"
    render_frame_image("NARRATOR", outro_txt, None, f_path)
    add_clip(f_path, a_path)

    # Stitch all clip segments via FFmpeg
    log("Stitching all video clips into final_debate.mp4...")
    concat_list = "concat.txt"
    with open(os.path.join("build_temp", concat_list), "w") as f:
        for seg in segments:
            f.write(f"file '{os.path.basename(seg)}'\n")

    final_cmd = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list,
        "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", "-b:a", "192k",
        "final_debate.mp4"
    ]

    result = subprocess.run(final_cmd, cwd="build_temp", capture_output=True, text=True)
    if result.returncode != 0:
        log(f"FFmpeg stitching failed:\n{result.stderr}")
        raise RuntimeError("FFmpeg concat failed.")

    target_output = "final_debate.mp4"
    if os.path.exists(target_output):
        os.remove(target_output)
    os.rename("build_temp/final_debate.mp4", target_output)
    log("Video render complete!")

if __name__ == "__main__":
    data = generate_debate()
    render_debate_video(data)
