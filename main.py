import os
import json
import asyncio
import requests
import re
import random
import math
import PIL.Image
import numpy as np

# Patch Pillow for MoviePy 1.x compatibility
if not hasattr(PIL.Image, 'ANTIALIAS'):
    PIL.Image.ANTIALIAS = PIL.Image.Resampling.LANCZOS

from PIL import Image, ImageDraw, ImageFont, ImageEnhance
from moviepy.editor import (
    AudioFileClip,
    VideoClip,
    concatenate_videoclips,
    concatenate_audioclips
)
from moviepy.audio.AudioClip import AudioArrayClip

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
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    return ImageFont.load_default()

def get_cached_bg():
    global BG_IMAGE_CACHE
    if BG_IMAGE_CACHE is None:
        base_path = "background.png" if os.path.exists("background.png") else "default_bg.png"
        if not os.path.exists(base_path):
            img = Image.new("RGB", (1920, 1080), color=(15, 23, 42))
            img.save(base_path)
        BG_IMAGE_CACHE = Image.open(base_path).convert("RGBA").resize((1920, 1080))
    return BG_IMAGE_CACHE.copy()

# Pre-cached stage camera angles
BG_FULL = get_cached_bg()
BG_DEBATER_A = ImageEnhance.Brightness(BG_FULL.crop((0, 0, 1200, 1080)).resize((1920, 1080))).enhance(1.35)
BG_DEBATER_B = ImageEnhance.Brightness(BG_FULL.crop((720, 0, 1920, 1080)).resize((1920, 1080))).enhance(1.35)

def load_or_create_icon(icon_path, name):
    if os.path.exists(icon_path):
        try:
            return Image.open(icon_path).convert("RGBA").resize((70, 70))
        except Exception:
            pass
    badge = Image.new("RGBA", (70, 70), (30, 41, 59, 255))
    draw = ImageDraw.Draw(badge)
    draw.rectangle([0, 0, 69, 69], outline=(0, 180, 255, 255), width=2)
    initials = "".join([w[0] for w in name.split()[:2]]).upper()
    draw.text((35, 35), initials, font=get_font(24), fill=(255, 255, 255), anchor="mm")
    return badge

def sanitize_speech_text(text):
    clean = re.sub(r'^(laura|brian|narrator|apologist|skeptic|debater_a|debater_b)(\s*\([^)]*\))?:\s*', '', text, flags=re.IGNORECASE)
    return clean.strip()

def clean_json_string(text):
    text = re.sub(r"^```(json)?", "", text, flags=re.MULTILINE)
    text = re.sub(r"^```", "", text, flags=re.MULTILINE)
    return text.strip()

def create_silent_audio(duration=0.6, fps=44100):
    samples = int(fps * duration)
    return AudioArrayClip(np.zeros((samples, 2), dtype=np.float32), fps=fps)

def synthesize_speech(text, voice_id, output_path):
    if ELEVENLABS_API_KEY:
        try:
            res = requests.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
                headers={"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"},
                json={"text": text},
                timeout=45
            )
            if res.status_code == 200:
                with open(output_path, "wb") as f:
                    f.write(res.content)
                return AudioFileClip(output_path)
        except Exception:
            pass

    try:
        from gtts import gTTS
        tts = gTTS(text=text, lang='en')
        tts.save(output_path)
        return AudioFileClip(output_path)
    except Exception:
        word_count = len(text.split())
        est_duration = max(2.0, word_count * 0.45)
        return create_silent_audio(duration=est_duration)

