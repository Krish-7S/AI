import os
from fastapi import FastAPI, Request
from dotenv import load_dotenv

load_dotenv()

from .state import ConversationState
from .voice import handle_voice_answer, handle_voice_asr, handle_voice_events

state = ConversationState()
app = FastAPI()

@app.get("/")
async def health():
    return {"status": " Sandeza Voice AI + REAL KB + Groq", "calls": len(state.calls)}

@app.get("/voice/answer")
@app.post("/voice/answer")
async def voice_answer(request: Request):
    return await handle_voice_answer(state, request)

@app.post("/voice/asr")
async def voice_asr(request: Request):
    #  Pass None - functions handle everything internally
    return await handle_voice_asr(state, None, None, request)

@app.post("/voice/events")
async def voice_events(request: Request):
    return await handle_voice_events(state, request)

if __name__ == "__main__":
    import uvicorn
    print(" SANDEZA VOICE AI + REAL KB + GROQ ")
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
