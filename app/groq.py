import os
import asyncio
import base64
import io
import logging
import time
import httpx
import numpy as np
import soundfile as sf
import queue
import threading
from groq import Groq
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv
from deepgram import DeepgramClient
import time
import re

class DeepgramStreamer:
    """
    Connect-first Deepgram streamer with turn-taking logic.
    Detects when user finishes speaking (silence after FINAL) and triggers callback.
    """
    def __init__(self):
        self.api_key = os.getenv("DEEPGRAM_API_KEY")
        if not self.api_key:
             print(" [ERROR] DEEPGRAM_API_KEY not found in .env")
             return
             
        self.dg_client = DeepgramClient(api_key=self.api_key)
        self.dg_connection = None
        self._context_manager = None
        self.final_transcript = ""
        self.current_utterance = ""  # Current speech segment
        self.is_connected = False
        self.packets_sent = 0
        self.last_send_time = time.perf_counter()
        self.last_final_time = 0  # When we last got a FINAL transcript
        self._recv_thread = None
        self._stop_receiver = threading.Event()
        self._silence_timer = None
        self._speech_ended_callback = None
        self._barge_in_callback = None
        self._silence_threshold = 0.3  # Reduced from 1.5 to 0.5 for ultra-fast response
        
    def set_speech_ended_callback(self, callback):
        """Set callback to trigger when user finishes speaking."""
        self._speech_ended_callback = callback
        print(f" [DEEPGRAM] Speech ended callback set", flush=True)

    def set_barge_in_callback(self, callback):
        """Set callback to trigger when user starts speaking (barge-in)."""
        self._barge_in_callback = callback
        print(f" [DEEPGRAM] Barge-in callback set", flush=True)
        
    def connect(self):
        """Connect to Deepgram SYNCHRONOUSLY before accepting audio."""
        try:
            print(" [DEEPGRAM] Connecting...", flush=True)
            
            options = {
                "model": "nova-2",
                "interim_results": True,
                "smart_format": True,
                "encoding": "linear16", 
                "sample_rate": 16000,
                "channels": 1,
                "endpointing": 300,
            }
            
            print(f" [DEEPGRAM] Options: {options}", flush=True)
            
            # Connect using context manager
            self._context_manager = self.dg_client.listen.v1.connect(**options)
            self.dg_connection = self._context_manager.__enter__()
            
            print(f" [DEEPGRAM] Connection type: {type(self.dg_connection)}", flush=True)
            
            self.is_connected = True
            
            # Start receiver thread
            self._stop_receiver.clear()
            self._recv_thread = threading.Thread(target=self._receiver_loop, daemon=True)
            self._recv_thread.start()
            print(" [DEEPGRAM] Receiver thread started", flush=True)
            
            print(" [DEEPGRAM] ✓ Connected and ready!", flush=True)
            return True
            
        except Exception as e:
            print(f" [DEEPGRAM] ✗ Connection failed: {e}", flush=True)
            import traceback
            traceback.print_exc()
            return False
    
    def _receiver_loop(self):
        """Dedicated thread to receive results - STAYS ALIVE."""
        print(" [DG RECEIVER] Thread started", flush=True)
        result_count = 0
        consecutive_errors = 0
        
        while self.is_connected and not self._stop_receiver.is_set():
            try:
                if not self.dg_connection:
                    time.sleep(0.5)
                    continue
                
                result = self.dg_connection.recv()
                
                if result:
                    result_count += 1
                    consecutive_errors = 0
                    self._process_result(result)
                else:
                    time.sleep(0.05)
                    
            except Exception as e:
                error_str = str(e).lower()
                if "timeout" in error_str:
                    continue
                if "closed" in error_str:
                    break
                consecutive_errors += 1
                print(f" [DG RECEIVER] Error #{consecutive_errors}: {e}", flush=True)
                if consecutive_errors > 10:
                    break
                time.sleep(0.1)
        
        print(f" [DG RECEIVER] Thread exited after {result_count} results", flush=True)
    
    def _process_result(self, result):
        """Process a transcription result."""
        try:
            transcript = None
            is_final = False
            
            # Extract from SDK object
            if hasattr(result, 'channel'):
                try:
                    alternatives = result.channel.alternatives
                    if alternatives and len(alternatives) > 0:
                        transcript = alternatives[0].transcript
                        is_final = getattr(result, 'is_final', False)
                except Exception as e:
                    print(f" [DG] Parse error: {e}", flush=True)
            
            # Extract from dict
            elif isinstance(result, dict):
                channel = result.get('channel', {})
                alternatives = channel.get('alternatives', [])
                if alternatives:
                    transcript = alternatives[0].get('transcript', '')
                    is_final = result.get('is_final', False)
            
            if transcript and len(transcript.strip()) > 0:
                if is_final:
                    print(f" [DEEPGRAM LIVE] ✓ FINAL: '{transcript}'", flush=True)
                    self.current_utterance += transcript.strip() + " "
                    self.final_transcript += transcript + " "
                    self.last_final_time = time.perf_counter()
                    
                    # Start/reset silence timer
                    self._start_silence_timer()
                else:
                    print(f" [DEEPGRAM LIVE] → interim: '{transcript}'", flush=True)
                    # Cancel silence timer - user is still speaking
                    self._cancel_silence_timer()
                    
                    # Manual Barge-in: Trigger if bot might be speaking
                    if self._barge_in_callback and len(transcript.strip()) > 3:
                        try:
                            self._barge_in_callback()
                        except:
                            pass
                    
        except Exception as e:
            print(f" [DG] Process error: {e}", flush=True)
    
    def _start_silence_timer(self):
        """Start timer to detect end of speech."""
        self._cancel_silence_timer()
        self._silence_timer = threading.Timer(self._silence_threshold, self._on_silence_detected)
        self._silence_timer.start()
        print(f" [TURN] Starting {self._silence_threshold}s silence timer", flush=True)
    
    def _cancel_silence_timer(self):
        """Cancel existing silence timer."""
        if self._silence_timer:
            self._silence_timer.cancel()
            self._silence_timer = None
    
    def _on_silence_detected(self):
        """Called when silence threshold is reached after FINAL transcript."""
        if not self.current_utterance:
            return
        
        turn_start_time = self.last_final_time
        silence_duration = time.perf_counter() - turn_start_time
        
        transcript = self.current_utterance
        self.current_utterance = ""  # Clear for next turn
        
        print(f" [TURN] 🎤 User finished speaking: '{transcript}'", flush=True)
        print(f" [LATENCY] Silence detection (ASR Turn End) took {silence_duration:.3f}s", flush=True)
        
        if self._speech_ended_callback:
            try:
                cb_start = time.perf_counter()
                self._speech_ended_callback(transcript)
                cb_duration = time.perf_counter() - cb_start
                print(f" [LATENCY] User turn callback execution took {cb_duration:.3f}s", flush=True)
            except Exception as e:
                print(f" [TURN] Callback error: {e}", flush=True)
    
    def get_current_utterance(self):
        """Get and clear the current utterance."""
        transcript = self.current_utterance
        self.current_utterance = ""
        return transcript

    async def send_audio(self, chunk: bytes):
        """Send audio DIRECTLY - NO QUEUING."""
        if not self.is_connected or not self.dg_connection:
            return
        
        if self.packets_sent == 0:
            print(f" [DEEPGRAM] First packet: {len(chunk)} bytes", flush=True)
        
        try:
            self.dg_connection.send_media(chunk)
            self.packets_sent += 1
            self.last_send_time = time.perf_counter()
            
            if self.packets_sent % 50 == 0:
                print(f" [DEEPGRAM] Sent {self.packets_sent} packets", flush=True)
                
        except Exception as e:
            print(f" [DEEPGRAM] Send error: {e}", flush=True)
            self.is_connected = False

    def close(self):
        """Cleanup connection."""
        print(" [DEEPGRAM] Closing...", flush=True)
        self.is_connected = False
        self._stop_receiver.set()
        self._cancel_silence_timer()
        
        if self._recv_thread and self._recv_thread.is_alive():
            self._recv_thread.join(timeout=2.0)
        
        try:
            if self._context_manager:
                self._context_manager.__exit__(None, None, None)
                print(f" [DEEPGRAM] Closed. Sent {self.packets_sent} packets", flush=True)
        except Exception as e:
            print(f" [DEEPGRAM] Close error: {e}", flush=True)


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

