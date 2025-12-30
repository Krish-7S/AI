from fastapi import Request
from fastapi.responses import JSONResponse
import os
import httpx
import base64
import re
import asyncio
import traceback
from typing import List, Dict, Any
from dotenv import load_dotenv

load_dotenv()

PUBLIC_URL = "https://carpogonial-candace-nonjuridically.ngrok-free.dev"

async def fetch_combined_knowledge(query: str) -> str:
    if not query.strip():
        return ""
    
    words = re.sub(r'[^\w\s]', '', query.lower()).split()
    words = [w for w in words if len(w) > 2][:6]
    search_term = ' '.join(words)
    
    print(f"SEARCHING: '{search_term}'")
    
    kb_snippets = []
    
    async def fetch_solutions():
        try:
            async with httpx.AsyncClient(timeout=4.0) as client:
                resp = await client.get(
                    "https://sandezainc.freshdesk.com/support/search/solutions.json",
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
            # We use the direct search URL for the 'Archives - Freshdesk' category
            url = f"https://community.freshworks.com/search?category=Using+Freshdesk+%3E+Archives+-+Freshdesk&q={search_term.replace(' ', '+')}"
            async with httpx.AsyncClient(timeout=6.0) as client:
                resp = await client.get(url)
                html = resp.text
                # Extremely primitive parsing to find result blocks
                # In a real app we'd use BeautifulSoup, but we try to extract snippets here
                # We look for result container patterns discovered in the browser
                results = re.findall(r'<a[^>]*class="[^"]*forum-search-result__title[^"]*"[^>]*>(.*?)</a>.*?<div[^>]*class="[^"]*forum-search-result__content[^"]*"[^>]*>(.*?)</div>', html, re.DOTALL)
                for title, content in results[:2]:
                    title_clean = re.sub(r'<[^>]*>', '', title).strip()
                    content_clean = re.sub(r'<[^>]*>', ' ', content).strip()
                    kb_snippets.append(f"COMMUNITY ARCHIVE: {title_clean}\nSOLUTION: {content_clean[:800]}")
        except Exception as e:
            print(f"Community search failed: {e}")

    # Run in parallel
    await asyncio.gather(fetch_solutions(), fetch_community_archives())
    
    combined_text = "\n\n".join(kb_snippets)
    print(f"CONTEXT READY: {len(kb_snippets)} total sources found.")
    return combined_text

async def create_ticket(call_id: str, issue: str, phone: str = None) -> str:
    ticket_id = f"TICKET-{call_id[:8].upper()}"
    print(f" Ticket: {ticket_id}")
    return ticket_id

from .groq import agent_response, transcribe_whisper
from .state import ConversationState

async def handle_voice_asr(state: ConversationState, groq_agent, freshdesk, request: Request):
    data = await request.json()
    call_uuid = data.get("uuid")
    
    print(f" ASR: {call_uuid}")
    
    call_state = await state.get_call_state(call_uuid)
    if call_state.get("processing"):
        print(" Processing...")
        return JSONResponse(content=[])
    
    call_state["processing"] = True
    await state.set_call_state(call_uuid, call_state)
    
    # 1. Parse Speech
    speech_text = ""
    try:
        # FAST PATH: Check for direct audio (PCM-based)
        audio_data = data.get("audio")
        if audio_data:
            speech_text = await transcribe_whisper(audio_data)
            
        # SLOW PATH: Fallback to Vonage built-in ASR
        if not speech_text:
            speech_data = data.get("speech", {})
            results = speech_data.get("results", [])
            if results and results[0] and results[0].get("text"):
                speech_text = str(results[0].get("text")).strip()
    except Exception as e:
        print(f" Speech parse: {e}")
    
    print(f" USER: '{speech_text}'")
    
    # 2. Handle 0-content speech
    if not speech_text or len(speech_text) < 2:
        call_state["processing"] = False
        await state.set_call_state(call_uuid, call_state)
        return JSONResponse(content=[
            {"action": "talk", "text": "I didn't catch that. Could you repeat?", "bargeIn": True},
            {"action": "input", "type": ["speech"], "eventUrl": [f"{PUBLIC_URL}/voice/asr"], "speech": {"language": "en-US", "endOnSilence": 1.2}}
        ])
    
    # 3. Synchronous Pipeline (KB + Groq)
    try:
        await state.append_history(call_uuid, "user", speech_text)
        
        # Always search both KB and Community
        kb = await fetch_combined_knowledge(speech_text)
        
        # Call Groq
        ai_response = await agent_response(speech_text, kb, call_state["history"], max_tokens=1000, temperature=0.4)
        
        await state.append_history(call_uuid, "assistant", ai_response)
        
        # Quick ticket mention (Log only for speed)
        await create_ticket(call_uuid, speech_text, call_state.get("phone"))
        
        print(f" PIPELINE READY: {ai_response[:40]}...")
        
    except Exception as e:
        print(f" Pipeline error: {e}")
        tb = traceback.format_exc()
        print(tb)
        ai_response = "I encountered an error. Could you repeat that?"

    call_state["processing"] = False
    await state.set_call_state(call_uuid, call_state)
    
    return JSONResponse(content=[
        {"action": "talk", "text": ai_response, "bargeIn": True},
        {"action": "input", "type": ["speech"], "eventUrl": [f"{PUBLIC_URL}/voice/asr"], "speech": {"language": "en-US", "endOnSilence": 1.5}}
    ])

async def handle_voice_answer(state: ConversationState, request: Request):
    data = await request.json() if request.method == "POST" else {}
    call_uuid = data.get("uuid") or data.get("conversation_uuid", "test")
    from_number = data.get("from", {}).get("number", "unknown")
    
    print(f" CALL: {call_uuid} from {from_number}")
    
    await state.set_call_state(call_uuid, {
        "history": [],
        "processing": False,
        "ticket_id": None,
        "phone": from_number
    })
    
    return JSONResponse(content=[
        {"action": "talk", "text": "Sandeza support. How can I help?", "bargeIn": True},
        {"action": "input", "type": ["speech"], "eventUrl": [f"{PUBLIC_URL}/voice/asr"], "speech": {"language": "en-US", "endOnSilence": 1.2}}
    ])

async def handle_voice_events(state: ConversationState, request: Request):
    try:
        data = await request.json()
        print(f" EVENT: {data.get('status', data.get('type', 'unknown'))}")
    except:
        pass
    return JSONResponse(content=[])
