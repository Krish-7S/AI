import os
import re
import traceback
import time
from typing import List, Dict
from fastapi import FastAPI, Request, WebSocket
from dotenv import load_dotenv

# Look for .env in current dir and /app subdir
load_dotenv()
load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

from .state import ConversationState
from .voice import handle_voice_answer, handle_voice_asr, handle_voice_events, inject_vonage_tts, stop_vonage_tts, transfer_to_agent
from .freshdesk import create_ticket, update_ticket_status, update_contact_name, create_contact
from fastapi.responses import JSONResponse

call_state = ConversationState()
app = FastAPI()

# Add exception middleware to catch ALL errors
@app.middleware("http")
async def catch_exceptions_middleware(request: Request, call_next):
    try:
        print(f" [REQUEST] {request.method} {request.url.path}", flush=True)
        response = await call_next(request)
        print(f" [RESPONSE] {response.status_code}", flush=True)
        return response
    except Exception as e:
        print(f" [MIDDLEWARE ERROR] {type(e).__name__}: {e}", flush=True)
        traceback.print_exc()
        return JSONResponse(content={"error": str(e)}, status_code=500)


@app.get("/")
async def health():
    return {"status": "DEBUG_MODE_LOGGING_ACTIVE", "calls": len(call_state.calls)}

@app.get("/voice/tts")
async def tts_ncco(request: Request):
    """
    Serves the NCCO for TTS injection dynamically.
    This works around inline NCCO validation issues.
    """
    params = dict(request.query_params)
    text = params.get("text", "Processing...")
    call_uuid = params.get("uuid", "")
    
    # Construct Websocket URL
    public_url = os.getenv("PUBLIC_URL", "")
    ws_url = public_url.replace("http://", "ws://").replace("https://", "wss://") + "/voice/stream"
    
    # Generate NCCO: Talk Only (Debug)
    ncco = [
        {
            "action": "talk",
            "text": text,
            "bargeIn": True,
            "language": "en-US"
        }
        # {
        #     "action": "connect",
        #     "endpoint": [{
        #         "type": "websocket",
        #         "uri": ws_url,
        #         "content-type": "audio/l16;rate=16000",
        #         "headers": {
        #             "uuid": call_uuid
        #         }
        #     }]
        # }
    ]
    
    print(f" [TTS_NCCO] Serving NCCO for uuid={call_uuid}", flush=True)
    return JSONResponse(content=ncco)

@app.post("/voice/events")
async def events(request: Request):
    return await handle_voice_events(call_state, request)

@app.get("/voice/answer")
async def answer(request: Request):
    print(f" >>> [DEBUG] ENTERING handle_voice_answer", flush=True)
    return await handle_voice_answer(call_state, request)

# --- WEBSOCKET STREAMING ---
from fastapi import WebSocket
from .groq import DeepgramStreamer, agent_response
from .voice import inject_vonage_tts
import asyncio
import json

# Store active websocket for sending TTS back
_active_websocket = None
_processing_ai = False
_main_loop = None
_current_call_uuid = None

