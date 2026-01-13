# Sandeza Freshdesk Voice Agent

An AI-powered voice bot integrated with Vonage and Freshdesk to automate L1 support. The agent identifies callers, respects existing ticket context, and handles real-time troubleshooting.

## 🚀 Key Features
- **Smart Identification**: Automatically look up Freshdesk contacts by phone number.
- **Ticket Deduplication**: Prioritizes existing tickets over creating new ones.
- **Contextual Resumption**: Resumes troubleshooting from the last step recorded in a ticket description.
- **Intelligent Noise Filtering**: Ignores background noise and wind while recognizing common confirmations.
- **Automatic History Sync**: Logs the full call transcript as a private note in Freshdesk upon completion.

## 🛠️ Conversation Flow

### 1. Inbound & Identification
- **Answer**: The agent greets the user (Personalized if name is found).
- **Lookup**: Simultaneously fetches the last 2 open tickets for the requester.
- **Silence**: Uses a 1.5s silence threshold on the first turn to ensure the user has time to respond.

### 2. Issue Analysis (The "Clarify-First" Logic)
- **Clarification**: If the user is vague, the bot asks for a description before checking tickets.
- **Matching**: Once an issue is stated, the bot compares it against `RECENT_TICKETS`.
- **Confirmation**: If a match is found, the bot **must** confirm before providing solutions.

### 3. Action Logic
- `[ACTION: CREATE_TICKET]`: Opens a new ticket if no match found.
- `[ACTION: USE_TICKET]`: Adopts an existing ticket ID into the session session.
- `[ACTION: RESOLVE_TICKET]`: Marks a ticket as resolved upon solution confirmation.
- `[ACTION: TRANSFER]`: Warm transfer to a human agent.
- `[ACTION: HANGUP]`: Ends call after a warm goodbye.

## 🏗️ Architecture
- **Web Framework**: FastAPI
- **Voice Provider**: Vonage Voice API (NCCO)
- **LLM**: Groq (Llama 3.3 70B + Whisper ASR/Transcription)
- **CRM/Helpdesk**: Freshdesk API
- **State Management**: Asynchronous in-memory `ConversationState`.

## ⚙️ Environment Variables
- `GROQ_API_KEY`: Groq Cloud API Key.
- `FRESH_DOMAIN`: Your Freshdesk domain.
- `FRESH_API_KEY`: Freshdesk API Key.
- `VONAGE_APP_ID`: Vonage Application ID.
- `VONAGE_PRIVATE_KEY_PATH`: Path to `private.key`.
- `PUBLIC_URL`: Public ngrok/deployment URL.
- `AGENT_NUMBER`: Fallback number for transfers.
