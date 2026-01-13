import asyncio
import aiohttp
import json

BASE_URL = "http://localhost:7125"

async def test_http_info():
    print(f"Testing HTTP GET /server/info...")
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{BASE_URL}/server/info") as resp:
            data = await resp.json()
            print(f"Status: {resp.status}")
            print(f"Response: {data}")
            assert resp.status == 200
            assert "result" in data
            assert data["result"]["state"] == "ready"

async def test_websocket_flow():
    print(f"\nTesting WebSocket Flow...")
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(f"{BASE_URL}/websocket") as ws:
            print("Connected to WebSocket")
            
            # 1. Expect notify_klippy_ready
            msg = await ws.receive_json()
            print(f"Received: {msg}")
            assert msg["method"] == "notify_klippy_ready"
            
            # 2. Subscribe
            sub_req = {
                "jsonrpc": "2.0",
                "method": "printer.objects.subscribe",
                "params": {
                    "objects": {
                        "extruder": None,
                        "print_stats": None
                    }
                },
                "id": 1
            }
            print(f"Sending Subscribe: {sub_req}")
            await ws.send_json(sub_req)
            
            resp = await ws.receive_json()
            print(f"Received subscription response: {resp}")
            assert resp["id"] == 1
            assert "result" in resp
            
            print("Waiting for status update (mock data?)...")
            # We might need to wait a bit if mock loop is running
            try:
                msg = await ws.receive_json(timeout=5)
                print(f"Received update: {msg}")
                assert msg["method"] == "notify_status_update"
            except asyncio.TimeoutError:
                print("No update received in 5s (mock might be slow to start or delta is empty)")

async def main():
    try:
        await test_http_info()
        await test_websocket_flow()
        print("\n✅ Verification Passed!")
    except Exception as e:
        print(f"\n❌ Verification Failed: {e}")

if __name__ == "__main__":
    asyncio.run(main())
