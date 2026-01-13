from fastapi import Request
from fastapi.responses import JSONResponse
import os
import httpx
import base64
import re
import time
import asyncio
import traceback
from typing import List, Dict, Any
from dotenv import load_dotenv

import vonage

# App-specific imports
from .groq import agent_response, transcribe_deep
from .state import ConversationState
from .freshdesk import search_contact_by_phone, get_latest_tickets, create_ticket, update_ticket_status, update_contact_name, add_ticket_note, create_contact

# Look for .env in current dir and /app subdir
load_dotenv()
load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

PUBLIC_URL = os.getenv("PUBLIC_URL", "https://carpogonial-candace-nonjuridically.ngrok-free.dev")
FRESH_DOMAIN = os.getenv("FRESH_DOMAIN")
AGENT_NUMBER = os.getenv("AGENT_NUMBER", "14702834062") # Default fallback
VONAGE_APP_ID = os.getenv("VONAGE_APPLICATION_ID")
VONAGE_PRIVATE_KEY_PATH = os.getenv("VONAGE_PRIVATE_KEY_PATH", "private.key")

import urllib.parse
import json
import jwt

# Caching for Performance
_VONAGE_PRIVATE_KEY = None
_SHARED_HTTP_CLIENT = None

def _get_vonage_private_key():
    global _VONAGE_PRIVATE_KEY
    if _VONAGE_PRIVATE_KEY is None:
        # Strategy: Try configured path, then app subfolder, then root
        paths_to_try = [
            VONAGE_PRIVATE_KEY_PATH,
            os.path.join(os.path.dirname(__file__), VONAGE_PRIVATE_KEY_PATH),
            os.path.join(os.path.dirname(os.path.dirname(__file__)), VONAGE_PRIVATE_KEY_PATH),
            "private.key",
            "app/private.key"
        ]
        for p in paths_to_try:
            if p and os.path.exists(p):
                try:
                    with open(p, 'r') as key_file:
                        _VONAGE_PRIVATE_KEY = key_file.read()
                        print(f" [VONAGE] Private key loaded from: {p}", flush=True)
                        break
                except Exception as e:
                    print(f" [VONAGE] Failed to read private key at {p}: {e}", flush=True)
        
        if _VONAGE_PRIVATE_KEY is None:
            print(f" [VONAGE] CRITICAL: Private key not found in any standard location!", flush=True)
            
    return _VONAGE_PRIVATE_KEY

def _generate_vonage_jwt(jti_prefix: str = "jwt"):
    """Internal helper to generate a Vonage JWT."""
    private_key = _get_vonage_private_key()
    if not private_key:
        return None
    
    now = int(time.time())
    claims = {
        "application_id": VONAGE_APP_ID,
        "iat": now,
        "exp": now + 60,
        "jti": f"{jti_prefix}_{now}"
    }
    return jwt.encode(claims, private_key, algorithm='RS256')

def _get_http_client():
    global _SHARED_HTTP_CLIENT
    if _SHARED_HTTP_CLIENT is None or _SHARED_HTTP_CLIENT.is_closed:
        _SHARED_HTTP_CLIENT = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))
    return _SHARED_HTTP_CLIENT

async def inject_vonage_tts(call_uuid: str, text: str, region_url: str = None):
    """
    Inject TTS into live call using Vonage REST API (Play TTS / Talk endpoint).
    Uses raw HTTP to bypass SDK 'api_host' configuration limitations and ensure proper Regional Endpoint targeting.
    """
    print(f" [VONAGE] Injecting TTS via play_tts_into_call (Raw HTTP): '{text[:50]}...'", flush=True)
    
    try:
        # 1. Determine correct endpoint (Regional or Global)
        if not region_url:
            print(" [VONAGE] Warning: No region_url provided, defaulting to api.nexmo.com")
            base_url = "https://api.nexmo.com"
        else:
            # region_url typically comes as 'https://api-us-3.vonage.com' from webhooks
            base_url = region_url
            
        url = f"{base_url}/v1/calls/{call_uuid}/talk"
        
        # 2. Construct Payload
        # Docs: https://developer.vonage.com/en/api/voice#startTalk
        # REMOVED voice_name to avoid conflict with language
        payload = {
            "text": text,
            "language": "en-US"
        }
        
        # 3. Generate JWT
        token = _generate_vonage_jwt("talk")
        if not token:
            return
        
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        
        # 4. Send Request
        http_client = _get_http_client()
        print(f" [VONAGE] HTTP PUT {url}", flush=True)
        start_tts = time.perf_counter()
        resp = await http_client.put(url, json=payload, headers=headers)
        tts_duration = time.perf_counter() - start_tts
            
        if resp.status_code == 200:
            print(f" [VONAGE] TTS Injection Success: 200 OK - {resp.json()}", flush=True)
            print(f" [LATENCY] Vonage TTS Injection (Network) took {tts_duration:.3f}s", flush=True)
        else:
            print(f" [VONAGE] Injection Failed: {resp.status_code} - {resp.text}", flush=True)

    except Exception as e:
        print(f" [VONAGE] Raw Injection Error: {e}", flush=True)
        traceback.print_exc()

