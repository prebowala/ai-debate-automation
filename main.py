import os
import json
import asyncio
import requests
import re

# Fetch API keys from GitHub Secrets
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")

# Replace with your actual ElevenLabs Voice IDs
VOICE_A_ID = "TDLo7essLth41zzKX1tJ"  # Example Voice ID for Debater A
VOICE_B_ID = "ukqx2suWsNQ1vu8lj2IC"  # Example Voice ID for Debater B

# List of models for OpenRouter AI Judges
JUDGES = [
    "openai/gpt-4o",
    "google/gemini-pro-1.5",
    "meta-llama/llama-3.1-70b-instruct",
    "mistralai/mistral-large",
    "cohere/command-r-plus"
]

def clean_json_string(text):
    """Removes markdown wrappers like ```json ... ``` from model responses"""
    text = re.sub(r'```json\s*', '', text)
    text = re.sub(r'