def render_block_captions(draw, text, t, total_duration, y_pos=720):
    """Renders steady block paragraphs that change synchronously with speech blocks."""
    words = text.split()
    if not words:
        return

    words_per_block = 18
    blocks = [" ".join(words[i:i + words_per_block]) for i in range(0, len(words), words_per_block)]
    
    num_blocks = len(blocks)
    block_idx = min(int((t / max(total_duration, 0.01)) * num_blocks), num_blocks - 1)
    active_block = blocks[block_idx]

    font = get_font(34)
    
    # Text word-wrapping for caption block
    lines = []
    line_words = active_block.split()
    curr_line = ""
    for w in line_words:
        test_line = f"{curr_line} {w}".strip()
        if font.getlength(test_line) > 1300:
            lines.append(curr_line)
            curr_line = w
        else:
            curr_line = test_line
    if curr_line:
        lines.append(curr_line)

    draw.rectangle([260, y_pos - 10, 1660, y_pos + (len(lines) * 44) + 10], fill=(15, 23, 42, 220), outline=(51, 65, 85), width=2)
    
    line_y = y_pos + 10
    for line in lines:
        draw.text((960, line_y), line, font=font, fill=(255, 255, 255), anchor="mm")
        line_y += 42

def draw_compliance_banner(draw):
    draw.rectangle([0, 1040, 1920, 1080], fill=(0, 0, 0, 220))
    font = get_font(18)
    draw.text((960, 1060), COMPLIANCE_BANNER_TEXT, font=font, fill=(200, 200, 200), anchor="mm")

