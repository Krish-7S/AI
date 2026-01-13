import httpx
import os
import base64
import re
from typing import Dict, Any, List
from dotenv import load_dotenv

# Look for .env in current dir and /app subdir
load_dotenv()
load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

FRESH_DOMAIN = os.getenv('FRESH_DOMAIN')
FRESH_API_KEY = os.getenv('FRESH_API_KEY')
FRESH_BASE = f"https://{FRESH_DOMAIN}/api/v2" if FRESH_DOMAIN else ""

# Safety: Don't crash at import time if API KEY is missing
FRESH_HEADERS = {}
if FRESH_API_KEY:
    auth_str = f"{FRESH_API_KEY}:X"
    encoded_auth = base64.b64encode(auth_str.encode()).decode()
    FRESH_HEADERS = {
        "Authorization": f"Basic {encoded_auth}",
        "Content-Type": "application/json"
    }
else:
    print(" [WARNING] FRESH_API_KEY not found in environment. Freshdesk integration will fail.", flush=True)

async def search_contact_by_phone(phone: str) -> Dict[str, Any]:
    """Search for a contact in Freshdesk using multiple phone number strategies."""
    if not phone:
        return {}
    
    # 1. CLEAN PHONES
    full_phone = re.sub(r'\D', '', phone)
    ten_digit = full_phone[-10:] if len(full_phone) >= 10 else full_phone
    
    print(f" [SEARCH] Identifying contact: {phone} (10D: {ten_digit}, Full: {full_phone})", flush=True)
    
   
    query = f"(phone:'{ten_digit}' OR mobile:'{ten_digit}' OR phone:'{full_phone}' OR mobile:'{full_phone}' OR phone:'+{full_phone}')"
    # Note: Search API results can take a few minutes to index, but it's the requested robust method.
    
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            url = f"{FRESH_BASE}/search/contacts"
            print(f" [SEARCH] GET {url} query={query}", flush=True)
            
            resp = await client.get(
                url,
                params={"query": f'"{query}"'},
                headers=FRESH_HEADERS
            )
            
            print(f" [SEARCH] Status: {resp.status_code}", flush=True)
            
            if resp.status_code == 200:
                data = resp.json()
                results = data.get("results", [])
                if results and isinstance(results, list):
                    contact = results[0]
                    print(f" Found contact: {contact.get('name')} (ID: {contact.get('id')})", flush=True)
                    return contact
                else:
                    print(f" [SEARCH] No contact matched", flush=True)
            else:
                print(f" [SEARCH] Error: {resp.text[:100]}", flush=True)

            return {}
            
    except Exception as e:
        print(f" Contact Search Error: {e}", flush=True)
        return {}

async def update_contact_name(contact_id: int, new_name: str) -> bool:
    """Update a contact's name in Freshdesk."""
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            url = f"{FRESH_BASE}/contacts/{contact_id}"
            resp = await client.put(url, json={"name": new_name}, headers=FRESH_HEADERS)
            if resp.status_code in [200, 201]:
                print(f" Contact {contact_id} renamed to {new_name}", flush=True)
                return True
            else:
                print(f" Contact rename failed: {resp.status_code} - {resp.text}", flush=True)
                return False
    except Exception as e:
        print(f" Contact Rename Error: {e}", flush=True)
        return False

async def create_contact(name: str, phone: str) -> Dict[str, Any]:
    """Create a new contact in Freshdesk with name and phone."""
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            url = f"{FRESH_BASE}/contacts"
            payload = {
                "name": name,
                "phone": phone
            }
            resp = await client.post(url, json=payload, headers=FRESH_HEADERS)
            if resp.status_code in [200, 201]:
                contact = resp.json()
                print(f" [CONTACT] Created new contact: {name} (ID: {contact.get('id')})", flush=True)
                return contact
            else:
                print(f" [CONTACT] Create failed: {resp.status_code} - {resp.text}", flush=True)
                return {}
    except Exception as e:
        print(f" [CONTACT] Create error: {e}", flush=True)
        return {}
        