async def stop_vonage_tts(call_uuid: str, region_url: str = None):
    """
    Stops any ongoing TTS playback for the given call leg.
    This is used to implement manual barge-in.
    """
    print(f" [VONAGE] Stopping TTS for {call_uuid}...", flush=True)
    
    try:
        base_url = region_url if region_url else "https://api.nexmo.com"
        url = f"{base_url}/v1/calls/{call_uuid}/talk"
        
        # 1. Generate JWT
        private_key = _get_vonage_private_key()
        if not private_key:
            return
            
        now = int(time.time())
        claims = {
            "application_id": VONAGE_APP_ID,
            "iat": now,
            "exp": now + 60,
            "jti": f"stop_{now}"
        }
        token = jwt.encode(claims, private_key, algorithm='RS256')
        
        headers = {"Authorization": f"Bearer {token}"}
        
        http_client = _get_http_client()
        print(f" [VONAGE] HTTP DELETE {url}", flush=True)
        resp = await http_client.delete(url, headers=headers)
        if resp.status_code in (200, 204):
            print(f" [VONAGE] TTS Stopped Successfully", flush=True)
        else:
                # 404 might mean no TTS was playing, which is fine
            print(f" [VONAGE] Stop Result: {resp.status_code}", flush=True)

    except Exception as e:
        print(f" [VONAGE] Stop Error: {e}", flush=True)

async def hangup_call(call_uuid: str, region_url: str = None):
    """
    Terminates a call programmatically via Vonage REST API.
    """
    print(f" [VONAGE] Hanging up call {call_uuid}...", flush=True)
    try:
        base_url = region_url if region_url else "https://api.nexmo.com"
        url = f"{base_url}/v1/calls/{call_uuid}"
        
        payload = {"action": "hangup"}
        token = _generate_vonage_jwt("hangup")
        if not token:
            return
            
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        
        client = _get_http_client()
        resp = await client.put(url, json=payload, headers=headers)
        
        if resp.status_code == 200:
            print(f" [VONAGE] Call {call_uuid} hung up successfully.", flush=True)
        else:
            print(f" [VONAGE] Hangup Result: {resp.status_code} {resp.text}", flush=True)
            
    except Exception as e:
        print(f" [VONAGE] Hangup Error: {e}", flush=True)

async def transfer_call_via_api(call_uuid: str, destination_number: str):
    """
    Transfers a live call using the Vonage Voice Method (Modifying the call).
    This equates to: curl -X PUT https://api.nexmo.com/v1/calls/{uuid} ...
    """
    print(f" [API_TRANSFER] Attempting to transfer {call_uuid} to {destination_number}...")
    
    # NOTE: This requires generating a JWT. 
    # Since we don't have the 'vonage' lib imported, we will assume a helper or 
    # placeholder for the JWT generation if the user wants to use this strictly.
    # For now, we print the would-be request to show compliance with the request logic.
    
    url = f"https://api.nexmo.com/v1/calls/{call_uuid}"
    
    # We construct the NCCO destination
    payload = {
        "action": "transfer",
        "destination": {
            "type": "ncco",
            "ncco": [
                 {
                    "action": "connect",
                    "from": AGENT_NUMBER,
                    "endpoint": [{
                        "type": "phone",
                        "number": destination_number
                    }]
                }
            ]
        }
    }
    
    print(f" [API_TRANSFER] Payload: {payload}")
    # To actually execute, we need a valid JWT.
    # headers = {"Authorization": f"Bearer {jwt_token}", "Content-Type": "application/json"}
    # async with httpx.AsyncClient() as client:
    #     resp = await client.put(url, json=payload, headers=headers)
    #     print(f" [API_TRANSFER] Result: {resp.status_code} {resp.text}")

