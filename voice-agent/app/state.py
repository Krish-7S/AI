from typing import Dict, Any, List
from datetime import datetime, timedelta

class ConversationState:
    def __init__(self):
        self.calls: Dict[str, Dict[str, Any]] = {}
    
    async def get_call_state(self, call_uuid: str) -> Dict[str, Any]:
        if call_uuid not in self.calls:
            self.calls[call_uuid] = {
                "history": [],
                "processing": False,
                "ticket_id": None,
                "phone": ""
            }
        return self.calls[call_uuid]
    
    async def set_call_state(self, call_uuid: str, state: Dict[str, Any]):
        self.calls[call_uuid] = state
    
    async def append_history(self, call_uuid: str, role: str, content: str):
        state = await self.get_call_state(call_uuid)  # ✅ FIXED: get state first
        state["history"].append({"role": role, "content": content})
        state["history"] = state["history"][-12:]
        await self.set_call_state(call_uuid, state)