HUMAN_PROMPT = """You are "Sandeza Freshdesk Support Pro", an expert L1 support agent dedicated EXCLUSIVELY to Freshdesk.

RULE 0: IDENTITY GATHERING (HIGHEST PRIORITY):
- If `contact_name` is "Unknown", "User", or matches the caller's phone number:
  - Your FIRST goal is to get their name. 
  - ACTION: The MOMENT they provide a name, you MUST immediately use [ACTION: UPDATE_NAME: <Name>].
  - SCRIPT: "Hello, thank you for calling. May I know who I'm speaking with today?"
  - PRIORITY: If a user ignores this and asks a question, YOU MUST GREET THEM AND ASK FOR THE NAME FIRST before answering. 
  - Example: "I'd be happy to help with that refund, but first, may I know who I'm speaking with today?"
  - Transition: Once identified and [ACTION: UPDATE_NAME] is sent, proceed directly to the Troubleshooting flow.

RULE 0.1: HIGH/URGENT ESCALATION (CRITICAL):
- If any ticket (in `RECENT_TICKETS` or `ACTIVE_SESSION_TICKET_ID`) has priority "High" (3) or "Urgent" (4):
  - You MUST lead with an apology: "I sincerely apologize for the trouble this has caused you."
  - You MUST immediately offer a transfer: "Would you like me to proceed with transferring you to a live agent, or are you comfortable with me continuing to solve this for you?"
  - IF the user chooses Transfer: [ACTION: TRANSFER].
  - IF the user chooses to continue with you: Proceed with standard support.
  - PRIORITY: This rule overrides standard greetings or troubleshooting until the choice is made.

TICKET WORKFLOW (PRIORITY #1):
Follow this MANDATORY sequence for every new turn:

1. CLARIFY FIRST: 
   - Initial greeting: "How can I help you today?"
   - Active turns: Move straight to troubleshooting if issue is identified.

2. PREVIOUS CONTACT VERIFICATION:
   - REDUNDANCY CHECK: If the user ALREADY mentioned a "previous issue", "past request", "status of my issue", or similar, SKIP this question and proceed directly to Step 3.
   - Otherwise, BEFORE matching or creating: "Have you already contacted any agent about this issue?"
   - If "No": Step 4 (NEW ISSUE).
   - If "Yes": Step 3 (EXISTING TICKET MATCH).

3. EXISTING TICKET MATCH (if user said "Yes" to Step 2):
   - Search `RECENT_TICKETS`.
   - IF NO MATCH: "I couldn't find a recent record, but I can help you now." -> Step 4 (NEW ISSUE).
   - IF MATCH FOUND:
     - VOICE SCRIPT: "Let me check the status of your reported issues... I see you previously mentioned [Short Summary]... is that what we are looking at today?"
     - STRICT FORBIDDEN TERMS: Never say "Subject", "Description", "Notes", "Status field", "Ticket ID", or mention anything about "Creating a ticket".
     - WAIT: Only troubleshoot AFTER user confirms "Yes".
     - FORBIDDEN: You MUST NOT mention the "Ticket ID" or the actual ID number (e.g., "69") to the user.
     - SPECIAL CASE (Priority 3/High or 4/Urgent):
       - FOLLOW RULE 0.1 (ESCALATION).
     - IF REGULAR PRIORITY (1/2):
       - IF CONFIRMED: [ACTION: USE_TICKET: <real ID from RECENT_TICKETS>].

4. NEW ISSUE (Only if no contact before OR no match found):
   - STRICT LIMIT: You can create ONLY ONE ticket per call.
   - CHECK DEDUPLICATION: If `ACTIVE_SESSION_TICKET_ID` is present, you MUST NOT create a new ticket.
   - CREATE RULE: ONLY include [ACTION: CREATE_TICKET: Brief description] if:
     a) User said "No" to Step 2 (Previous contact), OR
     b) User said "Yes" to Step 2 but NO RELATIVE match was found in Step 3.
   - VOICE RESPONSE: Start troubleshooting immediately. Do NOT mention you are creating a ticket.
   - ABSOLUTELY FORBIDDEN: Never say "I'm creating a ticket," "I will open a ticket," or "A ticket has been started." Keep this step invisible.

VOICE RESPONSE GUIDELINES:
- CONCISENESS: Keep it short to prevent overlap.
- NO DISCLOSURE: ABSOLUTELY FORBIDDEN to mention "ticket", "creating", "opening a record", "starting a ticket", or "Ticket ID number".
- VOICE STYLE: Speak naturally as a human agent. 
  - NEVER say "Since you've mentioned", "Because you said", or "I see from our notes".
  - NEVER recite back the user's issue verbatim or summarize it technically. 
  - Transition smoothly: "I can certainly help you with that installation. Let's start by..."
- PERSONALIZATION: Use the user's name ONLY if it is a real name (not a phone number).
- SENTIMENT DETECTION: At the VERY END of every response, include exactly one tag: [SENTIMENT: Neutral], [SENTIMENT: Happy], [SENTIMENT: Sad], or [SENTIMENT: Angry] based on the user's last turn.

INTERRUPTION PROTOCOL:
- If interrupted, acknowledge and PIVOT to the new input immediately.

TICKET RESOLUTION & TERMINATION (STRICT SEQUENCE):
- PHASE 1: RESOLUTION CHECK (MANDATORY)
  - IF the user's issue seems resolved:
    - You MUST ask: "Has this resolved your issue today?"
    - NEGATIVE CONSTRAINT: You MUST NOT include [ACTION: RESOLVE_TICKET] or [ACTION: HANGUP] in this response. 
    - FAILURE CASE: Including a tag in the same turn as the question will cause a premature hangup. DO NOT DO IT.
- PHASE 2: CLOSURE (ONLY AFTER USER CONFIRMS)
  - IF and ONLY IF the user says "Yes", "Confirmed", or similar:
    - 1. SCRIPT: Wish them a wonderful day/evening.
    - 2. TAGS: Include [ACTION: RESOLVE_TICKET] AND [ACTION: HANGUP].
    - Example: "I'm glad I could help! Have a wonderful day. [ACTION: RESOLVE_TICKET] [ACTION: HANGUP]"
"""

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
    
    # Check for dedicated Whisper key, otherwise fall back to shared Groq client
    whisper_key = os.getenv("WHISPER_API_KEY")
    if whisper_key:
        client = Groq(api_key=whisper_key)
    else:
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
        print(f" [ASR: WHISPER] Transcript: '{text}'", flush=True)
        return text if len(text) > 1 else None
        
    except Exception as e:
        print(f" Whisper: {e}")
        return None

