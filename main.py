import os
import json
import asyncio
import requests
import re

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")

VOICE_A_ID = "TDLo7essLth41zzKX1tJ"
VOICE_B_ID = "ukqx2suWsNQ1vu8lj2IC"

JUDGES = [
    "openai/gpt-4o",
    "google/gemini-pro-1.5",
    "meta-llama/llama-3.1-70b-instruct"
]

def clean_json_string(text):
    """Clean markdown code fences safely"""
    text = re.sub(r"^```(json)?", "", text, flags=re.MULTILINE)
    text = re.sub(r"^```", "", text, flags=re.MULTILINE)
    return text.strip()

def generate_debate():
    if not os.path.exists("topic.txt"):
        raise Exception("topic.txt file not found!")

    with open("topic.txt", "r") as f:
        topic = f.read().strip()

    print(f"Generating Debate for Topic: {topic}")
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }
    prompt = (
        f"Write a 4-round formal debate on '{topic}'. "
        "Return ONLY a valid JSON list without markdown formatting: "
        '[{"round": 1, "speaker": "A", "text": "..."}, {"round": 1, "speaker": "B", "text": "..."}]'
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
            }, timeout=30)
            
            data = res.json()
            if "choices" in data and len(data["choices"]) > 0:
                raw_content = data['choices'][0]['message']['content']
                cleaned_content = clean_json_string(raw_content)
                return json.loads(cleaned_content)
            else:
                last_error = data.get("error", "Unknown API error")
        except Exception as e:
            last_error = str(e)
            
    raise Exception(f"All OpenRouter attempts failed. Last error: {last_error}")

async def query_judge(model, arg_a, arg_b):
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }
    prompt = f"Debate A: {arg_a}\nDebate B: {arg_b}\nWhich debater was more logically sound? Output ONLY 'A' or 'B'."
    try:
        res = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}]
        }, timeout=15)
        
        data = res.json()
        if "choices" in data and len(data["choices"]) > 0:
            ans = data['choices'][0]['message']['content'].strip()
            return "A" if "A" in ans else "B"
        return "A"
    except:
        return "A"

async def run_judges(script):
    print("Querying AI Judges...")
    results = {}
    for r in range(1, 5):
        arg_a = next((i['text'] for i in script if i['round'] == r and i['speaker'] == 'A'), "")
        arg_b = next((i['text'] for i in script if i['round'] == r and i['speaker'] == 'B'), "")
        
        votes = await asyncio.gather(*[query_judge(m, arg_a, arg_b) for m in JUDGES])
        score_a = votes.count("A")
        score_b = votes.count("B")
        results[f"Round_{r}"] = {"Score_A": score_a, "Score_B": score_b}
        print(f"Round {r} Results - Debater A: {score_a} | Debater B: {score_b}")
    return results

def make_audio(script):
    print("Synthesizing Audio via ElevenLabs...")
    audio = bytearray()
    for line in script:
        vid = VOICE_A_ID if line['speaker'] == 'A' else VOICE_B_ID
        res = requests.post(f"https://api.elevenlabs.io/v1/text-to-speech/{vid}", 
                            headers={"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"}, 
                            json={"text": line['text']})
        
        if res.status_code == 200:
            audio.extend(res.content)
        else:
            print(f"ElevenLabs Error ({res.status_code}): {res.text}")
    
    with open("output_audio.mp3", "wb") as f:
        f.write(audio)

if __name__ == "__main__":
    script = generate_debate()
    judges_data = asyncio.run(run_judges(script))
    
    with open("scores.json", "w") as f:
        json.dump(judges_data, f, indent=2)
        
    make_audio(script)
    print("Execution complete! output_audio.mp3 and scores.json generated successfully.")