async def transfer_to_agent(call_uuid: str, agent_number: str, from_number: str, region_url: str = None):
    """
    Transfers a live call to an agent using the Vonage Voice REST API.
    Uses raw HTTP to ensure compatibility with Regional Endpoints.
    """
    # Ensure E.164 formatting (add + if missing)
    agent_number = agent_number if agent_number.startswith("+") else f"+{agent_number}"
    from_number = from_number if from_number.startswith("+") else f"+{from_number}"
    
    print(f" [VONAGE] Transferring call {call_uuid} TO {agent_number} FROM {from_number}...", flush=True)
    
    try:
        base_url = region_url if region_url else "https://api.nexmo.com"
        url = f"{base_url}/v1/calls/{call_uuid}"
        
        # 1. Generate JWT
        with open(VONAGE_PRIVATE_KEY_PATH, 'r') as key_file:
            private_key = key_file.read()
            
        now = int(time.time())
        claims = {
            "application_id": VONAGE_APP_ID,
            "iat": now,
            "exp": now + 60,
            "jti": f"agent_transfer_{now}"
        }
        token = jwt.encode(claims, private_key, algorithm='RS256')
        
        # 2. Construct NCCO-based Transfer Payload
        payload = {
            "action": "transfer",
            "destination": {
                "type": "ncco",
                "ncco": [
                    {
                        "action": "connect",
                        "from": from_number,
                        "endpoint": [
                            {
                                "type": "phone",
                                "number": agent_number
                            }
                        ]
                    }
                ]
            }
        }
        
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        
        # 3. Send Request
        async with httpx.AsyncClient(timeout=10.0) as http_client:
            print(f" [VONAGE] HTTP PUT {url} (Agent Transfer)", flush=True)
            resp = await http_client.put(url, json=payload, headers=headers)
            
            if resp.status_code in (200, 204):
                print(f" [VONAGE] Agent Transfer Initiated Successfully", flush=True)
                return True
            else:
                print(f" [VONAGE] Agent Transfer Failed: {resp.status_code} - {resp.text}", flush=True)
                return False

    except Exception as e:
        print(f" [VONAGE] Agent Transfer Error: {e}", flush=True)
        traceback.print_exc()
        return False


async def background_freshdesk_lookup(state: ConversationState, call_uuid: str, from_number: str):
    """Perform Freshdesk lookup in background to reduce initial call latency."""
    print(f" [BACKGROUND] Starting lookup for {from_number}...", flush=True)
    contact_name = None
    recent_tickets = []
    
    try:
        contact = await search_contact_by_phone(from_number)
        
        # If no contact found, create a placeholder immediately
        if not contact or not contact.get("id"):
            print(f" [BACKGROUND] No contact found for {from_number}. Creating placeholder...", flush=True)
            contact = await create_contact(name=from_number, phone=from_number)
        
        if contact and contact.get("id"):
            name = contact.get("name")
            # STRICTOR CHECK: Name MUST contain at least one letter [a-zA-Z]
            # If name is just numbers, phone-formatted, or "Unknown", treat as anonymous for AI grooming
            is_valid_name = name and re.search(r'[a-zA-Z]', str(name)) and name.lower() != "unknown"
            
            if not is_valid_name:
                print(f" [BACKGROUND] Numeric or placeholder name detected '{name}'. Treating as anonymous.", flush=True)
                contact_name = None # Set to None so AI prompt uses "Unknown"
            else:
                contact_name = name
                print(f" [BACKGROUND] Found valid contact: {contact_name}", flush=True)
            
            recent_tickets = await get_latest_tickets(contact.get("id"))
            call_state_update = {
                "contact_id": contact.get("id"),
                "contact_name": contact_name,
                "recent_tickets": recent_tickets
            }
    except Exception as e:
        print(f" [BACKGROUND] Lookup error: {e}", flush=True)

    # Update state with found info
    call_state = await state.get_call_state(call_uuid)
    if call_state:
        if 'call_state_update' in locals():
            call_state.update(call_state_update)
        call_state["lookup_done"] = True
        await state.set_call_state(call_uuid, call_state)
        print(f" [BACKGROUND] lookup sync complete for {call_uuid}", flush=True)

