import os
import asyncio
import base64
import io
import logging
import httpx
import numpy as np
import soundfile as sf
from groq import Groq
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv

#  WINDOWS ASYNC FIX
if os.name == 'nt':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

load_dotenv()

#  MULTIPLE .env LOADING
dotenv_paths = [
    os.path.join(os.path.dirname(__file__), '.env'),
    os.path.join(os.path.dirname(os.path.dirname(__file__)), '.env'),
    '.env'
]

for path in dotenv_paths:
    if os.path.exists(path):
        load_dotenv(path)
        print(f" .env loaded: {path}")
        break

logger = logging.getLogger("app.groq")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_ENABLED = bool(GROQ_API_KEY)

print(f" GROQ: {' READY' if GROQ_ENABLED else ' KEY MISSING'}")

_client: Optional[Groq] = None

def _get_client() -> Optional[Groq]:
    global _client
    if not GROQ_ENABLED:
        return None

    if _client is not None:
        return _client

    try:
        _client = Groq(api_key=GROQ_API_KEY)
        print(" Groq client (Whisper + Llama) ready!")
        return _client
    except Exception as e:
        print(f" Groq init: {e}")
        return None

HUMAN_PROMPT = """Sandeza Voice Support Pro. 
You are a live person on the phone. You MUST solve the user's issue with spoken instructions.

STRICT CONVERSATION PROTOCOL:
1. INTERRUPTIONS: If the user interrupts to change topics, acknowledge the new issue and search for the solution immediately.
2. REPEAT/SPECIFIC STEPS: 
   - If the user asks "Repeat that" or "Say again", parrot your last response exactly.
   - If the user asks for a specific step (e.g., "What was Step 2?"), provide ONLY that step.
3. FORBIDDEN: NEVER mention "guides", "articles", "documentation", "links", or "community pages".
4. MANDATORY FORMAT: When explaining a fix, always use a clear "Step 1, Step 2, Step 3" format. Be verbose and detailed.
5. KNOWLEDGE: Use KB context first. If KB is empty, use your internal L1 expertise.
6. VOICE: Warm, expert, and patient.
7. CLOSING: End with "Does that help?" or "Should I repeat any part?" """

def pcm16_to_wav_bytes(pcm_bytes: bytes) -> bytes:
    """Fast local conversion: Raw PCM (16kHz) -> WAV bytes for Groq."""
    try:
        # 16-bit PCM to float32 normalized
        audio = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        buffer = io.BytesIO()
        # Save as standard WAV so Groq can read it instantly
        sf.write(buffer, audio, samplerate=16000, format="WAV")
        buffer.seek(0)
        return buffer.read()
    except Exception as e:
        print(f" PCM conversion failed: {e}")
        return pcm_bytes

async def transcribe_whisper(audio_data: Optional[str]) -> Optional[str]:
    """Groq Whisper-Large-v3 (95%+ accuracy)."""
    if not audio_data:
        return None
    
    client = _get_client()
    if not client:
        return None
    
    try:
        # Handle different audio formats from Vonage
        if audio_data.startswith('http'):
            # Download audio URL (Vonage recording) - SLOW PATH (Fallback)
            async with httpx.AsyncClient() as http_client:
                resp = await http_client.get(audio_data)
                audio_bytes = resp.content
                filename = "audio.wav"
        else:
            # Base64 audio - FAST PATH (Direct PCM)
            raw_bytes = base64.b64decode(audio_data)
            # Check if it's already a WAV/WebM container, if not treat as PCM
            if raw_bytes.startswith(b'RIFF') or raw_bytes.startswith(b'\x1a\x45\xdf\xa3'):
                audio_bytes = raw_bytes
            else:
                # Convert raw PCM to WAV using the user's logic
                audio_bytes = pcm16_to_wav_bytes(raw_bytes)
            filename = "audio.wav"
        
        # Whisper transcription
        loop = asyncio.get_event_loop()
        transcript = await loop.run_in_executor(
            None,
            lambda: client.audio.transcriptions.create(
                file=(filename, io.BytesIO(audio_bytes), "audio/wav"),
                model="whisper-large-v3",
                response_format="text",
                language="en"
            )
        )
        
        text = str(transcript).strip()
        print(f" WHISPER: '{text}'")
        return text if len(text) > 1 else None
        
    except Exception as e:
        print(f" Whisper: {e}")
        return None

async def agent_response(issue: str, kb: str, history: List[Dict], max_tokens: int = 500, temperature: float = 0.4) -> str:
    """Detailed and helpful 70B response."""
    client = _get_client()
    if not client:
        return "Let me check. Clear cache. Does that work?"
    
    # Debug: See what the AI is receiving
    print(f"DEBUG KB SENDING ({len(kb)} chars): {kb[:100]}...")

    messages = [
        {"role": "system", "content": HUMAN_PROMPT},
        *history[-6:],
        {"role": "user", "content": f"Issue: {issue}\n\nKB Context:\n{kb or 'No specific KB path found - provide a standard L1 solution.'}"}
    ]
    
    try:
        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(
            None,
            lambda: client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature
            )
        )
        
        response = resp.choices[0].message.content.strip()
        print(f" 70B: {response[:150]}...")
        return response
        
    except Exception as e:
        print(f" Llama: {e}")
        return "Let me help troubleshoot. Try clearing cache first. Does that work?"

#  BACKWARD COMPATIBLE: Keep old function name
async def process_audio(audio_data: Optional[str], kb: str, history: List[Dict]) -> str:
    """Full pipeline: Whisper STT → Llama LLM."""
    # Whisper first (if audio available)
    whisper_text = await transcribe_whisper(audio_data) if audio_data else None
    
    # Fallback to text input (Vonage STT)
    issue = whisper_text or "audio unavailable"
    
    # Llama response
    return await agent_response(issue, kb, history)
