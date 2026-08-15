import os
import json
import asyncio
import requests

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")

VOICE_A_ID = "TDLo7essLth41zzKX1tJ"
VOICE_B_ID = "ukqx2suWsNQ1vu8lj2IC"

# 15 AI Judge Endpoints via OpenRouter
JUDGES = [
    "openai/gpt-4o", "anthropic/claude-3.5-sonnet", "x-ai/grok-2",
    "google/gemini-pro-1.5", "meta-llama/llama-3.1-70b-instruct",
    "mistralai/mistral-large", "cohere/command-r-plus"
]

def generate_debate():
    with open("topic.txt", "r") as f:
        topic = f.read().strip()

    print(f"Generating Debate for Topic: {topic}")
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
    prompt = f"Write a 4-round formal debate on '{topic}'. Return JSON list: [{{\"round\": 1, \"speaker\": \"A\", \"text\": \"...\"}}]"
    
    res = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json={
        "model": "anthropic/claude-3.5-sonnet", "messages": [{"role": "user", "content": prompt}]
    })
    return json.loads(res.json()['choices'][0]['message']['content'])

async def query_judge(model, arg_a, arg_b):
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
    prompt = f"Debate A: {arg_a}\nDebate B: {arg_b}\nWho wins logic-wise? Output ONLY 'A' or 'B'."
    try:
        res = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json={
            "model": model, "messages": [{"role": "user", "content": prompt}]
        }, timeout=10)
        return "A" if "A" in res.json()['choices'][0]['message']['content'] else "B"
    except:
        return "A"

async def run_judges(script):
    results = {}
    for r in range(1, 5):
        arg_a = next(i['text'] for i in script if i['round'] == r and i['speaker'] == 'A')
        arg_b = next(i['text'] for i in script if i['round'] == r and i['speaker'] == 'B')
        
        votes = await asyncio.gather(*[query_judge(m, arg_a, arg_b) for m in JUDGES])
        results[f"Round_{r}"] = {"Score_A": votes.count("A"), "Score_B": votes.count("B")}
    return results

def make_audio(script):
    audio = bytearray()
    for line in script:
        vid = VOICE_A_ID if line['speaker'] == 'A' else VOICE_B_ID
        res = requests.post(f"https://api.elevenlabs.io/v1/text-to-speech/{vid}", 
                            headers={"xi-api-key": ELEVENLABS_API_KEY}, 
                            json={"text": line['text']})
        audio.extend(res.content)
    with open("output_audio.mp3", "wb") as f:
        f.write(audio)

if __name__ == "__main__":
    script = generate_debate()
    judges_data = asyncio.run(run_judges(script))
    
    with open("scores.json", "w") as f:
        json.dump(judges_data, f, indent=2)
    make_audio(script)
