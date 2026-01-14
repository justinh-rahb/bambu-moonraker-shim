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
        await test_configfile()
        await test_webcams()
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
            assert "extruder" in resp["result"]
            assert "temperatures" in resp["result"]["extruder"]

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

async def test_configfile():
    print(f"\nTesting configfile...")
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(f"{BASE_URL}/websocket") as ws:
            # Consume initial greeting
            await ws.receive_json()

            # Query configfile
            req = {
                "jsonrpc": "2.0", 
                "method": "printer.objects.query", 
                "params": {"objects": {"configfile": None}}, 
                "id": 6
            }
            await ws.send_json(req)
            resp = await ws.receive_json()
            
            # Skip notifications
            while resp.get("method") == "notify_status_update":
                 resp = await ws.receive_json()

            print(f"Configfile: {resp}")
            assert resp["id"] == 6
            assert "configfile" in resp["result"]["status"]
            assert "settings" in resp["result"]["status"]["configfile"]
            assert "extruder" in resp["result"]["status"]["configfile"]["settings"]
            assert "virtual_sdcard" in resp["result"]["status"]["configfile"]["settings"]

    print("Configfile test passed")

async def test_webcams():
    print(f"\nTesting webcams...")
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(f"{BASE_URL}/websocket") as ws:
            await ws.receive_json()

            # 1. Add Webcam
            req = {
                "jsonrpc": "2.0", 
                "method": "server.webcams.post_item", 
                "params": {"name": "Test Cam", "stream_url": "http://test/stream"}, 
                "id": 7
            }
            await ws.send_json(req)
            resp = await ws.receive_json()
            # Expect response to ID 7
            while resp.get("id") != 7:
                 print(f"Skipping notification: {resp.get('method')}")
                 resp = await ws.receive_json()
            
            print(f"Add Webcam: {resp}")
            
            assert resp["id"] == 7
            assert resp["result"]["item"]["name"] == "Test Cam"
            assert resp["result"]["item"]["stream_url"] == "http://test/stream"
            uid = resp["result"]["item"]["uid"]

            # 2. Update Webcam
            req = {
                "jsonrpc": "2.0", 
                "method": "server.webcams.post_item", 
                "params": {"uid": uid, "name": "Updated Cam", "stream_url": "http://test/stream_v2"}, 
                "id": 71
            }
            await ws.send_json(req)
            resp = await ws.receive_json()
            while resp.get("id") != 71: resp = await ws.receive_json()
            
            print(f"Update Webcam: {resp}")
            assert resp["result"]["item"]["name"] == "Updated Cam"
            assert resp["result"]["item"]["stream_url"] == "http://test/stream_v2"
            assert resp["result"]["item"]["uid"] == uid

            # 3. List Webcams
            req = {"jsonrpc": "2.0", "method": "server.webcams.list", "id": 8}
            await ws.send_json(req)
            resp = await ws.receive_json()
            while resp.get("id") != 8: resp = await ws.receive_json()

            print(f"List Webcams: {resp}")
            assert len(resp["result"]["webcams"]) >= 1
            # Check for updated values
            cam = next((c for c in resp["result"]["webcams"] if c["uid"] == uid), None)
            assert cam is not None
            assert cam["name"] == "Updated Cam"

            # 4. Delete Webcam
            req = {"jsonrpc": "2.0", "method": "server.webcams.delete_item", "params": {"uid": uid}, "id": 9}
            await ws.send_json(req)
            resp = await ws.receive_json()
            while resp.get("id") != 9: resp = await ws.receive_json()
            
            print(f"Delete Webcam: {resp}")
            assert resp["result"]["item"]["uid"] == uid

            # 5. Verify Deletion
            req = {"jsonrpc": "2.0", "method": "server.webcams.list", "id": 10}
            await ws.send_json(req)
            resp = await ws.receive_json()
            while resp.get("id") != 10: resp = await ws.receive_json()
            
            # Should not contain the deleted uid
            assert not any(c["uid"] == uid for c in resp["result"]["webcams"])

    print("Webcam test passed")

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
            
        # Database List
        async with session.get(f"{BASE_URL}/server/database/list") as resp:
            print(f"GET /server/database/list Status: {resp.status}")
            r = await resp.json()
            assert resp.status == 200
            assert "namespaces" in r["result"]
            assert "backups" in r["result"]
            print(f"Namespaces: {r['result']['namespaces']}")
            assert len(r["result"]["namespaces"]) > 0
            assert isinstance(r["result"]["backups"], list)

        print("Database tests passed")

if __name__ == "__main__":
    asyncio.run(main())