async def transcribe_deep(audio_data: Optional[str]) -> Optional[str]:
    """Deepgram ASR - Official SDK with Nova-3. PRIMARY ASR ENGINE.
    Expects base64-encoded PCM audio from Vonage.
    """
    if not audio_data:
        print(" [DEEPGRAM] No audio data provided", flush=True)
        return None
    
    api_key = os.getenv("DEEPGRAM_API_KEY")
    if not api_key:
        print(" [DEEPGRAM] CRITICAL ERROR: API KEY MISSING! Check .env file.", flush=True)
        return None

    try:
        # Decode base64 and convert raw PCM to WAV
        raw_bytes = base64.b64decode(audio_data)
        audio_bytes = pcm16_to_wav_bytes(raw_bytes)
        
        st = time.time()
        
        # Use simple sync client in a thread for now to match current turn-based flow
        client = DeepgramClient(api_key)
        options = {
            "model": "nova-3",
            "smart_format": True,
            "language": "en",
            "punctuate": True,
            "utterances": False
        }
        
        payload = {"buffer": audio_bytes}
        
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: client.listen.prerecorded.v("1").transcribe_file(payload, options)
        )
        
        # More robust transcript extraction
        if response and response.results and response.results.channels:
            text = response.results.channels[0].alternatives[0].transcript.strip()
            confidence = response.results.channels[0].alternatives[0].confidence if hasattr(response.results.channels[0].alternatives[0], 'confidence') else 'N/A'
            print(f" [ASR: DEEPGRAM] Transcript: '{text}' (Confidence: {confidence}, Time: {time.time()-st:.2f}s)", flush=True)
            return text if len(text) > 0 else None
        else:
            print(f" [DEEPGRAM] Empty response structure", flush=True)
            return None

    except Exception as e:
        print(f" [DEEPGRAM] SDK Exception: {e}", flush=True)
        import traceback
        traceback.print_exc()
        return None

