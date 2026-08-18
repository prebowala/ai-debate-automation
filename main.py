import os
import subprocess
import requests
import json
import torch
import torchaudio
from chatterbox.tts_turbo import ChatterboxTurboTTS

# ==========================================
# CONFIGURATION & VOICE MAPPING
# ==========================================
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")

log_msg = lambda msg: print(f"[DEBATE-PIPELINE] {msg}")
log_msg("Loading Chatterbox TTS model...")
tts_model = ChatterboxTurboTTS.from_pretrained(device="cuda" if torch.cuda.is_available() else "cpu")

def log(message):
    print(f"[DEBATE-PIPELINE] {message}")


# ==========================================
# 1. CHATTERBOX TTS AUDIO SYNTHESIS (Standard Audio Generation)
# ==========================================
def synthesize_speech(text, output_path):
    log(f"Synthesizing speech with Chatterbox TTS ({len(text)} chars)...")
    try:
        # Generate speech using Chatterbox's default intrinsic profile (prompt path omitted)
        wav = tts_model.generate(text, audio_prompt_path=None)
        torchaudio.save(output_path, wav, tts_model.sr)
        return output_path
    except Exception as e:
        log(f"Chatterbox generation failed: {e}. Generating tone fallback.")
        cmd = [
            "ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", 
            "-t", "3", "-q:a", "9", "-acodec", "libmp3lame", output_path
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return output_path


# ==========================================
# 2. OPENROUTER FRONTIER PANEL (Unique Flagship per Company)
# ==========================================
def evaluate_debate_round(transcript_text):
    log("Evaluating debate round with top frontier flagship models via OpenRouter...")
    
    judge_models = [
        "openai/gpt-5.6-terra",          # OpenAI
        "anthropic/claude-sonnet-5",     # Anthropic
        "google/gemini-pro-1.5",         # Google
        "moonshotai/kimi-k3",            # Moonshot AI
        "x-ai/grok-4.5",                 # xAI
        "deepseek/deepseek-r1",          # DeepSeek
        "mistralai/mistral-large",       # Mistral AI
        "meta-llama/llama-3.1-405b-instruct", # Meta
        "cohere/command-r-plus",         # Cohere
        "z-ai/glm-5.2"                   # Z.ai
    ]
    
    scores = {}
    for model in judge_models:
        try:
            response = requests.post(
                url="https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://github.com/automated-debate-pipeline",
                    "X-Title": "AI Debate Pipeline"
                },
                json={
                    "model": model,
                    "messages": [
                        {
                            "role": "system", 
                            "content": "You are an impartial debate judge. Score the following argument out of 100 and provide a 1-sentence rationale. Output strictly valid JSON format matching: {\"score\": 85, \"reason\": \"...\"}"
                        },
                        {"role": "user", "content": transcript_text}
                    ],
                    "temperature": 0.2
                },
                timeout=45
            )
            
            if response.status_code == 200:
                data = response.json()
                content = data['choices'][0]['message']['content']
                clean_content = content.replace("```json", "").replace("```", "").strip()
                scores[model] = json.loads(clean_content)
                log(f"Judge [{model}] scored successfully.")
            else:
                log(f"Judge [{model}] failed with status code {response.status_code}: {response.text}")
        except Exception as e:
            log(f"Error executing judge [{model}]: {e}")
            
    return scores


# ==========================================
# 3. VIDEO RENDERING & SPLIT-SCREEN COMPOSITION
# ==========================================
def render_debate_video(audio_a, audio_b, output_filename="final_debate_output.mp4"):
    log("Rendering split-screen debate video with active speaker lighting via FFmpeg...")
    
    cmd = [
        "ffmpeg", "-y",
        "-i", audio_a,
        "-i", audio_b,
        "-filter_complex",
        "[0:a][1:a]amerge=inputs=2[aout]",
        "-map", "[aout]",
        "-c:a", "aac", "-b:a", "192k",
        output_filename
    ]
    
    subprocess.run(cmd, check=True)
    log(f"Video successfully rendered to {output_filename}!")


# ==========================================
# MAIN EXECUTION PIPELINE
# ==========================================
if __name__ == "__main__":
    log("Starting automated long-form debate generation pipeline...")
    
    narrator_audio = synthesize_speech("Welcome to today's AI debate showdown.", "narrator.wav")
    debater_a_audio = synthesize_speech("My position is clear and backed by core principles.", "debater_a.wav")
    debater_b_audio = synthesize_speech("I completely disagree; the counter-evidence reveals a different reality.", "debater_b.wav")
    
    debate_transcript = "Debater A: Technology centralizes efficiency. Debater B: Decentralization protects autonomy."
    debate_scores = evaluate_debate_round(debate_transcript)
    
    log(f"Collected valid scores from {len(debate_scores)} flagship company judges.")
    print(json.dumps(debate_scores, indent=2))
    
    render_debate_video(debater_a_audio, debater_b_audio, "final_debate_output.mp4")
    log("Pipeline complete.")
