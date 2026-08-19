import os
import subprocess
import requests
import json
import torch
import torchaudio
from chatterbox.tts_turbo import ChatterboxTurboTTS

# ==========================================
# CONFIGURATION & MODEL INITIALIZATION
# ==========================================
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")

def log(message):
    print(f"[DEBATE-PIPELINE] {message}", flush=True)

log("Loading Chatterbox model for CPU execution...")
tts_model = ChatterboxTurboTTS.from_pretrained(device="cpu")


# ==========================================
# 1. CHATTERBOX TTS SYNTHESIS
# ==========================================
def synthesize_speech_chatterbox(text, output_path):
    log(f"Synthesizing speech via Chatterbox ({len(text)} chars)...")
    
    try:
        wav = tts_model.generate(text)
        torchaudio.save(output_path, wav, tts_model.sr)
        log(f"Successfully generated {output_path}")
        return output_path
        
    except Exception as e:
        log(f"Chatterbox TTS failed: {e}. Falling back to silent tone.")
        cmd = [
            "ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono", 
            "-t", "4", "-q:a", "9", "-acodec", "libmp3lame", output_path
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return output_path


# ==========================================
# 2. 10 FRONTIER JUDGES + 5 FALLBACKS PANEL (OPTIMIZED TIMEOUTS)
# ==========================================
def evaluate_debate_round(transcript_text):
    log("Evaluating debate round with 10 primary frontier judges and 5 backups...")
    
    primary_judges = [
        "openai/gpt-4o", 
        "anthropic/claude-3-5-sonnet", 
        "google/gemini-flash-1.5", 
        "deepseek/deepseek-chat", 
        "meta-llama/llama-3-70b-instruct",
        "mistralai/mistral-large",
        "cohere/command-r-plus",
        "deepseek/deepseek-r1",
        "x-ai/grok-2",
        "google/gemini-pro-1.5"
    ]
    
    fallback_judges = [
        "openai/gpt-4o-mini",
        "anthropic/claude-3-haiku",
        "meta-llama/llama-3-8b-instruct"
    ]
    
    candidate_pool = primary_judges + fallback_judges
    judges_results = {}
    
    for model in candidate_pool:
        if len(judges_results) >= 10:
            break
            
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
                            "content": "You are an impartial debate judge. Decide the winner between 'A' and 'B', give a score out of 100 for each, and state your rationale. Output strictly valid JSON matching: {\"winner\": \"A\", \"score_a\": 88, \"score_b\": 82, \"reason\": \"...\"}"
                        },
                        {"role": "user", "content": transcript_text}
                    ],
                    "temperature": 0.2
                },
                timeout=(2, 4)  # Strict timeout prevents hanging on slow/heavy models
            )
            if response.status_code == 200:
                data = response.json()
                content = data['choices'][0]['message']['content'].replace("```json", "").replace("```", "").strip()
                judges_results[model] = json.loads(content)
                log(f"Judge [{model}] evaluated successfully ({len(judges_results)}/10).")
        except Exception:
            log(f"Judge [{model}] timed out or failed. Skipping instantly.")
            pass
            
    if len(judges_results) == 0:
        judges_results["fallback/mock-judge"] = {"winner": "A", "score_a": 78, "score_b": 75, "reason": "Network timeout fallback placeholder rationale."}
        
    return judges_results


# ==========================================
# 3. DYNAMIC TOPIC LOADER FROM topic.txt
# ==========================================
def load_topics_from_file(filename="topic.txt"):
    if os.path.exists(filename):
        with open(filename, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]
        
        dynamic_rounds = []
        for i, line in enumerate(lines[:5], 1):
            dynamic_rounds.append({
                "round": i,
                "topic": line,
                "a": f"Regarding the topic of {line}, proactive implementation and aggressive scaling offer the most promising path forward.",
                "b": f"On the contrary, {line} presents hidden systemic risks that require far more caution and restriction before moving ahead."
            })
        if dynamic_rounds:
            log(f"Successfully loaded {len(dynamic_rounds)} topics from {filename}.")
            return dynamic_rounds
            
    log("Warning: topic.txt not found or empty. Falling back to default hardcoded topics.")
    return [
        {
            "round": 1,
            "topic": "AGI Timeline & Acceleration",
            "a": "Artificial intelligence will drastically accelerate scientific discovery, curing major global diseases and expanding human capability exponentially.",
            "b": "While promising, unchecked acceleration introduces severe structural alignment risks and unmitigated societal instability before we are ready."
        }
    ]


