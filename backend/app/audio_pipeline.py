import os
import json
import httpx
import tempfile
import base64
from typing import List, Dict, Any
from .config import settings

GROQ_AUDIO_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_TTS_URL = "https://api.groq.com/openai/v1/audio/speech"

async def transcribe_audio_groq(audio_bytes: bytes, filename="audio.webm") -> str:
    """Send audio bytes to Groq Whisper for transcription."""
    headers = {"Authorization": f"Bearer {settings.groq_api_key}"}
    
    # We must send as multipart/form-data
    files = {
        "file": (filename, audio_bytes, "audio/webm"),
    }
    data = {
        "model": settings.model,
        "response_format": "json",
        "language": "en",  # Force English transcription; non-English speech will be translated
    }
    
    # Full candidate turns (VAD-recorded, can run 30-90s of audio) take Groq
    # a while to transcribe; 30s was cutting off in-flight requests (client
    # disconnect => empty-message TimeoutException, Groq logs it as HTTP 499).
    async with httpx.AsyncClient(timeout=90) as client:
        r = await client.post(GROQ_AUDIO_URL, headers=headers, data=data, files=files)
        if not r.is_success:
            raise ValueError(f"Groq STT error {r.status_code}: {r.text}")
        return r.json().get("text", "")

async def generate_chat_response_groq(messages: List[Dict[str, str]]) -> str:
    """Get the interviewer's response from Groq LLM."""
    headers = {
        "Authorization": f"Bearer {settings.groq_api_key}",
        "Content-Type": "application/json"
    }
    body = {
        "model": settings.groq_llm_model,
        "messages": messages,
        "temperature": 0.7,
        # gpt-oss is a reasoning model: its hidden chain-of-thought is still
        # billed against max_tokens even with reasoning_format=hidden, so 150
        # (and even 400) was getting fully consumed by reasoning before any
        # answer text was written, coming back as empty content every turn.
        "max_tokens": 1200,
        "reasoning_effort": "low",
        "reasoning_format": "hidden",  # keep chain-of-thought out of spoken content
    }

    async with httpx.AsyncClient(timeout=60) as client:  # gpt-oss reasons before answering; 30s was too tight
        r = await client.post(GROQ_CHAT_URL, headers=headers, json=body)
        if not r.is_success:
            raise ValueError(f"Groq LLM error {r.status_code}: {r.text}")
        data = r.json()
        choice = data["choices"][0]
        content = choice["message"]["content"] or ""
        if not content.strip():
            # Diagnostic: if this fires, reasoning is still eating the whole
            # budget (finish_reason will read "length") and max_tokens needs
            # to go up further, or reasoning_effort needs to drop more.
            print(f"[llm] empty content from {settings.groq_llm_model}, "
                  f"finish_reason={choice.get('finish_reason')}, usage={data.get('usage')}")
        return content

def _gtts_synth(text: str) -> bytes:
    from gtts import gTTS
    import io

    tts = gTTS(text=text, lang='en', tld='us')  # US English accent
    fp = io.BytesIO()
    tts.write_to_fp(fp)
    return fp.getvalue()


async def synthesize_speech_groq(text: str) -> bytes:
    """Generate audio from text using gTTS (unlimited, no API key).

    gTTS makes a blocking HTTP call to Google; run it in a thread so one
    user's TTS doesn't stall the event loop for every other connected user.
    """
    import asyncio
    return await asyncio.to_thread(_gtts_synth, text)
