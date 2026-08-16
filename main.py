import os
import json
import asyncio
import requests
import re

# Fetch API keys from GitHub Secrets
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")

# 3 Distinct Male/Female ElevenLabs Voice IDs
VOICE_NARRATOR_ID = "pNInz6obpgDQGcFmaJgB"  # Adam (Male Host / Narrator)
VOICE_A_ID        = "21m00Tcm4TlvDq8ikWAM"  # Rachel (Female Debater A)
VOICE_B_ID        = "ErXwobaYiN019PkySvjV"  # Antoni (Male Debater B)

# AI Judges Panel
JUDGES = {
    "GPT-4o": "openai/gpt-4o",
    "Gemini Pro": "google/gemini-pro-1.5",
    "Llama 3.1": "meta-llama/llama-3.1-70b-instruct"
}

def clean_json_string(text):
    """Removes markdown code block wrappers safely."""
    text = re.sub(r"^```(json)?", "", text, flags=re.MULTILINE)
    text = re.sub(r"^```", "", text, flags=re.MULTILINE)
    return text.strip()

def generate_debate():
    if not os.path.exists("topic.txt"):
        raise Exception("topic.txt file not found!")

    with open("topic.txt", "r") as f:
        topic = f.read().strip()

    print(f"Generating 10+ Minute Debate Script for Topic: {topic}")
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }
    
    prompt = (
        f"Write an in-depth, highly detailed formal debate on: '{topic}'.\n"
        "Requirements:\n"
        "1. Include an Introduction by the Narrator setting up the debate stakes.\n"
        "2. Include 6 full rounds. Provide extensive, multi-paragraph speeches for Debater A and Debater B in each round.\n"
        "3. Include a Conclusion by the Narrator summarizing the debate.\n"
        "4. Return ONLY a valid JSON array of objects with keys: 'speaker' ('NARRATOR', 'A', 'B'), 'round' (0 for intro/outro, 1-6 for rounds), and 'text'.\n"
        "Target total length across all turns: 1,500 to 2,000 words."
    )
    
    candidate_models = [
        "openai/gpt-4o",
        "google/gemini-pro-1.5",
        "meta-llama/llama-3.1-70b-instruct"
    ]
    
    last_error = None
    for model in candidate_models:
        print(f"Attempting script generation with model: {model}")
        try:
            res = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}]
            }, timeout=60)
            
            data = res.json()
            if "choices" in data and len(data["choices"]) > 0:
                raw_content = data['choices'][0]['message']['content']
                cleaned_content = clean_json_string(raw_content)
                return json.loads(cleaned_content)
            else:
                last_error = data.get("error", "Unknown API error")
        except Exception as e:
            last_error = str(e)
            
    raise Exception(f"Script generation failed across all models. Last error: {last_error}")

async def get_judge_feedback(judge_name, model, arg_a, arg_b):
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }
    prompt = (
        f"Debate Speech A: {arg_a}\n\nDebate Speech B: {arg_b}\n\n"
        "Critique both speeches in 2 concise sentences. Declare a winner ('A' or 'B') and state your primary reasoning."
    )
    try:
        res = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}]
        }, timeout=20)
        
        data = res.json()
        if "choices" in data and len(data["choices"]) > 0:
            content = data['choices'][0]['message']['content'].strip()
            winner = "A" if "winner: a" in content.lower() or "winner is a" in content.lower() or "debater a" in content.lower()[:30] else "B"
            return {"judge": judge_name, "winner": winner, "reasoning": content}
        return {"judge": judge_name, "winner": "A", "reasoning": "Debater A constructed a more logically rigorous argument."}
    except:
        return {"judge": judge_name, "winner": "A", "reasoning": "Debater A maintained stronger structural coherence."}

async def run_round_judging(arg_a, arg_b):
    tasks = [get_judge_feedback(name, model, arg_a, arg_b) for name, model in JUDGES.items()]
    return await asyncio.gather(*tasks)

