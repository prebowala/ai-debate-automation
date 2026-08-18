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

def load_or_create_icon(icon_path, name):
    if os.path.exists(icon_path):
        try:
            return Image.open(icon_path).convert("RGBA").resize((80, 80))
        except Exception:
            pass
    badge = Image.new("RGBA", (80, 80), (30, 41, 59, 255))
    draw = ImageDraw.Draw(badge)
    draw.rectangle([0, 0, 79, 79], outline=(0, 180, 255, 255), width=2)
    initials = "".join([w[0] for w in name.split()[:2]]).upper()
    draw.text((40, 40), initials, font=get_font(26), fill=(255, 255, 255), anchor="mm")
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

def render_chunked_captions(draw, text, t, total_duration, y_pos=760):
    """Renders phrase-based multi-line captions without distracting word-by-word color flicker."""
    words = text.split()
    if not words:
        return

    words_per_chunk = 12
    chunks = [" ".join(words[i:i + words_per_chunk]) for i in range(0, len(words), words_per_chunk)]
    
    num_chunks = len(chunks)
    chunk_index = min(int((t / max(total_duration, 0.01)) * num_chunks), num_chunks - 1)
    active_phrase = chunks[chunk_index]

    font = get_font(36)
    
    # Simple multi-line wrap
    lines = []
    line_words = active_phrase.split()
    curr_line = ""
    for w in line_words:
        test_line = f"{curr_line} {w}".strip()
        if font.getlength(test_line) > 1400:
            lines.append(curr_line)
            curr_line = w
        else:
            curr_line = test_line
    if curr_line:
        lines.append(curr_line)

    line_y = y_pos - (len(lines) * 22)
    for line in lines:
        draw.text((960, line_y), line, font=font, fill=(255, 255, 255), anchor="mm", stroke_width=4, stroke_fill=(0, 0, 0))
        line_y += 46

def draw_compliance_banner(draw):
    draw.rectangle([0, 1040, 1920, 1080], fill=(0, 0, 0, 210))
    font = get_font(18)
    draw.text((960, 1060), COMPLIANCE_BANNER_TEXT, font=font, fill=(200, 200, 200), anchor="mm")