@app.websocket("/voice/stream")
async def voice_stream(websocket: WebSocket):
    global _active_websocket, _conversation_history, _processing_ai, _main_loop, _current_call_uuid
    
    await websocket.accept()
    print(" [WS] Client Connected")
    
    _active_websocket = websocket
    _processing_ai = False
    _current_call_uuid = None
    
    # Capture the running event loop
    _main_loop = asyncio.get_running_loop()
    
    # Connect Deepgram FIRST
    streamer = DeepgramStreamer()
    if not streamer.connect():
        print(" [WS] Failed to connect to Deepgram")
        await websocket.close()
        return
    
    # Set up speech ended callback
    def on_speech_ended(transcript: str):
        global _processing_ai, _main_loop, _current_call_uuid
        if _processing_ai:
            print(" [TURN] AI already processing, queuing...", flush=True)
            return
        
        print(f" [TURN] Processing user input: '{transcript}'", flush=True)
        _processing_ai = True
        
        # Schedule AI processing
        try:
            if _main_loop and _main_loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    process_and_respond(transcript, websocket, _current_call_uuid),
                    _main_loop
                )
            else:
                print(" [TURN] No main loop available", flush=True)
                _processing_ai = False
        except Exception as e:
            print(f" [TURN] Failed to schedule AI: {e}", flush=True)
            _processing_ai = False
    
    streamer.set_speech_ended_callback(on_speech_ended)

    # Set up barge-in (interruption) callback
    def on_barge_in():
        global _current_call_uuid, _main_loop
        if _current_call_uuid and _main_loop:
            # Check if barge-in makes sense (only if bot might be speaking)
            # We don't have a strict 'is_speaking' flag but stop_vonage_tts is safe to call
            # as it returns 404 if nothing is playing.
            call_data = call_state.calls.get(_current_call_uuid, {})
            region_url = call_data.get("region_url")
            
            asyncio.run_coroutine_threadsafe(
                stop_vonage_tts(_current_call_uuid, region_url),
                _main_loop
            )
            # Give Vonage regional endpoint a tiny moment to process the stop
            # before we potentially start any new logic.

    streamer.set_barge_in_callback(on_barge_in)
    
    try:
        packet_count = 0
        while True:
            message = await websocket.receive()
            
            if "text" in message:
                # Metadata / Handshake - Capture UUID
                try:
                    data = json.loads(message["text"])
                    # Check root or headers for UUID
                    possible_uuid = data.get("uuid") or data.get("headers", {}).get("uuid")
                    if possible_uuid:
                        _current_call_uuid = possible_uuid
                        print(f" [WS] Call UUID captured: {_current_call_uuid}", flush=True)
                except:
                    pass
            
            if "bytes" in message:
                packet_count += 1
                if packet_count % 100 == 0:
                    print(f" [WS] Recv {packet_count} packets", flush=True)
                await streamer.send_audio(message["bytes"])
                
    except Exception as e:
        print(f" [WS] Error/Disconnect: {e}")
    finally:
        streamer.close()
        _active_websocket = None
        print(" [WS] Closed")


async def execute_ai_actions(call_uuid: str, all_tags: List, current_call_state: Dict, region_url: str = None, text_len: int = 0):
    """Executes AI actions (CRM updates, transfers) in the background."""
    try:
        from .voice import transfer_to_agent
        from .freshdesk import create_ticket, update_ticket_status, update_contact_name, create_contact
        
        for tag_type, tag_content in all_tags:
            print(f" [{tag_type}] Processing: {tag_content}", flush=True)
            
            if tag_type == "SENTIMENT":
                sentiment = tag_content.strip().capitalize()
                print(f" [ANALYSIS] User sentiment: {sentiment}", flush=True)
                current_call_state["sentiment"] = sentiment
                await call_state.set_call_state(call_uuid, current_call_state)
                continue

            # Original Action Logic
            parts = [p.strip() for p in tag_content.split(':')]
            cmd = parts[0].upper()
            
            if cmd == "TRANSFER":
                dest = os.getenv("AGENT_NUMBER", "18335645478")
                bot_num = current_call_state.get("bot_number", "18335645478")
                # Transfers MUST remain blocking or we might lose the turn context
                await transfer_to_agent(call_uuid, dest, bot_num, region_url)
                
            elif cmd == "CREATE_TICKET":
                desc = parts[1] if len(parts) > 1 else "No description provided"
                contact_phone = current_call_state.get("from")
                contact_id = current_call_state.get("contact_id")
                sentiment = current_call_state.get("sentiment", "Neutral")
                ticket_id = await create_ticket(call_uuid, desc, contact_phone, sentiment, requester_id=contact_id)
                if ticket_id:
                    current_call_state["ticket_id"] = ticket_id
                    await call_state.set_call_state(call_uuid, current_call_state)
                    
            elif cmd == "RESOLVE_TICKET":
                t_id = parts[1] if len(parts) > 1 else current_call_state.get("ticket_id")
                if t_id:
                    await update_ticket_status(int(t_id), 4) # 4 = Resolved
                    
            elif cmd == "UPDATE_NAME":
                new_name = parts[1] if len(parts) > 1 else "Unknown"
                contact_id = current_call_state.get("contact_id")
                contact_phone = current_call_state.get("from")
                
                if contact_id:
                    await update_contact_name(contact_id, new_name)
                elif contact_phone:
                    new_contact = await create_contact(new_name, contact_phone)
                    if new_contact:
                        current_call_state["contact_id"] = new_contact.get("id")
                
                current_call_state["contact_name"] = new_name
                await call_state.set_call_state(call_uuid, current_call_state)
            
            elif cmd == "USE_TICKET":
                t_id = parts[1] if len(parts) > 1 else None
                if t_id:
                    print(f" [ACTION] User confirmed Ticket #{t_id}", flush=True)
                    current_call_state["ticket_id"] = t_id
                    await call_state.set_call_state(call_uuid, current_call_state)

            elif cmd == "HANGUP":
                # Wait for TTS to finish. ~12 chars per second approx.
                delay = (text_len / 12.0) + 1.2 # Add buffer
                delay = min(delay, 20.0) # Cap at 20s
                print(f" [ACTION] Delaying hangup by {delay:.2f}s for TTS completion (text_len={text_len})", flush=True)
                await asyncio.sleep(delay)
                from .voice import hangup_call
                await hangup_call(call_uuid, region_url)

    except Exception as e:
        print(f" [ACTION] Background execution error: {e}", flush=True)
        traceback.print_exc()