async def get_latest_tickets(contact_id: int) -> List[Dict[str, Any]]:
    """Fetch the last 2 open/pending tickets for the contact."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            # Use standard List Tickets API
            url = f"{FRESH_BASE}/tickets"
            params = {
                "requester_id": contact_id,
                "include": "description",
                "order_by": "created_at",
                "order_type": "desc"
            }
            
            print(f" [TICKETS] Fetching for requester={contact_id}", flush=True)
            resp = await client.get(url, params=params, headers=FRESH_HEADERS)
            print(f" [TICKETS] Status: {resp.status_code}", flush=True)
            
            if resp.status_code == 200:
                all_tickets = resp.json()
                if isinstance(all_tickets, list):
                    # Filter for Open (2) and Pending (3) in Python
                    tickets = [t for t in all_tickets if t.get("status") in [2, 3]]
                    print(f" Found {len(tickets)} open/pending from {len(all_tickets)} total.", flush=True)
                    return tickets[:2]
                else:
                    print(f" [TICKETS] Response not a list: {type(all_tickets)}", flush=True)
            else:
                print(f" [TICKETS] List error: {resp.text[:100]}", flush=True)
            
            return []
    except Exception as e:
        print(f" Ticket Fetch Error: {e}", flush=True)
        return []

async def fetch_kb_context(query: str) -> str:
    if not query.strip(): return ""
    
    words = re.sub(r'[^\w\s]', '', query.lower()).split()
    words = [w for w in words if len(w) > 2][:6]
    search_term = ' '.join(words)
    
    try:
        # Strict 1.5s timeout for voice latency
        async with httpx.AsyncClient(timeout=1.5) as client:
            resp = await client.get(
                f"https://{FRESH_DOMAIN}/support/search/solutions.json",
                params={'term': search_term}
            )
            if resp.status_code != 200:
                return ""
            articles = resp.json().get('data', [])
        
        snippets = []
        for article in articles[:3]:
            title = re.sub(r'<[^>]*>', '', article.get('title', '')).strip()
            desc = re.sub(r'<[^>]*>', ' ', article.get('desc', '')).strip()
            snippets.append(f"• {title}: {desc[:120]}")
        return "\n".join(snippets)
    except:
        return ""

async def create_ticket(call_id: str, description: str, phone: str = None, sentiment: str = "Neutral", requester_id: int = None) -> str:
    tags = ["Arta_Ai"]
    if sentiment:
        tags.append(f"Sentiment_{sentiment}")
        
    payload = {
        "description": f"Call ID: {call_id}\n\nLast issue: {description}",
        "subject": f"Voice AI Call Support - {description[:30]}...",
        "priority": 1,
        "status": 2, # Open
        "source": 3, # Phone
        "tags": tags
    }
    if requester_id:
        payload["requester_id"] = requester_id
    elif phone:
        payload["phone"] = phone
    
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.post(f"{FRESH_BASE}/tickets", json=payload, headers=FRESH_HEADERS)
            if resp.status_code not in [200, 201]:
                print(f" [TICKET] Create failed: {resp.status_code} - {resp.text}", flush=True)
                return None
                
            data = resp.json()
            # If standard response format
            ticket_id = data.get("id") or data.get("ticket", {}).get("id")
            if ticket_id:
                print(f" Created New Ticket: {ticket_id}", flush=True)
                return str(ticket_id)
            return None
    except Exception as e:
        print(f" Ticket Creation Error: {e}", flush=True)
        return None

async def update_ticket_status(ticket_id: int, status: int = 4) -> bool:
    """Update a ticket status (default 4=Resolved, 5=Closed)."""
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            url = f"{FRESH_BASE}/tickets/{ticket_id}"
            resp = await client.put(url, json={"status": status}, headers=FRESH_HEADERS)
            if resp.status_code in [200, 201]:
                print(f" Ticket {ticket_id} status updated to {status}", flush=True)
                return True
            else:
                print(f" Ticket update failed: {resp.status_code} - {resp.text}", flush=True)
                return False
    except Exception as e:
        print(f" Ticket Update Error: {e}", flush=True)
        return False

async def add_ticket_note(ticket_id: str, history: List[Dict]) -> bool:
    """Add the full conversation history as a private note to the ticket."""
    if not ticket_id or not history:
        return False
    
    # Format the history into a clean HTML transcript
    transcript = "<h3>Call Transcript (Arta AI)</h3><div style='font-family: sans-serif; line-height: 1.6;'>"
    
    for turn in history:
        role_label = turn.get("role", "user").capitalize()
        content = turn.get("content", "")
        
        # 1. Clean content (Strip all [ACTION: ...] tags for the agent)
        clean_content = re.sub(r'\[ACTION:[^\]]+\]', '', content).strip()
        
        # 2. Add to HTML with color-coded blocks
        if role_label.lower() == "user":
            transcript += f"<p><b>User:</b> <span style='color: #2c3e50;'>{clean_content}</span></p>"
        else:
            transcript += f"<p><b>Assistant:</b> <span style='color: #2980b9;'>{clean_content}</span></p>"
            
    transcript += "</div><hr><p><small><i>Generated by Sandeza Support AI</i></small></p>"
    
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            url = f"{FRESH_BASE}/tickets/{ticket_id}/notes"
            payload = {
                "body": transcript,
                "private": True
            }
            resp = await client.post(url, json=payload, headers=FRESH_HEADERS)
            if resp.status_code in [200, 201, 202]:
                print(f" [HISTORY] Conversation synced to ticket {ticket_id}", flush=True)
                return True
            else:
                print(f" [HISTORY] Sync failed for ticket {ticket_id} (Status {resp.status_code}): {resp.text[:200]}", flush=True)
                return False
    except Exception as e:
        print(f" [HISTORY] Sync error: {e}", flush=True)
        return False
