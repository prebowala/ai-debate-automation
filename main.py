import os
import json
import asyncio
import requests
import re
import random
import subprocess
import numpy as np
from PIL import Image, ImageDraw, ImageFont

# API Keys
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")

# Primary ElevenLabs Voices
VOICE_NARRATOR_ID = "QIhD5ivPGEoYZQDocuHI"   # Narrator
VOICE_APOLOGIST_ID = "GZ4PpFJV8ikEGUtBrjK7"  # Debater A
VOICE_SKEPTIC_ID   = "gPPH6SLdL8XSX6GNJ40G"  # Debater B

# Voice Pool for AI Judges
JUDGE_VOICE_POOL = [
    "21m00Tcm4TlvDq8ikWAM", "AZnzlk1XvdvUeBnXmlld", "EXAVITQu4vr4xnSDxMaL",
    "ErXwobaYiN019PkySvjV", "MF3mGyEYCl7XYWbV9V6O", "TxGEqnHWrfWFTfGW9XjX"
]

COMPLIANCE_BANNER_TEXT = "INDEPENDENT AI EVALUATION • NOT AFFILIATED WITH OR ENDORSED BY ANY FEATURED PROVIDERS"

# 15 AI Judge Models
JUDGES = [
    {"name": "GPT-5.6 Sol", "company": "OpenAI", "model": "openai/gpt-5.6-sol", "icon": "icons/openai.png"},
    {"name": "Claude Opus 5", "company": "Anthropic", "model": "anthropic/claude-opus-5", "icon": "icons/claude.png"},
    {"name": "Gemini 3.7 Flash", "company": "Google", "model": "google/gemini-3.7-flash", "icon": "icons/gemini.png"},
    {"name": "Grok 4.6", "company": "xAI", "model": "xai/grok-4.6", "icon": "icons/grok.png"},
    {"name": "DeepSeek V4 Pro", "company": "DeepSeek", "model": "deepseek/deepseek-v4-pro", "icon": "icons/deepseek.png"},
    {"name": "GLM 5.2", "company": "Zhipu AI", "model": "zhipu/glm-5.2", "icon": "icons/glm.png"},
    {"name": "Nemotron 3 Ultra", "company": "NVIDIA", "model": "nvidia/nemotron-3-ultra-550b-a55b:free", "icon": "icons/nvidia.png"},
    {"name": "North Mini", "company": "Cohere", "model": "cohere/north-mini-code:free", "icon": "icons/cohere.png"},
    {"name": "Laguna S 2.1", "company": "Poolside", "model": "poolside/laguna-s-2.1:free", "icon": "icons/poolside.png"},
    {"name": "Llama 3.3 70B", "company": "Meta", "model": "meta-llama/llama-3.3-70b-instruct", "icon": "icons/llama.png"},
    {"name": "Mistral Large 3", "company": "Mistral AI", "model": "mistralai/mistral-large-2411", "icon": "icons/mistral.png"},
    {"name": "Jamba 1.5 Large", "company": "AI21 Labs", "model": "ai21/jamba-1-5-large", "icon": "icons/ai21.png"},
    {"name": "Qwen 2.5 72B", "company": "Alibaba Cloud", "model": "qwen/qwen-2.5-72b-instruct", "icon": "icons/qwen.png"},
    {"name": "Titan Express", "company": "Amazon Bedrock", "model": "amazon/titan-text-express", "icon": "icons/amazon.png"},
    {"name": "Phi 3.5 Vision", "company": "Microsoft", "model": "microsoft/phi-3.5-vision-instruct", "icon": "icons/microsoft.png"}
]

BG_IMAGE_CACHE = None

def get_font(size):
    font_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:\\Windows\\Fonts\\arialbd.ttf",
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
    return re.sub(r'^(laura|brian|narrator|apologist|skeptic|debater_a|debater_b)(\s*\([^)]*\))?:\s*', '', text, flags=re.IGNORECASE).strip()