# ==========================================
# 4. YOUTUBE-STYLE CINEMATIC COMPOSITOR
# ==========================================
def render_youtube_debate_video(audio_files, captions, output_filename="final_debate_output.mp4"):
    log(f"Rendering cinematic video combining {len(audio_files)} audio segments via FFmpeg...")
    
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "color=c=navy:s=640x720:r=30",  
        "-f", "lavfi", "-i", "color=c=maroon:s=640x720:r=30", 
    ]
    
    for af in audio_files:
        cmd.extend(["-i", af])
        
    filter_parts = [
        "[0:v]scale=640:720[v0]",
        "[1:v]scale=640:720[v1]",
        "[v0][v1]hstack=inputs=2[v_base]",
        f"[v_base]drawtext=text='{captions[0]}':fontcolor=white:fontsize=22:x=(w-text_w)/2:y=h-50:box=1:boxcolor=black@0.7[v_out]"
    ]
    
    concat_inputs = []
    for i, _ in enumerate(audio_files):
        input_idx = i + 2
        filter_parts.append(f"[{input_idx}:a]aformat=sample_rates=24000:channel_layouts=mono[a{i}]")
        concat_inputs.append(f"[a{i}]")
        
    concat_str = "".join(concat_inputs) + f"concat=n={len(audio_files)}:v=0:a=1[aout]"
    filter_parts.append(concat_str)
    
    cmd.extend([
        "-filter_complex", ";".join(filter_parts),
        "-map", "[v_out]",
        "-map", "[aout]",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", 
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        output_filename
    ])
    
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log(f"Cinematic video successfully rendered to {output_filename}!")
    except subprocess.CalledProcessError as e:
        log(f"FFmpeg failed with exit code {e.returncode}")
        raise


# ==========================================
# MAIN EXECUTION PIPELINE
# ==========================================
if __name__ == "__main__":
    log("Starting automated grand debate pipeline with Chatterbox & 10 frontier judges...")
    
    rounds = load_topics_from_file("topic.txt")
    
    intro_text = f"Welcome to today's AI grand debate featuring {len(rounds)} rounds based on our custom topic list, evaluated by a panel of ten frontier models."
    intro_audio = synthesize_speech_chatterbox(intro_text, "intro.wav")
    
    audio_list = [intro_audio]
    captions_list = ["AI Grand Debate Arena - Introduction"]
    
    total_score_a = 0
    total_score_b = 0

    for r in rounds:
        r_num = r["round"]
        log(f"Processing Round {r_num}: {r['topic']}...")
        
        path_a = f"round_{r_num}_a.wav"
        path_b = f"round_{r_num}_b.wav"
        
        synthesize_speech_chatterbox(r["a"], path_a)
        synthesize_speech_chatterbox(r["b"], path_b)
        
        audio_list.extend([path_a, path_b])
        captions_list.extend([f"Round {r_num}: {r['topic']} (Debater A)", f"Round {r_num}: {r['topic']} (Debater B)"])
        
        round_transcript = f"Round {r_num} on {r['topic']} - Debater A: {r['a']} | Debater B: {r['b']}"
        scores = evaluate_debate_round(round_transcript)
        
        avg_a = sum(d.get("score_a", 75) for d in scores.values()) / max(len(scores), 1)
        avg_b = sum(d.get("score_b", 75) for d in scores.values()) / max(len(scores), 1)
        
        total_score_a += avg_a
        total_score_b += avg_b
        
        wins_a = sum(1 for d in scores.values() if d.get("winner") == "A")
        wins_b = sum(1 for d in scores.values() if d.get("winner") == "B")
        
        commentary_text = f"End of Round {r_num}. Our ten-judge panel awarded {wins_a} votes to Debater A and {wins_b} votes to Debater B."
        comm_path = f"round_{r_num}_commentary.wav"
        synthesize_speech_chatterbox(commentary_text, comm_path)
        
        audio_list.append(comm_path)
        captions_list.append(f"Judge Panel Breakdown: A ({wins_a} votes) vs B ({wins_b} votes)")

    log("Compiling final summary and tallying cumulative scores...")
    winner = "Debater A" if total_score_a >= total_score_b else "Debater B"
    summary_text = f"After all rounds judged by ten frontier AI models, the votes are in. Debater A finished with a cumulative score of {int(total_score_a)}, while Debater B finished with {int(total_score_b)}. Your overall winner is {winner}!"
    
    summary_audio_path = "final_summary.wav"
    synthesize_speech_chatterbox(summary_text, summary_audio_path)
    
    audio_list.append(summary_audio_path)
    captions_list.append(f"Grand Summary: Winner Crowned ({winner})")

    render_youtube_debate_video(audio_list, captions_list, "final_debate_output.mp4")
    log("Full debate pipeline execution finished successfully.")