async def process_and_respond(transcript: str, websocket: WebSocket, call_uuid: str):
    """Process user speech with AI and respond using Vonage TTS."""
    global _processing_ai
    start_total = time.perf_counter()
    
    try:
        if not call_uuid:
            print(" [AI] Cannot process: No Call UUID", flush=True)
            return

        print(f" [AI] Processing: '{transcript}'", flush=True)
        
        # 1. Update Persistent History (User Turn)
        state_start = time.perf_counter()
        await call_state.append_history(call_uuid, "user", transcript)
        
        # 2. Retrieve current call state for context
        current_call_state = await call_state.get_call_state(call_uuid)
        state_duration = time.perf_counter() - state_start
        print(f" [LATENCY] State management took {state_duration:.3f}s", flush=True)
        
        # 3. Get AI response with context
        ai_start = time.perf_counter()
        from .groq import agent_response
        ai_reply = await agent_response(
            issue=transcript,
            kb="",
            history=current_call_state.get("history", []),
            contact_name=current_call_state.get("contact_name"),
            recent_tickets=current_call_state.get("recent_tickets", []),
            active_ticket_id=current_call_state.get("ticket_id"),
            phone=current_call_state.get("phone")
        )
        ai_duration = time.perf_counter() - ai_start
        print(f" [LATENCY] AI generation (agent_response) took {ai_duration:.3f}s", flush=True)
        
        # 4. Update Persistent History (Assistant Turn)
        await call_state.append_history(call_uuid, "assistant", ai_reply)
        
        # 5. Parse Actions & Sentiment
        parse_start = time.perf_counter()
        all_tags = re.findall(r'\[(ACTION|SENTIMENT):\s*([^\]]+)\]', ai_reply)
        
        # 6. Clean AI response for TTS (Strip all tags)
        clean_speech = re.sub(r'\[(ACTION|SENTIMENT):[^\]]+\]', '', ai_reply).strip()
        parse_duration = time.perf_counter() - parse_start
        print(f" [LATENCY] Parse & Clean took {parse_duration:.3f}s", flush=True)
        
        # 7. Start TTS Injection immediately (PRIORITY 1)
        tts_start = time.perf_counter()
        region_url = current_call_state.get("region_url")
        if call_uuid:
            print(f" [TTS] Injecting Vonage TTS (Clean): '{clean_speech[:50]}...'", flush=True)
            # Fire TTS immediately. We await it since it's the bridge to the user.
            await inject_vonage_tts(call_uuid, clean_speech, region_url)
        else:
            print(" [TTS] No UUID captured, cannot use Vonage TTS", flush=True)
        tts_final_duration = time.perf_counter() - tts_start
        print(f" [LATENCY] Total TTS step took {tts_final_duration:.3f}s", flush=True)
        
        total_duration = time.perf_counter() - start_total
        print(f" [LATENCY] TOTAL PIPELINE TIME: {total_duration:.3f}s", flush=True)

        # 8. Offload CRM/Actions to BACKGROUND (PRIORITY 2)
        if all_tags:
            import asyncio
            asyncio.create_task(execute_ai_actions(call_uuid, all_tags, current_call_state, region_url, text_len=len(clean_speech)))
        
    except Exception as e:
        print(f" [AI] Error: {e}", flush=True)
        import traceback
        traceback.print_exc()
    finally:
        _processing_ai = False
        print(f" [TURN] Ready for next input", flush=True)

if __name__ == "__main__":
    import uvicorn
    print("\n" + "="*50, flush=True)
    print("  SANDEZA VOICE AI v2.DEBUG.03 - [REL-01] ", flush=True)
    print("="*50 + "\n", flush=True)
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