async def fetch_combined_knowledge(query: str) -> str:
    if not query.strip():
        return ""
    
    # words = re.sub(r'[^\w\s]', '', query.lower()).split()
    # words = [w for w in words if len(w) > 2][:6]
    # search_term = ' '.join(words)
    
    # Reuse the refined logic from before
    search_term = query # Or more sophisticated parsing

    print(f" Searching KB for: '{search_term}'", flush=True)
    
    kb_snippets = []
    
    async def fetch_solutions():
        try:
            async with httpx.AsyncClient(timeout=1.5) as client:
                resp = await client.get(
                    f"https://{FRESH_DOMAIN}/support/search/solutions.json",
                    params={'term': search_term}
                )
                data = resp.json()
                articles = data.get('data', []) if isinstance(data, dict) else data
                for art in articles[:2]:
                    title = art.get('title', '')
                    raw_desc = art.get('description') or art.get('description_text') or art.get('desc') or ""
                    desc = re.sub(r'<[^>]*>', ' ', str(raw_desc)).strip()
                    kb_snippets.append(f"ARTICLE: {title}\nSTEPS: {desc[:800]}")
        except Exception as e:
            print(f"Solutions search failed: {e}")

    async def fetch_community_archives():
        try:
            # Archives search - faster timeout
            url = f"https://community.freshworks.com/search?category=Using+Freshdesk+%3E+Archives+-+Freshdesk&q={search_term.replace(' ', '+')}"
            async with httpx.AsyncClient(timeout=1.5) as client:
                resp = await client.get(url)
                html = resp.text
                results = re.findall(r'<a[^>]*class="[^"]*forum-search-result__title[^"]*"[^>]*>(.*?)</a>.*?<div[^>]*class="[^"]*forum-search-result__content[^"]*"[^>]*>(.*?)</div>', html, re.DOTALL)
                for title, content in results[:2]:
                    title_clean = re.sub(r'<[^>]*>', '', title).strip()
                    content_clean = re.sub(r'<[^>]*>', ' ', content).strip()
                    kb_snippets.append(f"COMMUNITY ARCHIVE: {title_clean}\nSOLUTION: {content_clean[:800]}")
        except Exception as e:
            print(f"Community search failed: {e}")

    await asyncio.gather(fetch_solutions(), fetch_community_archives())
    
    combined_text = "\n\n".join(kb_snippets)
    print(f"CONTEXT READY: {len(kb_snippets)} sources.")
    return combined_text


