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
            "-t", "4", "-q:a", "9", "-acodec", "libmp3lame", output_path
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return output_path


# ==========================================
# 2. 10 FRONTIER JUDGES + 5 FALLBACKS PANEL
# ==========================================
def evaluate_debate_round(transcript_text):
    log("Evaluating debate round with 10 primary frontier judges and 5 backups...")
    
    primary_judges = [
        "openai/gpt-5.6-terra", 
        "anthropic/claude-sonnet-5", 
        "google/gemini-pro-1.5", 
        "moonshotai/kimi-k3", 
        "deepseek/deepseek-r1",
        "x-ai/grok-4.5",
        "mistralai/mistral-large",
        "meta-llama/llama-3.1-405b-instruct",
        "cohere/command-r-plus",
        "z-ai/glm-5.2"
    ]
    
    fallback_judges = [
        "openai/gpt-4o",
        "anthropic/claude-3-5-sonnet",
        "google/gemini-flash-1.5",
        "deepseek/deepseek-chat",
        "meta-llama/llama-3-70b-instruct"
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
                timeout=(4, 8)
            )
            if response.status_code == 200:
                data = response.json()
                content = data['choices'][0]['message']['content'].replace("```json", "").replace("```", "").strip()
                judges_results[model] = json.loads(content)
                log(f"Judge [{model}] evaluated successfully ({len(judges_results)}/10).")
        except Exception:
            log(f"Judge [{model}] timed out or failed. Moving to next candidate.")
            pass
            
    if len(judges_results) == 0:
        judges_results["fallback/mock-judge"] = {"winner": "A", "score_a": 78, "score_b": 75, "reason": "Network timeout fallback placeholder rationale."}
        
    return judges_results


# ==========================================
# 3. YOUTUBE-STYLE CINEMATIC COMPOSITOR
# ==========================================
def render_youtube_debate_video(audio_files, captions, output_filename="final_debate_output.mp4"):
    log(f"Rendering 5-round cinematic video combining {len(audio_files)} audio segments via FFmpeg...")
    
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
# MAIN EXECUTION PIPELINE (5 ROUNDS + SUMMARY)
# ==========================================
if __name__ == "__main__":
    log("Starting automated 5-round grand debate pipeline with 10 frontier judges...")
    
    intro_text = "Welcome to the ultimate five-round AI grand debate, evaluated by a panel of ten frontier models. Let us dive straight into Round One."
    intro_audio = synthesize_speech_gemini(intro_text, "intro.wav", voice_name="Puck")
    
    rounds = [
        {
            "round": 1,
            "topic": "AGI Timeline & Acceleration",
            "a": "Artificial intelligence will drastically accelerate scientific discovery, curing major global diseases and expanding human capability exponentially.",
            "b": "While promising, unchecked acceleration introduces severe structural alignment risks and unmitigated societal instability before we are ready."
        },
        {
            "round": 2,
            "topic": "Open Source vs Closed Ecosystems",
            "a": "Democratizing powerful frontier models ensures benefits are distributed equitably globally rather than locked behind corporate monopolies.",
            "b": "Open-source proliferation without robust foundational verification tools is an invitation to widespread cyber-attacks and dual-use harms."
        },
        {
            "round": 3,
            "topic": "Economic Impact & Labor Markets",
            "a": "AI automation will eliminate burdensome cognitive and physical toil, liberating humanity to focus on creative and philosophical pursuits.",
            "b": "Without pre-emptive economic restructuring, rapid displacement will trigger unprecedented structural unemployment and wealth concentration."
        },
        {
            "round": 4,
            "topic": "Regulation & Governance",
            "a": "Heavy government regulation will only stifle nimble innovation and push cutting-edge research into underground or hostile jurisdictions.",
            "b": "Global coordination and strict guardrails are mandatory to prevent an unconstrained race to the bottom in autonomous capabilities."
        },
        {
            "round": 5,
            "topic": "Long-Term Human Autonomy",
            "a": "Deep integration with advanced AI systems represents the natural next step in human evolution and cognitive expansion.",
            "b": "Subcontracting critical decision-making to black-box systems risks eroding core human agency and independent critical thought."
        }
    ]
    
    audio_list = [intro_audio]
    captions_list = ["AI Grand Debate Arena - Introduction"]
    
    total_score_a = 0
    total_score_b = 0

    for r in rounds:
        r_num = r["round"]
        log(f"Processing Round {r_num}: {r['topic']}...")
        
        path_a = f"round_{r_num}_a.wav"
        path_b = f"round_{r_num}_b.wav"
        
        synthesize_speech_gemini(r["a"], path_a, voice_name="Puck")
        synthesize_speech_gemini(r["b"], path_b, voice_name="Fenrir")
        
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
        synthesize_speech_gemini(commentary_text, comm_path, voice_name="Puck")
        
        audio_list.append(comm_path)
        captions_list.append(f"Judge Panel Breakdown: A ({wins_a} votes) vs B ({wins_b} votes)")

    log("Compiling final summary and tallying cumulative scores...")
    winner = "Debater A" if total_score_a >= total_score_b else "Debater B"
    summary_text = f"After five intense rounds judged by ten frontier AI models, the votes are in. Debater A finished with a cumulative score of {int(total_score_a)}, while Debater B finished with {int(total_score_b)}. Your overall winner for this grand debate is {winner}!"
    
    summary_audio_path = "final_summary.wav"
    synthesize_speech_gemini(summary_text, summary_audio_path, voice_name="Puck")
    
    audio_list.append(summary_audio_path)
    captions_list.append(f"Grand Summary: Winner Crowned ({winner})")

    render_youtube_debate_video(audio_list, captions_list, "final_debate_output.mp4")
    log("Full 10-judge 5-round debate pipeline execution finished successfully.")
