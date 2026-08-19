import os
import subprocess
import requests
import json
from google import genai
from google.genai import types

# ==========================================
# CONFIGURATION & API CLIENTS
# ==========================================
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", OPENROUTER_API_KEY)

client = genai.Client(api_key=GEMINI_API_KEY)

def log(message):
    print(f"[DEBATE-PIPELINE] {message}", flush=True)


# ==========================================
# 1. GEMINI TTS AUDIO SYNTHESIS
# ==========================================
def synthesize_speech_gemini(text, output_path, voice_name="Puck"):
    log(f"Synthesizing speech with Gemini TTS (Voice: {voice_name}, {len(text)} chars)...")
    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=text,
            config=types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(
                            voice_name=voice_name
                        )
                    )
                )
            )
        )
        
        for candidate in response.candidates:
            for part in candidate.content.parts:
                if part.inline_data and part.inline_data.mime_type.startswith("audio/"):
                    pcm_bytes = part.inline_data.data
                    import wave
                    with wave.open(output_path, 'wb') as wf:
                        wf.setnchannels(1)
                        wf.setsampwidth(2) # 16-bit PCM
                        wf.setframerate(24000)
                        wf.writeframes(pcm_bytes)
                    log(f"Successfully generated {output_path} via Gemini TTS")
                    return output_path
                    
        raise Exception("No audio data returned in Gemini response payload.")
        
    except Exception as e:
        log(f"Gemini TTS failed: {e}. Using silent tone fallback.")
        cmd = [
            "ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono", 
            "-t", "5", "-q:a", "9", "-acodec", "libmp3lame", output_path
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return output_path


# ==========================================
# 2. DYNAMIC 5-JUDGE PANEL WITH DUAL TIMEOUTS & FALLBACKS
# ==========================================
def evaluate_debate_round(transcript_text):
    log("Evaluating debate round, targeting 5 active judges with automatic backups...")
    
    # Primary target list of 5 flagship judges
    primary_judges = [
        "openai/gpt-5.6-terra",          
        "anthropic/claude-sonnet-5",     
        "google/gemini-pro-1.5",         
        "moonshotai/kimi-k3",            
        "deepseek/deepseek-r1"           
    ]
    
    # Backup pool of remaining models to fill in if any primary judge times out
    backup_judges = [
        "x-ai/grok-4.5",                 
        "mistralai/mistral-large",       
        "meta-llama/llama-3.1-405b-instruct", 
        "cohere/command-r-plus",         
        "z-ai/glm-5.2"                   
    ]
    
    candidate_pool = primary_judges + backup_judges
    scores = {}
    target_score_count = 5
    
    for model in candidate_pool:
        if len(scores) >= target_score_count:
            log("Secured 5 successful judge scores. Breaking loop early.")
            break
            
        log(f"Attempting query to judge [{model}]...")
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
                timeout=(5, 10)  # (Connect timeout 5s, Read timeout 10s) to prevent hanging
            )
            
            log(f"Received response status {response.status_code} from [{model}]")
            if response.status_code == 200:
                data = response.json()
                content = data['choices'][0]['message']['content']
                clean_content = content.replace("```json", "").replace("```", "").strip()
                scores[model] = json.loads(clean_content)
                log(f"Judge [{model}] scored successfully. ({len(scores)}/{target_score_count})")
            else:
                log(f"Judge [{model}] failed with status code {response.status_code}. Trying backup...")
        except Exception as e:
            log(f"Skipping model [{model}] due to network/timeout error: {e}. Trying backup...")
            
    # Safety net: If OpenRouter is completely stalled, inject a mock placeholder so it never freezes the pipeline
    if len(scores) == 0:
        log("WARNING: All OpenRouter connections failed or timed out. Injecting fallback score.")
        scores["fallback/mock-judge"] = {"score": 75, "reason": "Network timeout fallback placeholder."}
            
    return scores


# ==========================================
# 3. VIDEO RENDERING & SPLIT-SCREEN COMPOSITION
# ==========================================
def render_debate_video(audio_a, audio_b, output_filename="final_debate_output.mp4"):
    log("Rendering split-screen video canvas with actual audio tracks via FFmpeg...")
    
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "color=c=navy:s=640x720:r=30",  
        "-f", "lavfi", "-i", "color=c=maroon:s=640x720:r=30", 
        "-i", audio_a,
        "-i", audio_b,
        "-filter_complex",
        (
            "[0:v][1:v]hstack=inputs=2[v_canvas];"
            "[2:a][3:a]concat=n=2:v=0:a=1[aout]"
        ),
        "-map", "[v_canvas]",
        "-map", "[aout]",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", 
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        output_filename
    ]
    
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log(f"Split-screen video successfully rendered to {output_filename}!")
    except subprocess.CalledProcessError as e:
        log(f"FFmpeg failed with exit code {e.returncode}")
        raise


# ==========================================
# MAIN EXECUTION PIPELINE
# ==========================================
if __name__ == "__main__":
    log("Starting automated long-form debate generation pipeline with Gemini TTS...")
    
    debater_a_audio = synthesize_speech_gemini("My position is clear and backed by core principles.", "debater_a.wav", voice_name="Puck")
    debater_b_audio = synthesize_speech_gemini("I completely disagree; the counter-evidence reveals a different reality.", "debater_b.wav", voice_name="Fenrir")
    
    debate_transcript = "Debater A: Technology centralizes efficiency. Debater B: Decentralization protects autonomy."
    debate_scores = evaluate_debate_round(debate_transcript)
    
    log(f"Collected valid scores from {len(debate_scores)} judges.")
    print(json.dumps(debate_scores, indent=2))
    
    render_debate_video(debater_a_audio, debater_b_audio, "final_debate_output.mp4")
    log("Pipeline complete.")