async def handle_voice_asr(state: ConversationState, _a, _b, request: Request):
    data = await request.json()
    call_uuid = data.get("uuid")
    
    call_state = await state.get_call_state(call_uuid)
    if not call_state:
        return JSONResponse(content={"error": "No session"}, status_code=404)

    # 1. Wait for background lookup if needed (Max 2.5 seconds)
    wait_count = 0
    while not call_state.get("lookup_done") and wait_count < 25:
        await asyncio.sleep(0.1)
        call_state = await state.get_call_state(call_uuid)
        wait_count += 1
    
    if call_state.get("processing"):
        print(" Processing...")
        return JSONResponse(content=[])
    
    call_state["processing"] = True
    await state.set_call_state(call_uuid, call_state)
    
    turn_start = time.time()
    
    # 2. Parse Speech - WHISPER (Enabled for testing)
    # asr_marker = "DEEPGRAM"
    
    speech_text = ""
    asr_marker = "WHISPER"
    
    try:
        # DEBUG: Print exact keys received to diagnose "No Audio" cases
        print(f" [DEBUG_PAYLOAD] Received Keys: {list(data.keys())}", flush=True)
        if 'status' in data:
            print(f" [DEBUG_PAYLOAD] Status: {data.get('status')} | Reason: {data.get('reason')}", flush=True)
        if 'speech' in data:
            print(f" [DEBUG_PAYLOAD] Speech Data: {data.get('speech')}", flush=True)
            
        audio_data = data.get("audio")
        # Fallback: Check for recording_url (common in some Vonage configs)
        if not audio_data and data.get("recording_url"):
            print(f" [VONAGE] Found recording_url. Downloading...", flush=True)
            audio_data = data.get("recording_url") # transcribe_deep handles URLs
            
        if not audio_data:
            print(" [ERROR] No audio data or recording_url received from Vonage!", flush=True)
            speech_text = "[NO_AUDIO]"
        else:
            st = time.time()
            # WHISPER ASR (Toggled ON)
            # Make sure transcribe_whisper is imported in the header!
            from .groq import transcribe_whisper
            speech_text = await transcribe_whisper(audio_data)
            print(f" [LATENCY] ASR Process: {time.time()-st:.2f}s")
            
            if not speech_text:
                print(" [ERROR] Deepgram returned empty transcription!", flush=True)
                asr_marker = "DEEPGRAM_FAILED"
                speech_text = "[ASR_FAILED]"
                
    except Exception as e:
        print(f" [ERROR] Speech transcription failed: {e}", flush=True)
        asr_marker = "DEEPGRAM_ERROR"
        speech_text = "[ASR_ERROR]"
    
    print(f" USER ({asr_marker}): '{speech_text}' (Contact: {call_state.get('contact_name', 'Unknown')})", flush=True)
    
    # ASR Error Handling: Don't filter ASR error tags
    asr_error_tags = {"[ASR_FAILED]", "[ASR_ERROR]", "[NO_AUDIO]"}
    if speech_text in asr_error_tags:
        print(f"  [ASR_ERROR] Deepgram transcription issue: {speech_text}", flush=True)
        # Keep the error tag so AI can handle it appropriately
    else:
        # Noise Filter: Discard common disturbance/gibberish artifacts
        noise_artifacts = {"hau", "uh", "um", "the wind", "background noise", "[noise]", "disturbance"}
        cleaned_speech = re.sub(r'[^\w\s]', '', speech_text.lower().strip())
        
        is_noise = False
        # Expand whitelist: Allow common confirmation/greeting words even if short
        whitelist = {"yes", "no", "ok", "help", "yeah", "yep", "sure", "yup", "hi", "hey"}
        if speech_text and len(speech_text) < 5 and cleaned_speech not in whitelist:
            is_noise = True
        elif cleaned_speech in noise_artifacts:
            is_noise = True
            
        if not speech_text or len(speech_text) < 2 or is_noise:
            if is_noise:
                print(f"  [NOISE] Filtering artifact: '{speech_text}'")
            speech_text = "[SILENCE]"
            print(f"  Silence or noise detected for {call_state.get('contact_name', 'Unknown')}", flush=True)
    
    # 3. Synchronous Pipeline
    try:
        await state.append_history(call_uuid, "user", speech_text)
        
        # ASR ERROR SHORT-CIRCUIT: Don't waste time on KB/LLM if ASR failed
        if speech_text in asr_error_tags:
            print(f" [SHORT-CIRCUIT] Skipping KB/LLM due to ASR failure: {speech_text}", flush=True)
            # Simple fallback response
            ai_response = "I'm sorry, I couldn't hear you clearly due to a connection issue. Could you please repeat that?"
            
            # Simple NCCO with input
            ncco = [
                {"action": "talk", "text": ai_response, "bargeIn": True},
                {"action": "input", "type": ["speech"], "eventUrl": [f"{PUBLIC_URL}/voice/asr"], "speech": {"language": "en-US", "endOnSilence": 1.5}}
            ]
            call_state["processing"] = False
            await state.set_call_state(call_uuid, call_state)
            return JSONResponse(content=ncco)

        # Confidence Filter: Skip KB for short/confirmation turns
        kb = ""
        st = time.time()
        confirmations = {"yes", "no", "okay", "ok", "yep", "sure", "correct", "perfect", "done", "completed"}
        query_clean = re.sub(r'[^\w\s]', '', speech_text.lower().strip())
        
        if len(speech_text) < 10 or query_clean in confirmations:
            print(f" [LATENCY] KB Search SKIPPED for short/confirmation turn: '{speech_text}'")
        else:
            kb = await fetch_combined_knowledge(speech_text)
            print(f" [LATENCY] KB Search: {time.time()-st:.2f}s")
        
        active_id = call_state.get("ticket_id")
        
        st = time.time()
        ai_response = await agent_response(
            speech_text, 
            kb, 
            call_state["history"], 
            contact_name=call_state.get("contact_name"),
            recent_tickets=call_state.get("recent_tickets", []),
            active_ticket_id=active_id,
            phone=call_state.get("phone"),
            max_tokens=600,
            temperature=0.3
        )
        print(f" [LATENCY] LLM Turn: {time.time()-st:.2f}s")
        
        # 3. Update Persistent History (Assistant Turn) - Added missing sync
        await state.append_history(call_uuid, "assistant", ai_response)

        # 3a. Detect Sentiment Action (Added)
        sentiment_match = re.search(r'\[SENTIMENT:\s*([^\]]+)\]', ai_response)
        if sentiment_match:
            sentiment = sentiment_match.group(1).strip().capitalize()
            print(f" [FALLBACK] Sentiment detected: {sentiment}", flush=True)
            call_state["sentiment"] = sentiment
            await state.set_call_state(call_uuid, call_state)
            # Strip sentiment tag
            ai_response = re.sub(r'\[SENTIMENT:.*?\]', '', ai_response).strip()

        # 3b. Detect Ticket Actions
        create_match = re.search(r'\[ACTION: CREATE_TICKET: (.*?)\]', ai_response)
        if create_match:
            issue_summary = create_match.group(1)
            print(f" [TICKET] AI requested new ticket for: {issue_summary}", flush=True)
            active_id = await create_ticket(call_uuid, issue_summary, call_state.get("phone"), call_state.get("sentiment", "Neutral"), requester_id=call_state.get("contact_id"))
            call_state["ticket_id"] = active_id
            await state.set_call_state(call_uuid, call_state)
            ai_response = re.sub(r'\[ACTION: CREATE_TICKET: .*?\]', '', ai_response).strip()

        # 3g. Detect Ticket Adoption Action (for matched existing tickets)
        use_match = re.search(r'\[ACTION: USE_TICKET: (\d+)\]', ai_response)
        if use_match:
            adopted_id = use_match.group(1)
            print(f" [SESSION] Adopting matching ticket: {adopted_id}", flush=True)
            call_state["ticket_id"] = adopted_id
            await state.set_call_state(call_uuid, call_state)
            ai_response = re.sub(r'\[ACTION: USE_TICKET: \d+\]', '', ai_response).strip()

        # 3b. Detect Resolution Action
        resolve_match = re.search(r'\[ACTION: RESOLVE_TICKET: (\d+)\]', ai_response)
        if resolve_match:
            target_ticket_id = resolve_match.group(1)
            print(f" [RESOLVE] AI requested to resolve ticket: {target_ticket_id}", flush=True)
            await update_ticket_status(int(target_ticket_id), status=4)
            ai_response = re.sub(r'\[ACTION: RESOLVE_TICKET: \d+\]', '', ai_response).strip()

        # 3c. Detect Contact Name Update Action
        name_match = re.search(r'\[ACTION: UPDATE_NAME: (.*?)\]', ai_response)
        if name_match and call_state.get("contact_id"):
            new_name = name_match.group(1).strip()
            # Only update if current name is empty, numeric, or "Unknown"
            current_name = call_state.get("contact_name")
            is_placeholder = not current_name or current_name.lower() == "unknown" or re.match(r'^\+?\d+$', str(current_name).replace(" ", ""))
            
            if is_placeholder and new_name.lower() != "unknown" and not re.match(r'^\+?\d+$', new_name.replace(" ", "")):
                print(f" [CONTACT] Updating contact {call_state.get('contact_id')} name to: {new_name}", flush=True)
                await update_contact_name(call_state["contact_id"], new_name)
                call_state["contact_name"] = new_name
                await state.set_call_state(call_uuid, call_state)
            else:
                print(f" [CONTACT] Skipping name update to '{new_name}' because current name '{current_name}' is already valid or new name is placeholder.", flush=True)
            ai_response = re.sub(r'\[ACTION: UPDATE_NAME: .*?\]', '', ai_response).strip()

        # 3d. Detect Hangup Action
        should_hangup = "[ACTION: HANGUP]" in ai_response
        if should_hangup:
            print(f" [HANGUP] AI requested to end the call", flush=True)
            ai_response = ai_response.replace("[ACTION: HANGUP]", "").strip()

        # 3e. Detect Wait Action
        should_wait = "[ACTION: WAIT]" in ai_response
        if should_wait:
            print(f" [WAIT] AI requested to wait for the user", flush=True)
            ai_response = ai_response.replace("[ACTION: WAIT]", "").strip()

        # 3f. Detect Transfer Action
        # Tag format: [ACTION: TRANSFER] or [ACTION: TRANSFER: 1234567890]
        transfer_match = re.search(r'\[ACTION: TRANSFER(?::\s*(\d+))?\]', ai_response)
        should_transfer = False
        target_number = AGENT_NUMBER

        # Check for transfer directive
        if transfer_match:
            should_transfer = True
            raw_number = transfer_match.group(1)
            if raw_number:
                clean_number = "".join(filter(str.isdigit, raw_number))
                if len(clean_number) >= 7:
                    target_number = clean_number
                    print(f" [TRANSFER] Transferring to: {target_number} (Specific)", flush=True)
                else:
                    print(f" [TRANSFER] Invalid number: {raw_number}. Using default: {target_number}", flush=True)
            else:
                print(f" [TRANSFER] Transferring to default agent: {target_number}", flush=True)

            # Mark transfer in state for tracking
            call_state["transfer_requested"] = True
            await state.set_call_state(call_uuid, call_state)

            # Clean up response
            ai_response = re.sub(r'\[ACTION: TRANSFER(?::\s*\d+)?\]', '', ai_response).strip()

        # Build Response
        ncco = []

        # Add talk action
        ncco.append({"action": "talk", "text": ai_response, "bargeIn": False if should_transfer else True})

        if should_transfer:
            # Format phone numbers correctly
            formatted_target = target_number if target_number.startswith("+") else f"+{target_number}"
            formatted_from = call_state.get("bot_number", AGENT_NUMBER)

            print(f" [TRANSFER_EXECUTING] From: {formatted_from} → To: {formatted_target}")

            # Add connect action for transfer
            ncco.append({
                "action": "connect",
                "from": formatted_from,
                "endpoint": [{
                    "type": "phone",
                    "number": formatted_target
                }],
                "timeout": 60,  # Increased timeout
                "eventUrl": [f"{PUBLIC_URL}/voice/events"]
            })

            # Don't continue conversation after transfer
            print(f" [TRANSFER_COMPLETE] NCCO sent with connect action", flush=True)

        elif should_hangup:
            ncco.append({"action": "hangup"})
        else:
            # Continue conversation
            is_first_response = len(call_state.get("history", [])) <= 1
            silence_threshold = 5.0 if should_wait else (1.5 if is_first_response else 1.2)
            ncco.append({
                "action": "input",
                "type": ["speech"],
                "eventUrl": [f"{PUBLIC_URL}/voice/asr"],
                "speech": {
                    "language": "en-US",
                    "endOnSilence": silence_threshold
                }
            })

        print(f" [LATENCY] Total Turn: {time.time()-turn_start:.2f}s")
        print(f" [RESPONSE_NCCO] Sending: {ncco}", flush=True)
        call_state["processing"] = False
        await state.set_call_state(call_uuid, call_state)
        return JSONResponse(content=ncco)

    except Exception as e:
        print(f" Pipeline error: {e}", flush=True)
        traceback.print_exc()
        ai_response = "I encountered an error. Could you repeat that?"
        call_state["processing"] = False
        await state.set_call_state(call_uuid, call_state)
        return JSONResponse(content=[
            {"action": "talk", "text": ai_response, "bargeIn": True},
            {"action": "input", "type": ["speech"], "eventUrl": [f"{PUBLIC_URL}/voice/asr"], "speech": {"language": "en-US", "endOnSilence": 1.5}}
        ])