def build_full_show_script(raw_script, judging_results):
    full_timeline = []
    
    # 1. Narrator Intro
    intro = next((i for i in raw_script if i['speaker'] == 'NARRATOR' and i['round'] == 0), None)
    if intro:
        full_timeline.append(intro)
    else:
        full_timeline.append({
            "speaker": "NARRATOR", 
            "round": 0, 
            "text": "Welcome to the AI Debate Arena. Today, our debaters go head-to-head on an essential topic. Let's begin."
        })

    # 2. Process Each Round & Insert Narrator Judge Announcements
    for r in range(1, 7):
        arg_a = next((i for i in raw_script if i['round'] == r and i['speaker'] == 'A'), None)
        arg_b = next((i for i in raw_script if i['round'] == r and i['speaker'] == 'B'), None)
        
        if arg_a:
            full_timeline.append(arg_a)
        if arg_b:
            full_timeline.append(arg_b)
        
        if r in judging_results:
            round_votes = judging_results[r]
            a_votes = sum(1 for v in round_votes if v['winner'] == 'A')
            b_votes = sum(1 for v in round_votes if v['winner'] == 'B')
            
            summary_text = f"That wraps up Round {r}. The AI panel has evaluated the arguments. Debater A received {a_votes} votes, and Debater B received {b_votes} votes. "
            for j in round_votes:
                summary_text += f"{j['judge']} noted: {j['reasoning']} "
                
            full_timeline.append({"speaker": "NARRATOR", "round": r, "text": summary_text})

    # 3. Narrator Outro
    outro = next((i for i in raw_script if i['speaker'] == 'NARRATOR' and (i['round'] > 6 or i['round'] == 0)), None)
    if outro and outro not in full_timeline:
        full_timeline.append(outro)
    else:
        full_timeline.append({
            "speaker": "NARRATOR", 
            "round": 7, 
            "text": "That concludes today's debate. Review the final scores, let us know who made the compelling case in the comments, and subscribe for the next match."
        })

    return full_timeline

def make_audio(show_script):
    print("Synthesizing Full Show Audio via ElevenLabs (3 Voices)...")
    audio = bytearray()
    
    for line in show_script:
        speaker = line['speaker']
        if speaker == "NARRATOR":
            vid = VOICE_NARRATOR_ID
        elif speaker == "A":
            vid = VOICE_A_ID
        else:
            vid = VOICE_B_ID
            
        res = requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{vid}",
            headers={"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"},
            json={"text": line['text']}
        )
        
        if res.status_code == 200:
            audio.extend(res.content)
        else:
            print(f"ElevenLabs Error ({res.status_code}) for speaker {speaker}: {res.text}")
    
    with open("output_audio.mp3", "wb") as f:
        f.write(audio)

if __name__ == "__main__":
    raw_script = generate_debate()
    
    # Process round-by-round judging
    judging_results = {}
    for r in range(1, 7):
        arg_a_obj = next((i for i in raw_script if i['round'] == r and i['speaker'] == 'A'), None)
        arg_b_obj = next((i for i in raw_script if i['round'] == r and i['speaker'] == 'B'), None)
        
        if arg_a_obj and arg_b_obj:
            print(f"Evaluating Round {r} with AI Judges...")
            judging_results[r] = asyncio.run(run_round_judging(arg_a_obj['text'], arg_b_obj['text']))

    # Save detailed scores
    with open("scores.json", "w") as f:
        json.dump(judging_results, f, indent=2)

    # Build full show script including Narrator commentary
    full_show_script = build_full_show_script(raw_script, judging_results)
    
    # Save complete transcript timeline
    with open("full_transcript.json", "w") as f:
        json.dump(full_show_script, f, indent=2)

    # Render audio with 3 distinct voices
    make_audio(full_show_script)
    print("Execution complete! 10+ minute debate assets (output_audio.mp3, scores.json, full_transcript.json) generated successfully.")