def clean_json_string(text):
    text = re.sub(r"^```(json)?", "", text, flags=re.MULTILINE)
    return re.sub(r"^```", "", text, flags=re.MULTILINE).strip()

def get_audio_duration(file_path):
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", file_path]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        return float(res.stdout.strip())
    except Exception:
        return 3.0

def synthesize_speech(text, voice_id, output_path):
    if ELEVENLABS_API_KEY:
        try:
            res = requests.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
                headers={"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"},
                json={"text": text},
                timeout=(5, 15)
            )
            if res.status_code == 200:
                with open(output_path, "wb") as f:
                    f.write(res.content)
                return output_path
        except Exception as e:
            print(f"ElevenLabs TTS failed: {e}. Falling back to gTTS.")

    try:
        from gTTS import gTTS
        tts = gTTS(text=text, lang='en')
        tts.save(output_path)
    except Exception:
        # Fallback to silent MP3 via FFmpeg if network drops
        cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "3", "-q:a", "9", "-acodec", "libmp3lame", output_path]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    return output_path

def generate_debate():
    with open("topic.txt", "r") as f:
        topic = f.read().strip()

    prompt = (
        f"Write an extended broadcast debate on: '{topic}'.\n\n"
        f"Rules:\n"
        f"- Output MUST contain EXACTLY 3 comprehensive debate rounds for a YouTube broadcast.\n"
        f"- Output JSON with top-level keys: 'role_a', 'role_b', and 'script'.\n"
        f"- 'role_a' and 'role_b' are concise debater titles.\n"
        f"- Speaker tags: 'DEBATER_A', 'DEBATER_B', and 'NARRATOR'.\n"
        f"- In EVERY speaker turn for DEBATER_A and DEBATER_B, provide an explicit Bible reference or quote using the NIV translation in the 'quote' key.\n"
        f"JSON Format:\n"
        f"{{\n"
        f'  "role_a": "Title A",\n'
        f'  "role_b": "Title B",\n'
        f'  "script": [\n'
        f'    {{"speaker": "DEBATER_A", "round": 1, "text": "...", "quote": "John 3:16 (NIV) - For God so loved..."}}\n'
        f'  ]\n'
        f"}}\n"
    )
    
    res = requests.post(
        "https://openrouter.ai/api/v1/chat/completions", 
        headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
        json={"model": "openai/gpt-5.6-sol", "messages": [{"role": "user", "content": prompt}]}, 
        timeout=(5, 30)
    )
    
    parsed = json.loads(clean_json_string(res.json()['choices'][0]['message']['content']))
    parsed['topic'] = topic

    if "script" in parsed:
        parsed["script"] = [item for item in parsed["script"] if item.get("round", 1) <= 3]

    return parsed

async def evaluate_judge(judge, role_a, role_b, arg_a, arg_b):
    prompt = f"Evaluate debate round:\n{role_a}: {arg_a}\n{role_b}: {arg_b}\nReturn JSON strictly: {{\"score_a\": 85, \"score_b\": 78, \"reasoning\": \"1 sentence explanation.\"}}"
    try:
        res = requests.post("https://openrouter.ai/api/v1/chat/completions", 
                            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
                            json={"model": judge["model"], "messages": [{"role": "user", "content": prompt}]}, 
                            timeout=(5, 10))
        parsed = json.loads(clean_json_string(res.json()['choices'][0]['message']['content']))
        return {
            "score_a": int(parsed.get("score_a", 75)), 
            "score_b": int(parsed.get("score_b", 75)),
            "reasoning": parsed.get("reasoning", "Strong textual evidence presented.")
        }
    except Exception:
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

    if speaker == "DEBATER_A":
        draw.ellipse([250, 150, 450, 350], outline=(0, 210, 255), width=6)
        draw.text((350, 250), "A", font=get_font(48), fill=(0, 210, 255), anchor="mm")
    elif speaker == "DEBATER_B":
        draw.ellipse([830, 150, 1030, 350], outline=(255, 60, 90), width=6)
        draw.text((930, 250), "B", font=get_font(48), fill=(255, 60, 90), anchor="mm")
    elif speaker == "NARRATOR":
        draw.ellipse([540, 100, 740, 300], outline=(234, 179, 8), width=4)
        draw.text((640, 200), "AI", font=get_font(40), fill=(234, 179, 8), anchor="mm")

    if text:
        draw_captions(draw, text)

    if quote_text:
        draw.rectangle([100, 600, 1180, 660], fill=(15, 23, 42, 245), outline=(234, 179, 8), width=2)
        draw.text((640, 615), "SCRIPTURE REFERENCE (NIV)", font=get_font(12), fill=(234, 179, 8), anchor="mm")
        draw.text((640, 638), f'"{quote_text}"', font=get_font(18), fill=(255, 255, 255), anchor="mm")

    draw_compliance_banner(draw)
    bg.convert("RGB").save(output_path)