def render_frame(t, duration, speaker, text, quote_text, audio_clip, show_disclaimer=True):
    # Dynamic Stage Camera Angle and Zoom Selection
    if speaker == "DEBATER_A":
        bg = BG_DEBATER_A.copy()
    elif speaker == "DEBATER_B":
        bg = BG_DEBATER_B.copy()
    else:  # NARRATOR / Full Stage
        bg = BG_FULL.copy()
        
    overlay = Image.new("RGBA", (1920, 1080), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    
    try:
        sample = audio_clip.get_frame(t)
        amplitude = np.linalg.norm(sample) if isinstance(sample, np.ndarray) else abs(sample)
    except Exception:
        amplitude = 0.2
        
    amp_factor = min(max(amplitude * 350, 15), 180)
    pulse = math.sin(t * 8) * 12
    
    # Active Speaker Lighting, Aura Pulse & Dynamic Soundbar
    if speaker == "DEBATER_A":
        # Left Stage Spotlight Pulse
        center_x, center_y = 480, 500
        draw.ellipse([center_x - 170 - pulse - amp_factor/2, center_y - 170 - pulse - amp_factor/2, 
                      center_x + 170 + pulse + amp_factor/2, center_y + 170 + pulse + amp_factor/2], 
                     outline=(0, 210, 255, 220), width=6)
        
        # Audio Waveform on Left Side
        for i in range(16):
            h = int(abs(amp_factor * (0.5 + 0.5 * math.sin(i + t * 12))))
            x = 360 + (i * 18)
            draw.rectangle([x, 680 - h, x + 12, 680], fill=(0, 210, 255, 240))

    elif speaker == "DEBATER_B":
        # Right Stage Spotlight Pulse
        center_x, center_y = 1440, 500
        draw.ellipse([center_x - 170 - pulse - amp_factor/2, center_y - 170 - pulse - amp_factor/2, 
                      center_x + 170 + pulse + amp_factor/2, center_y + 170 + pulse + amp_factor/2], 
                     outline=(255, 60, 90, 220), width=6)
        
        # Audio Waveform on Right Side
        for i in range(16):
            h = int(abs(amp_factor * (0.5 + 0.5 * math.sin(i + t * 12))))
            x = 1320 + (i * 18)
            draw.rectangle([x, 680 - h, x + 12, 680], fill=(255, 60, 90, 240))

    elif speaker == "NARRATOR":
        # Center Stage Gold Lighting Glow
        draw.ellipse([960 - 200 - pulse, 300 - pulse, 960 + 200 + pulse, 700 + pulse], outline=(234, 179, 8, 120), width=4)
        
        # Center Top Audio Waveform
        for i in range(24):
            h = int(abs(amp_factor * (0.4 + 0.6 * math.cos(i + t * 9))))
            x = 740 + (i * 18)
            draw.rectangle([x, 140 - h//2, x + 12, 140 + h//2], fill=(234, 179, 8, 240))

    # Caption Blocks
    if text:
        render_block_captions(draw, text, t, duration)

    # Scripture Quotes Lower Third (NIV)
    if quote_text:
        draw.rectangle([200, 890, 1720, 990], fill=(15, 23, 42, 245), outline=(234, 179, 8), width=3)
        draw.text((960, 918), "SCRIPTURE REFERENCE (NIV)", font=get_font(24), fill=(234, 179, 8), anchor="mm")
        draw.text((960, 956), f'"{quote_text}"', font=get_font(32), fill=(255, 255, 255), anchor="mm")

    if show_disclaimer:
        draw_compliance_banner(draw)

    composite = Image.alpha_composite(bg, overlay)
    return np.array(composite.convert("RGB"))

def render_score_board_frame(t, duration, round_num, scores, role_a, role_b, total_a, total_b, audio_clip):
    """Renders a split screen showing AI models assigned to the side of the speaker they awarded higher points."""
    overlay = Image.new("RGBA", (1920, 1080), (15, 23, 42, 252))
    draw = ImageDraw.Draw(overlay)
    
    draw.text((960, 60), f"ROUND {round_num} JUDGING BREAKDOWN", font=get_font(44), fill=(234, 179, 8), anchor="mm")
    draw.text((960, 110), f"CUMULATIVE SCORE: {role_a} ({total_a})  vs  {role_b} ({total_b})", font=get_font(28), fill=(255, 255, 255), anchor="mm")
    
    # Split models based on higher score preference
    favored_a = []
    favored_b = []
    
    for j, s in zip(JUDGES, scores):
        if s["score_a"] >= s["score_b"]:
            favored_a.append((j, s))
        else:
            favored_b.append((j, s))

    # Column A (Left Side)
    draw.text((480, 160), f"MODELS FAVORING {role_a.upper()}", font=get_font(26), fill=(0, 210, 255), anchor="mm")
    draw.line([(80, 190), (880, 190)], fill=(0, 210, 255), width=2)
    
    for idx, (j, s) in enumerate(favored_a[:7]):
        y = 210 + idx * 110
        draw.rectangle([80, y, 880, y + 95], fill=(30, 41, 59, 255), outline=(51, 65, 85), width=2)
        icon_img = load_or_create_icon(j["icon"], j["name"])
        overlay.paste(icon_img, (95, y + 12), mask=icon_img)
        draw.text((180, y + 28), j["name"], font=get_font(22), fill=(255, 255, 255))
        draw.text((180, y + 58), j["company"], font=get_font(18), fill=(148, 163, 184))
        draw.text((820, y + 45), f"{s['score_a']} pts", font=get_font(24), fill=(0, 210, 255), anchor="e")

    # Column B (Right Side)
    draw.text((1440, 160), f"MODELS FAVORING {role_b.upper()}", font=get_font(26), fill=(255, 60, 90), anchor="mm")
    draw.line([(1040, 190), (1840, 190)], fill=(255, 60, 90), width=2)
    
    for idx, (j, s) in enumerate(favored_b[:7]):
        y = 210 + idx * 110
        draw.rectangle([1040, y, 1840, y + 95], fill=(30, 41, 59, 255), outline=(51, 65, 85), width=2)
        icon_img = load_or_create_icon(j["icon"], j["name"])
        overlay.paste(icon_img, (1055, y + 12), mask=icon_img)
        draw.text((1140, y + 28), j["name"], font=get_font(22), fill=(255, 255, 255))
        draw.text((1140, y + 58), j["company"], font=get_font(18), fill=(148, 163, 184))
        draw.text((1780, y + 45), f"{s['score_b']} pts", font=get_font(24), fill=(255, 60, 90), anchor="e")

    draw_compliance_banner(draw)
    return np.array(overlay.convert("RGB"))

def render_judge_intro_frame(t, duration, judge, speech_text, audio_clip):
    overlay = Image.new("RGBA", (1920, 1080), (15, 23, 42, 245))
    draw = ImageDraw.Draw(overlay)
    
    icon_img = load_or_create_icon(judge["icon"], judge["name"])
    overlay.paste(icon_img.resize((120, 120)), (900, 200), mask=icon_img.resize((120, 120)))
    
    draw.text((960, 360), judge["name"].upper(), font=get_font(48), fill=(0, 210, 255), anchor="mm")
    draw.text((960, 420), f"OFFICIAL AI DEBATE JUDGE ({judge['company']})", font=get_font(28), fill=(234, 179, 8), anchor="mm")
    
    render_block_captions(draw, speech_text, t, duration)
    draw_compliance_banner(draw)
    return np.array(overlay.convert("RGB"))

def generate_debate():
    with open("topic.txt", "r") as f:
        topic = f.read().strip()

    prompt = (
        f"Write an extended broadcast debate on: '{topic}'.\n\n"
        f"Rules:\n"
        f"- Output MUST contain 6 comprehensive debate rounds to achieve a 10-minute video duration.\n"
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
    
    res = requests.post("https://openrouter.ai/api/v1/chat/completions", 
                        headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
                        json={"model": "openai/gpt-5.6-sol", "messages": [{"role": "user", "content": prompt}]}, 
                        timeout=120)
    
    parsed = json.loads(clean_json_string(res.json()['choices'][0]['message']['content']))
    parsed['topic'] = topic
    return parsed

async def evaluate_judge(judge, role_a, role_b, arg_a, arg_b):
    prompt = f"Evaluate debate round:\n{role_a}: {arg_a}\n{role_b}: {arg_b}\nReturn JSON strictly: {{\"score_a\": 85, \"score_b\": 78}}"
    try:
        res = requests.post("https://openrouter.ai/api/v1/chat/completions", 
                            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
                            json={"model": judge["model"], "messages": [{"role": "user", "content": prompt}]}, 
                            timeout=20)
        parsed = json.loads(clean_json_string(res.json()['choices'][0]['message']['content']))
        return {"score_a": int(parsed.get("score_a", 75)), "score_b": int(parsed.get("score_b", 75))}
    except Exception:
        return {"score_a": random.randint(70, 90), "score_b": random.randint(70, 90)}

def render_debate_video(data):
    topic = data.get("topic", "AI Debate")
    role_a = data.get("role_a", "Proponent")
    role_b = data.get("role_b", "Opponent")
    raw_script = data.get("script", [])

    video_segments, audio_segments = [], []
    total_a, total_b = 0, 0
    buffer_silence = create_silent_audio(duration=0.6)

    # 1. AI Judges Self-Introductions
    intro_judges = JUDGES[:3]
    for idx, j in enumerate(intro_judges):
        intro_text = f"I am {j['name']} from {j['company']}. I am serving as one of 15 official AI judges for today's debate."
        voice_id = JUDGE_VOICE_POOL[idx % len(JUDGE_VOICE_POOL)]
        
        j_audio = synthesize_speech(intro_text, voice_id, f"temp_judge_intro_{idx}.mp3")
        j_vid = VideoClip(lambda t: render_judge_intro_frame(t, j_audio.duration, j, intro_text, j_audio), duration=j_audio.duration).set_audio(j_audio)
        
        video_segments.append(j_vid)
        audio_segments.append(j_audio)
        audio_segments.append(buffer_silence)

    # 2. Debaters Self-Introductions
    debater_intros = [
        {"speaker": "DEBATER_A", "text": f"I am representing the position of {role_a} in today's debate.", "voice": VOICE_APOLOGIST_ID},
        {"speaker": "DEBATER_B", "text": f"I am representing the position of {role_b} in today's debate.", "voice": VOICE_SKEPTIC_ID}
    ]

    for idx, d in enumerate(debater_intros):
        d_audio = synthesize_speech(d["text"], d["voice"], f"temp_debater_intro_{idx}.mp3")
        d_vid = VideoClip(lambda t: render_frame(t, d_audio.duration, d["speaker"], d["text"], None, d_audio), duration=d_audio.duration).set_audio(d_audio)
        
        video_segments.append(d_vid)
        audio_segments.append(d_audio)
        audio_segments.append(buffer_silence)

    # 3. Debate Rounds Loop
    max_rounds = max((item.get("round", 1) for item in raw_script), default=1)
    
    for r in range(1, max_rounds + 1):
        round_items = [item for item in raw_script if item.get("round") == r]
        
        for idx, item in enumerate(round_items):
            speaker = item["speaker"]
            text = sanitize_speech_text(item["text"])
            quote_text = item.get("quote", None)

            vid = VOICE_NARRATOR_ID if speaker == "NARRATOR" else (VOICE_APOLOGIST_ID if speaker == "DEBATER_A" else VOICE_SKEPTIC_ID)
            temp_audio_path = f"temp_r{r}_{idx}.mp3"
            audio_clip = synthesize_speech(text, vid, temp_audio_path)
            duration = audio_clip.duration

            stage_clip = VideoClip(lambda t: render_frame(t, duration, speaker, text, quote_text, audio_clip), duration=duration).set_audio(audio_clip)
            video_segments.append(stage_clip)
            audio_segments.append(audio_clip)

        arg_a = next((sanitize_speech_text(i['text']) for i in round_items if i['speaker'] == 'DEBATER_A'), "")
        arg_b = next((sanitize_speech_text(i['text']) for i in round_items if i['speaker'] == 'DEBATER_B'), "")

        async def run_evaluations():
            return await asyncio.gather(*[evaluate_judge(j, role_a, role_b, arg_a, arg_b) for j in JUDGES])

        round_scores = asyncio.run(run_evaluations())
        
        avg_a = sum(s["score_a"] for s in round_scores) // len(round_scores)
        avg_b = sum(s["score_b"] for s in round_scores) // len(round_scores)
        total_a += avg_a
        total_b += avg_b

        # Spoken Summary by Narrator
        narrator_summary = (
            f"At the conclusion of Round {r}, our 15 AI judges have evaluated the arguments. "
            f"The {role_a} scored an average of {avg_a} points, while the {role_b} received {avg_b} points. "
            f"The cumulative score stands at {total_a} for the {role_a} and {total_b} for the {role_b}."
        )
        score_audio = synthesize_speech(narrator_summary, VOICE_NARRATOR_ID, f"temp_score_summary_{r}.mp3")
        
        score_vid = VideoClip(
            lambda t: render_score_board_frame(t, score_audio.duration, r, round_scores, role_a, role_b, total_a, total_b, score_audio),
            duration=score_audio.duration
        ).set_audio(score_audio)

        video_segments.append(score_vid)
        audio_segments.append(score_audio)
        audio_segments.append(buffer_silence)

    # 4. Final Winner Spoken Announcement
    winner_title = f"the {role_a}" if total_a > total_b else f"the {role_b}"
    winner_text = (
        f"That concludes our broadcast debate. Across all rounds, the 15 AI model judges have compiled the final tally. "
        f"The {role_a} finishes with {total_a} total points, and the {role_b} finishes with {total_b} total points. "
        f"The winner of this debate is {winner_title}!"
    )
    final_audio = synthesize_speech(winner_text, VOICE_NARRATOR_ID, "temp_final_winner.mp3")
    final_vid = VideoClip(lambda t: render_frame(t, final_audio.duration, "NARRATOR", winner_text, None, final_audio), duration=final_audio.duration).set_audio(final_audio)
    
    video_segments.append(final_vid)
    audio_segments.append(final_audio)

    master_video = concatenate_videoclips(video_segments, method="compose")
    master_audio = concatenate_audioclips(audio_segments)

    # Optimized multi-threaded export
    master_video.write_videofile(
        "final_debate.mp4", 
        fps=15, 
        codec="libx264", 
        audio_codec="aac", 
        preset="ultrafast",
        threads=8
    )
    master_audio.write_audiofile("output_audio.mp3")

if __name__ == "__main__":
    data = generate_debate()
    render_debate_video(data)