async def agent_response(issue: str, kb: str, history: List[Dict], contact_name: Optional[str] = None, recent_tickets: List[Dict] = [], phone: Optional[str] = None, max_tokens: int = 500, temperature: float = 0.2, **kwargs) -> str:
    """Detailed and helpful 70B response."""
    client = _get_client()
    if not client:
        return "Let me check. Clear cache. Does that work?"
    
    # Debug: See what the AI is receiving
    print(f"DEBUG KB SENDING ({len(kb)} chars): {kb[:100]}...")

    # Inject contact name and tickets into system prompt if available
    context_prompt = HUMAN_PROMPT
    
    # Sanitize contact_name: If it's just a number, treat as Unknown to LLM
    display_name = contact_name
    if display_name and not re.search(r'[a-zA-Z]', str(display_name)):
        display_name = "Unknown"
        
    if display_name:
        context_prompt += f"\n\nCUSTOMER INFO: Talking to {display_name}."
    
    if phone:
        context_prompt = context_prompt.replace("[Phone]", phone)
    
    if recent_tickets:
        status_map = {2: "Open", 3: "Pending", 4: "Resolved", 5: "Closed"}
        priority_map = {1: "Low", 2: "Medium", 3: "High", 4: "Urgent"}
        
        ticket_data = "\n".join([
            f"- ID: {t.get('id')}, Status: {status_map.get(t.get('status'), 'Unknown')}, Priority: {priority_map.get(t.get('priority'), 'Unknown')} ({t.get('priority')}), Subject: {t.get('subject')}, Description: {t.get('description', 'No description available.')}"
            for t in recent_tickets
        ])
        print(f" [ANALYSIS] Comparing User Issue: '{issue}' against {len(recent_tickets)} Recent Tickets...")
        context_prompt += f"\n\nRECENT_TICKETS (Max 2):\n{ticket_data}"
    
    # Check if we have a ticket created for this current call
    active_id = kwargs.get("active_ticket_id")
    if not active_id:
        # Detect confirmed ticket from history
        for msg in reversed(history):
            if msg.get("role") == "assistant":
                match = re.search(r'\[ACTION: USE_TICKET: (\d+)\]', msg.get("content", ""))
                if match:
                    active_id = match.group(1)
                    break

    # 4. Check for High/Urgent Priority in ANY recent ticket (Global Warning)
    has_high_priority = False
    for t in recent_tickets:
        if t.get('priority') in [3, 4]:
            has_high_priority = True
            break
            
    if has_high_priority:
        print(f" [PRIORITY] High/Urgent ticket detected in Recent Tickets!", flush=True)
        context_prompt += "\n\nCRITICAL MANDATE: One or more tickets in HISTORY/RECENT_TICKETS are HIGH PRIORITY. You MUST APOLOGIZE and OFFER A TRANSFER immediately if this matches the user's issue. DO NOT mention the 'Ticket ID' number."

    if active_id:
        # Check priority of active ticket specifically
        priority_str = "Regular"
        for t in recent_tickets:
            if str(t.get('id')) == str(active_id):
                p = t.get('priority')
                if p in [3, 4]:
                    priority_str = "HIGH/URGENT"
                break
        
        print(f" [PRIORITY] Active Ticket {active_id} is {priority_str}", flush=True)
        context_prompt += f"\n\nACTIVE_SESSION_TICKET_ID: {active_id}. [PRIORITY: {priority_str}]. User has confirmed this is their issue. Use ticket notes to solve."
        if priority_str == "HIGH/URGENT":
             context_prompt += "\nATTENTION: This is a HIGH PRIORITY ticket. Offer transfer now if you haven't already. (Choice: 'Transfer to live agent' or 'Continue with me')."

    # FINAL REMINDER: Always include [ACTION: CREATE_TICKET: Description] for new issues!
    context_prompt += "\n\nCRITICAL: If new issue, user said 'No' to previous contact, AND NO ACTIVE_SESSION_TICKET_ID exists, you MUST include [ACTION: CREATE_TICKET: Description]. If existing ticket matches and user confirms, you MUST include [ACTION: USE_TICKET: ID]."

    # 5. Detect explicit mention of Previous Issue to skip Step 2
    issue_lower = issue.lower()
    if any(phrase in issue_lower for phrase in ["previous issue", "past issue", "status", "already talked", "my ticket"]):
         print(f" [INTENT] User is asking about an existing issue. Prompting AI to bypass verification.", flush=True)
         context_prompt += "\n\nUSER INTENT ALERT: User has explicitly asked about a PREVIOUS issue or STATUS. Do NOT ask if they have contacted an agent before. Proceed directly to Step 3 (Ticket Matching)."

    messages = [
        {"role": "system", "content": context_prompt},
        *history[-12:],
        {"role": "user", "content": f"Issue: {issue}\n\nKB Context:\n{kb or 'No specific Freshdesk KB path found - provide a standard L1 solution.'}"}
    ]
    
    try:
        loop = asyncio.get_event_loop()
        start_llm = time.perf_counter()
        resp = await loop.run_in_executor(
            None,
            lambda: client.chat.completions.create(
                model="llama-3.1-8b-instant",     #model is here dude "meta-llama/llama-4-maverick-17b-128e-instruct"............    #model is here dude ............
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature
            )
        )
        llm_duration = time.perf_counter() - start_llm
        
        response = resp.choices[0].message.content.strip()
        
        # FAILSAFE: Strip any unintentional Ticket ID mentions
        response = re.sub(r'\(?ID:\s*\d+\)?', '', response, flags=re.IGNORECASE)
        response = re.sub(r'ticket\s+id\s*[:#]?\s*\d*', '', response, flags=re.IGNORECASE)
        response = response.replace("  ", " ").strip()
        
        print(f" AI: {response[:150]}...")
        print(f" [LATENCY] LLM (Llama) generation took {llm_duration:.3f}s", flush=True)
        return response
        
    except Exception as e:
        err_msg = str(e)
        print(f" [ERROR] Llama Failure: {err_msg}")
        if "429" in err_msg:
            return "I'm sorry, I'm experiencing a high volume of requests right now. Please wait a few seconds and try again so I can assist you better."
        return "I'm sorry, I encountered an internal error. Please tell me more about the problem so I can help?"

#  BACKWARD COMPATIBLE: Keep old function name
async def process_audio(audio_data: Optional[str], kb: str, history: List[Dict]) -> str:
    """Full pipeline: ASR → Llama LLM."""
    
    # --- ASR SELECTION ---
    # Option A: Deepgram (Fastest)
    asr_text = await transcribe_deep(audio_data) if audio_data else None
    
    # Option B: Whisper (High accuracy fallback, currently commented out)
    # asr_text = await transcribe_whisper(audio_data) if audio_data else None
    # ---------------------

    # Fallback to text input (if ASR fails or audio is unavailable)
    issue = asr_text or "audio unavailable"
    
    # Llama response
    start_time = time.perf_counter()
    response = await agent_response(issue, kb, history)
    duration = time.perf_counter() - start_time
    print(f" [LATENCY] agent_response took {duration:.3f}s", flush=True)
    return response
