# Voice Agent Flow Chart

This document provides a visual flow chart of the Sandeza Voice Agent system along with a brief summary of its architecture and call flow.

---

## Architecture Flow Chart

![Voice Agent Flow Diagram](file:///C:/Users/KishorekumarS/.gemini/antigravity/brain/678cc842-ebdb-40d4-811f-f736eac1edb2/voice_agent_flow_diagram_1768215667405.png)

---

## Summary

- **Entry point:** `app/main.py` – FastAPI server handling `/voice/answer`, `/voice/asr`, `/voice/events`, and the WebSocket `/voice/stream`.
- **Core components:**
  - `DeepgramStreamer` – real‑time STT with ultra‑fast silence detection (threshold 0.3 s).
  - `ConversationState` – in‑memory per‑call state management.
  - `voice.py` – TTS injection, call control, JWT generation, and hang‑up logic with a duration‑based delay to ensure the farewell is spoken.
  - `groq.py` – prompt handling, LLM request to Groq, and action tag parsing.
  - `freshdesk.py` – CRM integration for contact lookup, ticket creation, updates, and notes.
- **External APIs:** Vonage Voice API, Deepgram STT API, Groq LLM API, Freshdesk REST API.
- **Async pattern:** FastAPI async endpoints, background `asyncio.create_task` for CRM actions, WebSocket loop with callbacks for speech‑ended and barge‑in events, and timed hang‑up after TTS.
- **Data flow:** Audio PCM → **Deepgram** (STT) → transcript → **Groq** (LLM) → response text → **Vonage** (TTS) → optional actions (ticket resolve, transfer, hang‑up) → **Freshdesk** (CRM).

The diagram follows a 4×3 grid layout with swimlanes (Frontend/UI, Backend Core, External APIs, Data Flow) and highlights key decision points such as confirmation before ticket resolution and the silence‑timeout detection.
