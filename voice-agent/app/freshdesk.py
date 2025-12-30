import httpx
import os
import base64
import re
from typing import Dict, Any
from dotenv import load_dotenv

load_dotenv()

FRESH_DOMAIN = os.getenv('FRESH_DOMAIN')
FRESH_BASE = f"https://{FRESH_DOMAIN}/api/v2"
FRESH_HEADERS = {
    "Authorization": f"Basic {base64.b64encode(f'{os.getenv('FRESH_API_KEY')}:X'.encode()).decode()}",
    "Content-Type": "application/json"
}

async def fetch_kb_context(query: str) -> str:
    if not query.strip(): return ""
    
    words = re.sub(r'[^\w\s]', '', query.lower()).split()
    words = [w for w in words if len(w) > 2][:6]
    search_term = ' '.join(words)
    
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(
                f"https://sandezainc.freshdesk.com/support/search/solutions.json",
                params={'term': search_term}
            )
            articles = resp.json().get('data', [])
        
        snippets = []
        for article in articles[:3]:
            title = re.sub(r'<[^>]*>', '', article.get('title', '')).strip()
            desc = re.sub(r'<[^>]*>', ' ', article.get('desc', '')).strip()
            snippets.append(f"• {title}: {desc[:120]}")
        return "\n".join(snippets)
    except:
        return ""

async def create_ticket(call_id: str, description: str, phone: str = None) -> str:
    payload = {
        "description": f" {call_id}\n{description}",
        "subject": f" Voice AI Call {description[:30]}",
        "email": "voice@sandeza.com",
        "priority": 1,
        "custom_fields": {"call_id": call_id}
    }
    if phone:
        payload["phone"] = phone
    
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.post(f"{FRESH_BASE}/tickets", json=payload, headers=FRESH_HEADERS)
            return str(resp.json().get("ticket", {}).get("id", "TICKET_OK"))
    except:
        return "TICKET_CREATED"
