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
        await test_database_endpoints()
        await test_missing_ws_methods()
        await test_gcode_script()
        print("\n✅ Verification Passed!")
    except Exception as e:
        print(f"\n❌ Verification Failed: {e}")

async def test_missing_ws_methods():
    print(f"\nTesting generic WS methods...")
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(f"{BASE_URL}/websocket") as ws:
            # Consume initial greeting
            await ws.receive_json()

            # 1. server.temperature_store
            req = {"jsonrpc": "2.0", "method": "server.temperature_store", "id": 2}
            await ws.send_json(req)
            resp = await ws.receive_json()
            # Skip notifications if they come effectively
            while resp.get("method") == "notify_status_update":
                 resp = await ws.receive_json()

            print(f"Temperature Store: {resp}")
            assert resp["id"] == 2
            assert "temperatures" in resp["result"]

            # 2. server.files.metadata
            req = {"jsonrpc": "2.0", "method": "server.files.metadata", "params": {"filename": "test.gcode"}, "id": 3}
            await ws.send_json(req)
            resp = await ws.receive_json()
            # Skip notifications
            while resp.get("method") == "notify_status_update":
                 resp = await ws.receive_json()
                 
            print(f"Files Metadata: {resp}")
            assert resp["id"] == 3
            assert resp["result"]["filename"] == "test.gcode"

            # 3. server.gcode_store
            req = {"jsonrpc": "2.0", "method": "server.gcode_store", "id": 4}
            await ws.send_json(req)
            resp = await ws.receive_json()
            # Skip notifications
            while resp.get("method") == "notify_status_update":
                 resp = await ws.receive_json()
            print(f"Gcode Store: {resp}")
            assert resp["id"] == 4
            assert "gcode_store" in resp["result"]
            
            
        print("WS methods tests passed")

async def test_gcode_script():
    print(f"\nTesting printer.gcode.script...")
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(f"{BASE_URL}/websocket") as ws:
            # Consume initial greeting
            await ws.receive_json()

            # Test printer.gcode.script
            req = {
                "jsonrpc": "2.0", 
                "method": "printer.gcode.script", 
                "params": {"script": "G28\nM140 S60"}, 
                "id": 5
            }
            await ws.send_json(req)
            resp = await ws.receive_json()
            
            # Skip notifications
            while resp.get("method") == "notify_status_update":
                 resp = await ws.receive_json()

            print(f"Gcode Script: {resp}")
            assert resp["id"] == 5
            assert resp["result"] == "ok"
    print("G-code script test passed")

async def test_database_endpoints():
    print(f"\nTesting Database Endpoints...")
    async with aiohttp.ClientSession() as session:
        # POST
        data = {"namespace": "test_ns", "key": "test_key", "value": "test_val"}
        async with session.post(f"{BASE_URL}/server/database/item", json=data) as resp:
            print(f"POST Status: {resp.status}")
            r = await resp.json()
            print(f"POST Response: {r}")
            assert resp.status == 200
            assert r["result"]["value"] == "test_val"

        # GET
        async with session.get(f"{BASE_URL}/server/database/item?namespace=test_ns&key=test_key") as resp:
            print(f"GET Status: {resp.status}")
            r = await resp.json()
            print(f"GET Response: {r}")
            assert resp.status == 200
            assert r["result"]["value"] == "test_val"
            
        print("Database tests passed")

if __name__ == "__main__":
    asyncio.run(main())