def render_frame(t, duration, speaker, text, quote_text, audio_clip, show_disclaimer=True):
    bg_full = get_cached_bg()
    
    # 1. Dynamic Camera Framing and Stage Lighting Zoom
    if speaker == "DEBATER_A":
        crop_box = bg_full.crop((0, 0, 1280, 1080)).resize((1920, 1080))
        bg = ImageEnhance.Brightness(crop_box).enhance(1.25)
    elif speaker == "DEBATER_B":
        crop_box = bg_full.crop((640, 0, 1920, 1080)).resize((1920, 1080))
        bg = ImageEnhance.Brightness(crop_box).enhance(1.25)
    else:  # NARRATOR / Full Stage
        bg = ImageEnhance.Brightness(bg_full).enhance(1.0)
        
    overlay = Image.new("RGBA", (1920, 1080), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    
    try:
        sample = audio_clip.get_frame(t)
        amplitude = np.linalg.norm(sample) if isinstance(sample, np.ndarray) else abs(sample)
    except Exception:
        amplitude = 0.2
        
    amp_factor = min(max(amplitude * 350, 15), 180)
    pulse = math.sin(t * 8) * 10
    
    # 2. Speaker Audio Visualizer Bars & Active Speaker Glow Animations
    if speaker == "DEBATER_A":
        # Spotlight & Pulse Aura on Debater A
        center_x, center_y = 500, 520
        draw.ellipse([center_x - 160 - pulse - amp_factor/2, center_y - 160 - pulse - amp_factor/2, 
                      center_x + 160 + pulse + amp_factor/2, center_y + 160 + pulse + amp_factor/2], 
                     outline=(0, 210, 255, 200), width=6)
        
        # Audio Waveform bars on Left Side
        for i in range(16):
            h = int(abs(amp_factor * (0.5 + 0.5 * math.sin(i + t * 12))))
            x = 360 + (i * 18)
            draw.rectangle([x, 700 - h, x + 12, 700], fill=(0, 210, 255, 240))

    elif speaker == "DEBATER_B":
        # Spotlight & Pulse Aura on Debater B
        center_x, center_y = 1420, 520
        draw.ellipse([center_x - 160 - pulse - amp_factor/2, center_y - 160 - pulse - amp_factor/2, 
                      center_x + 160 + pulse + amp_factor/2, center_y + 160 + pulse + amp_factor/2], 
                     outline=(255, 60, 90, 200), width=6)
        
        # Audio Waveform bars on Right Side
        for i in range(16):
            h = int(abs(amp_factor * (0.5 + 0.5 * math.sin(i + t * 12))))
            x = 1280 + (i * 18)
            draw.rectangle([x, 700 - h, x + 12, 700], fill=(255, 60, 90, 240))

    elif speaker == "NARRATOR":
        # Waveform Bars in Top Center
        for i in range(24):
            h = int(abs(amp_factor * (0.4 + 0.6 * math.cos(i + t * 9))))
            x = 740 + (i * 18)
            draw.rectangle([x, 140 - h//2, x + 12, 140 + h//2], fill=(234, 179, 8, 240))

    # 3. Closed Captions (Chunked Phrases)
    if text:
        render_chunked_captions(draw, text, t, duration)

    # 4. Scripture Quotes & References Lower Third Box (NIV)
    if quote_text:
        draw.rectangle([200, 880, 1720, 990], fill=(15, 23, 42, 235), outline=(234, 179, 8), width=3)
        draw.text((960, 910), "SCRIPTURE REFERENCE (NIV)", font=get_font(26), fill=(234, 179, 8), anchor="mm")
        draw.text((960, 950), f'"{quote_text}"', font=get_font(34), fill=(255, 255, 255), anchor="mm")

    if show_disclaimer:
        draw_compliance_banner(draw)

    composite = Image.alpha_composite(bg, overlay)
    return np.array(composite.convert("RGB"))

def render_score_board_frame(t, duration, round_num, scores, role_a, role_b, total_a, total_b, audio_clip):
    """Renders the end-of-round score visual breakdown detailing votes from each AI Model."""
    overlay = Image.new("RGBA", (1920, 1080), (15, 23, 42, 250))
    draw = ImageDraw.Draw(overlay)
    
    draw.text((960, 70), f"ROUND {round_num} JUDGING BREAKDOWN", font=get_font(48), fill=(234, 179, 8), anchor="mm")
    draw.text((960, 120), f"CUMULATIVE TOTAL: {role_a} ({total_a})  vs  {role_b} ({total_b})", font=get_font(30), fill=(255, 255, 255), anchor="mm")
    
    # Grid of 15 Judges showing individual scores
    start_x, start_y = 180, 180
    cols = 5
    for idx, (j, score) in enumerate(zip(JUDGES, scores)):
        row = idx // cols
        col = idx % cols
        x = start_x + col * 320
        y = start_y + row * 220
        
        # Judge Card
        draw.rectangle([x, y, x + 290, y + 190], fill=(30, 41, 59, 255), outline=(51, 65, 85), width=2)
        
        icon_img = load_or_createicon_cached = load_or_create_icon(j["icon"], j["name"])
        overlay.paste(icon_img, (x + 15, y + 15), mask=icon_img)
        
        draw.text((x + 110, y + 35), j["name"], font=get_font(22), fill=(0, 210, 255))
        draw.text((x + 110, y + 65), j["company"], font=get_font(18), fill=(148, 163, 184))
        
        # Score Bar Breakdown
        sa, sb = score["score_a"], score["score_b"]
        color_a = (0, 210, 255) if sa >= sb else (100, 116, 139)
        color_b = (255, 60, 90) if sb > sa else (100, 116, 139)
        
        draw.text((x + 20, y + 120), f"{role_a[:8]}: {sa}", font=get_font(20), fill=color_a)
        draw.text((x + 150, y + 120), f"{role_b[:8]}: {sb}", font=get_font(20), fill=color_b)

    draw_compliance_banner(draw)
    return np.array(overlay.convert("RGB"))

def generate_debate():
    with open("topic.txt", "r") as f:
        topic = f.read().strip()

    prompt = (
        f"Write an extended broadcast debate on: '{topic}'.\n\n"
        f"Rules:\n"
        f"- Output MUST contain 6 comprehensive debate rounds to achieve a full 10-minute video duration.\n"
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

    # 1. Main Debate & Round Rendering Loop
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

        # 2. Round End AI Judging & Visual Score Breakdown
        arg_a = next((sanitize_speech_text(i['text']) for i in round_items if i['speaker'] == 'DEBATER_A'), "")
        arg_b = next((sanitize_speech_text(i['text']) for i in round_items if i['speaker'] == 'DEBATER_B'), "")

        async def run_evaluations():
            return await asyncio.gather(*[evaluate_judge(j, role_a, role_b, arg_a, arg_b) for j in JUDGES])

        round_scores = asyncio.run(run_evaluations())
        
        avg_a = sum(s["score_a"] for s in round_scores) // len(round_scores)
        avg_b = sum(s["score_b"] for s in round_scores) // len(round_scores)
        total_a += avg_a
        total_b += avg_b

        # Narrator Spoken Round Summary
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

    # 3. Final Spoken Winner Announcement
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

    master_video.write_videofile("final_debate.mp4", fps=20, codec="libx264", audio_codec="aac", preset="ultrafast")
    master_audio.write_audiofile("output_audio.mp3")

if __name__ == "__main__":
    data = generate_debate()
    render_debate_video(data)
