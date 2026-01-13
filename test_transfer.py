import asyncio
from app.voice import transfer_call_via_api

async def test():
    print("Testing Transfer Payload Generation...")
    await transfer_call_via_api("test-uuid-1234", "15550001234")

if __name__ == "__main__":
    asyncio.run(test())