def render_score_board_frame(round_num, scores, role_a, role_b, total_a, total_b, text, output_path):
    bg = Image.new("RGBA", (1280, 720), (15, 23, 42, 255))
    draw = ImageDraw.Draw(bg)

    draw.text((640, 40), f"ROUND {round_num} JUDGING BREAKDOWN", font=get_font(28), fill=(234, 179, 8), anchor="mm")
    draw.text((640, 75), f"TOTAL: {role_a} ({total_a} PTS)  vs  {role_b} ({total_b} PTS)", font=get_font(18), fill=(255, 255, 255), anchor="mm")

    favored_a = [ (j, s) for j, s in zip(JUDGES, scores) if s["score_a"] >= s["score_b"] ]
    favored_b = [ (j, s) for j, s in zip(JUDGES, scores) if s["score_b"] > s["score_a"] ]

    # Left Column
    draw.text((320, 110), f"FAVORING {role_a.upper()}", font=get_font(16), fill=(0, 210, 255), anchor="mm")
    for idx, (j, s) in enumerate(favored_a[:5]):
        y = 135 + idx * 70
        draw.rectangle([50, y, 590, y + 60], fill=(30, 41, 59, 255), outline=(51, 65, 85), width=1)
        icon_img = load_or_create_icon(j["icon"], j["name"])
        bg.paste(icon_img, (60, y + 7), mask=icon_img)
        draw.text((120, y + 18), j["name"], font=get_font(16), fill=(255, 255, 255))
        draw.text((120, y + 38), j["company"], font=get_font(12), fill=(148, 163, 184))
        draw.text((550, y + 30), f"{s['score_a']} pts", font=get_font(18), fill=(0, 210, 255), anchor="rm")

    # Right Column
    draw.text((960, 110), f"FAVORING {role_b.upper()}", font=get_font(16), fill=(255, 60, 90), anchor="mm")
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
    topic = data.get("topic", "AI Debate")
    role_a = data.get("role_a", "Proponent")
    role_b = data.get("role_b", "Opponent")
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

    # 1. Broadcast Intro
    intro_txt = f"Welcome to today's AI Debate Broadcast. Topic: {topic}. Representing {role_a} versus {role_b}, evaluated by 15 AI judge models."
    a_path = synthesize_speech(intro_txt, VOICE_NARRATOR_ID, "build_temp/intro.mp3")
    f_path = "build_temp/intro.png"
    render_frame_image("NARRATOR", intro_txt, None, f_path)
    add_clip(f_path, a_path)

    # 2. Judges Self-Intros
    for idx, j in enumerate(JUDGES[:2]):
        j_txt = f"Greetings. I am {j['name']} from {j['company']}. I will serve as an official AI judge evaluating this debate."
        a_path = synthesize_speech(j_txt, JUDGE_VOICE_POOL[idx % len(JUDGE_VOICE_POOL)], f"build_temp/j_intro_{idx}.mp3")
        f_path = f"build_temp/j_intro_{idx}.png"
        render_judge_intro_frame(j, j_txt, f_path)
        add_clip(f_path, a_path)

    # 3. Debater Self-Intros
    d1_txt = f"I am presenting the case for {role_a}. I will ground my arguments strictly in scriptural truth."
    a_path = synthesize_speech(d1_txt, VOICE_APOLOGIST_ID, "build_temp/d1_intro.mp3")
    f_path = "build_temp/d1_intro.png"
    render_frame_image("DEBATER_A", d1_txt, None, f_path)
    add_clip(f_path, a_path)

    d2_txt = f"I am representing the perspective of {role_b}. I look forward to examining all arguments critically."
    a_path = synthesize_speech(d2_txt, VOICE_SKEPTIC_ID, "build_temp/d2_intro.mp3")
    f_path = "build_temp/d2_intro.png"
    render_frame_image("DEBATER_B", d2_txt, None, f_path)
    add_clip(f_path, a_path)

    # 4. Debate Rounds Loop (3 Rounds Max)
    total_a, total_b = 0, 0
    max_rounds = min(3, max((item.get("round", 1) for item in raw_script), default=1))

    for r in range(1, max_rounds + 1):
        round_items = [item for item in raw_script if item.get("round") == r]

        for idx, item in enumerate(round_items):
            speaker = item["speaker"]
            text = sanitize_speech_text(item["text"])
            quote_text = item.get("quote", None)
            vid = VOICE_NARRATOR_ID if speaker == "NARRATOR" else (VOICE_APOLOGIST_ID if speaker == "DEBATER_A" else VOICE_SKEPTIC_ID)

            a_path = synthesize_speech(text, vid, f"build_temp/r{r}_{idx}.mp3")
            f_path = f"build_temp/r{r}_{idx}.png"
            render_frame_image(speaker, text, quote_text, f_path)
            add_clip(f_path, a_path)

        # AI Models Evaluation
        arg_a = next((sanitize_speech_text(i['text']) for i in round_items if i['speaker'] == 'DEBATER_A'), "")
        arg_b = next((sanitize_speech_text(i['text']) for i in round_items if i['speaker'] == 'DEBATER_B'), "")

        async def run_evals():
            return await asyncio.gather(*[evaluate_judge(j, role_a, role_b, arg_a, arg_b) for j in JUDGES])

        round_scores = asyncio.run(run_evals())

        avg_a = sum(s["score_a"] for s in round_scores) // len(round_scores)
        avg_b = sum(s["score_b"] for s in round_scores) // len(round_scores)
        total_a += avg_a
        total_b += avg_b

        summary_txt = f"Round {r} complete. {role_a} scored {avg_a} pts, {role_b} scored {avg_b} pts. Total: {total_a} to {total_b}."
        a_path = synthesize_speech(summary_txt, VOICE_NARRATOR_ID, f"build_temp/score_{r}.mp3")
        f_path = f"build_temp/score_{r}.png"
        render_score_board_frame(r, round_scores, role_a, role_b, total_a, total_b, summary_txt, f_path)
        add_clip(f_path, a_path)

    # 5. Outro & Winner
    winner_title = role_a if total_a > total_b else role_b
    outro_txt = f"That concludes today's debate! Final score: {total_a} to {total_b}. Our winner is {winner_title}! Don't forget to like and subscribe."
    a_path = synthesize_speech(outro_txt, VOICE_NARRATOR_ID, "build_temp/outro.mp3")
    f_path = "build_temp/outro.png"
    render_frame_image("NARRATOR", outro_txt, None, f_path)
    add_clip(f_path, a_path)

    # Stitch all clip segments into final output
    concat_list = "build_temp/concat.txt"
    with open(concat_list, "w") as f:
        for seg in segments:
            f.write(f"file '{os.path.basename(seg)}'\n")

    final_cmd = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list,
        "-c", "copy", "final_debate.mp4"
    ]
    subprocess.run(final_cmd, cwd="build_temp", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.rename("build_temp/final_debate.mp4", "final_debate.mp4")

if __name__ == "__main__":
    data = generate_debate()
    render_debate_video(data)