async def handle_voice_answer(state: ConversationState, request: Request):
    try:
        if request.method == "POST":
            data = await request.json()
        else:
            data = dict(request.query_params)
        
        call_uuid = data.get("uuid") or data.get("conversation_uuid") or "test"
        from_number = data.get("from") or "unknown"
        if isinstance(from_number, dict):
            from_number = from_number.get("number", "unknown")
        to_number = data.get("to") or "unknown"
        region_url = data.get("region_url")
        
        print(f" [TRACE] Extraction: Call ID: {call_uuid}, From: '{from_number}', To: '{to_number}', Region: {region_url}", flush=True)
        
        # Generic fallback
        greeting = "Hello. Welcome to Sandeza support. How can I help you today?"
        
        await state.set_call_state(call_uuid, {
            "history": [],
            "processing": False,
            "ticket_id": None,
            "phone": from_number,
            "bot_number": to_number,
            "contact_name": None,
            "recent_tickets": [],
            "lookup_done": False,
            "region_url": region_url
        })
        
        # Start background lookup task
        lookup_task = None
        if from_number != "unknown" and len(from_number) > 5:
            lookup_task = asyncio.create_task(background_freshdesk_lookup(state, call_uuid, from_number))
            
            # HYBRID LOGIC: Wait briefly (0.4s) for a fast lookup for snappiness
            # If lookup is slow, greeting proceeds anonymously and background sync handles the rest
            try:
                print(f" [HYBRID] Waiting 0.4s for name lookup...", flush=True)
                done, pending = await asyncio.wait([lookup_task], timeout=0.4)
                
                # Re-fetch state to see if name was set
                cs = await state.get_call_state(call_uuid)
                if cs:
                    raw_name = cs.get("contact_name", "")
                    # Only greet if name is a real name (not unknown, numeric, or a phone number)
                    # MUST contain at least one letter
                    is_valid_name = raw_name and re.search(r'[a-zA-Z]', str(raw_name)) and raw_name.lower() != "unknown"
                    
                    if is_valid_name:
                        greeting = f"Hello {raw_name}. Welcome back to Sandeza support. How can I help you today?"
                        print(f" [HYBRID] Personalization success: {raw_name}", flush=True)
                    else:
                        print(f" [HYBRID] Name is numeric or placeholder ({raw_name}). Skipping personalization.", flush=True)
                else:
                    print(f" [HYBRID] Timeout or no name found. Using generic.", flush=True)
            except Exception as e:
                print(f" [HYBRID] Wait error: {e}", flush=True)
        else:
            # Mark as done if no phone
            cs = await state.get_call_state(call_uuid)
            cs["lookup_done"] = True
            await state.set_call_state(call_uuid, cs)

        await state.append_history(call_uuid, "assistant", greeting)
        
        ws_url = PUBLIC_URL.replace("http", "ws") + "/voice/stream"
        print(f" [STREAM] Generating NCCO with Websocket URL: {ws_url}", flush=True)
        
        ncco = [
            {"action": "talk", "text": greeting, "bargeIn": False},
            {
                "action": "connect",
                "endpoint": [{
                    "type": "websocket",
                    "uri": ws_url,
                    "content-type": "audio/l16;rate=16000",
                    "headers": {
                        "uuid": call_uuid
                    }
                }]
            }
        ]
        print(f" [STREAM] NCCO: {ncco}", flush=True)
        return JSONResponse(content=ncco)
    except Exception as e:
        print(f" [ERROR] handle_voice_answer failed: {e}", flush=True)
        traceback.print_exc()
        return JSONResponse(content={"error": str(e)}, status_code=500)

async def handle_voice_events(state: ConversationState, request: Request):
    try:
        data = await request.json()
        status = data.get('status', data.get('type', 'unknown'))
        call_uuid = data.get("uuid") or data.get("conversation_uuid")
        
        print(f" EVENT: {status}")
        if status == "unknown" or "connection" in status:
            print(f" [DEBUG_EVENT] Full Payload: {data}", flush=True)
        
        if status == "completed" and call_uuid:
            call_state = await state.get_call_state(call_uuid)
            if call_state:
                call_state["status"] = "completed"
                await state.set_call_state(call_uuid, call_state)
                
                if call_state.get("ticket_id"):
                    ticket_id = call_state.get("ticket_id")
                    history = call_state.get("history", [])
                    
                    print(f" [EVENT] Call {call_uuid} completed. Syncing history to ticket {ticket_id}...")
                    # Run sync in background or wait briefly
                    asyncio.create_task(add_ticket_note(str(ticket_id), history))
                else:
                    print(f" [EVENT] Call {call_uuid} completed but no ticket_id found in state.")
            else:
                print(f" [EVENT] Call {call_uuid} completed but no state found.")
                
    except Exception as e:
        print(f" [EVENT] Error processing event: {e}")
    
    return JSONResponse(content=[])
