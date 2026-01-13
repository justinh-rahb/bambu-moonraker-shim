import asyncio
import json
import time
import uuid
from typing import Dict, Any, List, Optional
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Request, UploadFile, File
from fastapi.responses import JSONResponse
from state_manager import state_manager
from bambu_client import bambu_client
from config import Config
from database_manager import database_manager

router = APIRouter()

# --- HTTP Helpers ---
def success_response(data: Any) -> Dict[str, Any]:
    return {"result": data}

def error_response(code: int, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=code,
        content={"error": {"code": code, "message": message}}
    )

# --- HTTP Endpoints ---

@router.get("/server/info")
async def server_info():
    return success_response({
        "state": "ready",
        "klippy_state": "ready",
        "components": ["printer", "websocket"],
        "version": "v0.0.1-bambu-shim",
        "api_version": [1, 0, 0]
    })

@router.get("/printer/info")
async def printer_info():
    return success_response({
        "state": "ready",
        "hostname": "bambu-shim",
        "model": "Bambu",
        "firmware_version": "unknown",
        "software_version": "bambu-moonraker-shim"
    })

@router.get("/printer/objects/list")
async def objects_list():
    # Return keys from our observable state
    keys = list(state_manager.get_state().keys())
    return success_response({"objects": keys})

@router.get("/printer/objects/query")
async def objects_query(request: Request):
    """
    Mainsail passes query like ?objects:json={"extruder":null, ...}
    FastAPI doesn't automatically parse the weird :json param key well,
    so we parse query_params manually.
    """
    objects_param = None
    for key, value in request.query_params.items():
        if key == "objects" or key == "objects:json":
             try:
                 objects_param = json.loads(value)
             except:
                 pass
             break
    
    if not objects_param:
        # Fallback if just ?objects=extruder,heater_bed (less common but possible)
        # But Mainsail strictly uses JSON object map
        return success_response({"status": {}, "eventtime": time.time()})

    result_status = {}
    current_state = state_manager.get_state()
    
    for obj_name, fields in objects_param.items():
        if obj_name in current_state:
            result_status[obj_name] = current_state[obj_name]
    
    return success_response({
        "status": result_status,
        "eventtime": time.time()
    })

@router.get("/server/files/list")
async def file_list(root: str = "gcodes"):
    if root == "config":
         return success_response([])

    # Mock file list for MVP
    # In real impl, we'd list local files or query printer
    files = [
        {"path": "gcodes/benchy.gcode", "size": 123456, "modified": time.time()},
        {"path": "gcodes/calibration.gcode", "size": 65432, "modified": time.time()}
    ]
    return success_response(files)

@router.post("/server/files/upload")
async def file_upload(file: UploadFile = File(...), path: str = None):
    # Dummy upload
    return success_response({
        "item": { "path": f"gcodes/{file.filename}", "size": 0 },
        "print_started": False
    })

@router.get("/server/files/{root}/{path:path}")
async def file_download(root: str, path: str):
    # Mocking theme files to avoid 404s for Mainsail
    if root == "config" and ".theme" in path:
        # Return empty JSON for theme files to satisfy Mainsail
        return success_response({})
    
    # Generic 404 for now unless checked against real files
    return error_response(404, "File not found")

@router.get("/server/database/item")
async def database_get(namespace: str, key: str = None):
    # namespace is required, key is optional query param
    val = database_manager.get_item(namespace, key)
    return success_response({"namespace": namespace, "key": key, "value": val})

@router.post("/server/database/item")
async def database_post(request: Request):
    try:
        body = await request.json()
        namespace = body.get("namespace")
        key = body.get("key")
        value = body.get("value")
        
        if not namespace:
            return error_response(400, "Namespace required")

        new_val = database_manager.post_item(namespace, key, value)
        return success_response({"namespace": namespace, "key": key, "value": new_val})
    except Exception as e:
        return error_response(400, str(e))

@router.delete("/server/database/item")
async def database_delete(request: Request):
     # Delete usually comes as query params or body? Moonraker docs say DELETE method.
     # FastAPI handles method. Query params likely.
     namespace = request.query_params.get("namespace")
     key = request.query_params.get("key")
     if not namespace or not key:
         return error_response(400, "Namespace and key required")
     
     val = database_manager.delete_item(namespace, key)
     return success_response({"namespace": namespace, "key": key, "value": val})

@router.post("/printer/print/start")
async def print_start(request: Request):
    try:
        body = await request.json()
        filename = body.get("filename")
        # TODO: Implement start print logic in BambuClient
        print(f"Requested start print: {filename}")
        # await bambu_client.start_print(filename)
        return success_response("ok")
    except Exception as e:
        return error_response(400, str(e))

@router.post("/printer/print/pause")
async def print_pause():
    await bambu_client.pause_print()
    return success_response("ok")

@router.post("/printer/print/resume")
async def print_resume():
    await bambu_client.resume_print()
    return success_response("ok")

@router.post("/printer/print/cancel")
async def print_cancel():
    await bambu_client.cancel_print()
    return success_response("ok")


# --- WebSocket / JSON-RPC ---

class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        # Notify readiness immediately
        await websocket.send_json({
            "jsonrpc": "2.0",
            "method": "notify_klippy_ready",
            "params": []
        })

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        # Broadcast to all connected clients
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except:
                pass # Handle broken pipe?

manager = ConnectionManager()

# Hook state manager to broadcast
async def broadcast_state_update(notification: dict):
    await manager.broadcast(notification)

state_manager.set_broadcast_callback(broadcast_state_update)


@router.websocket("/websocket")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_json()
            # Process JSON-RPC request
            response = await handle_jsonrpc(data)
            if response:
                await websocket.send_json(response)
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        print(f"WS Error: {e}")
        manager.disconnect(websocket)

async def handle_jsonrpc(request: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = request.get("id")
    method = request.get("method")
    
    response = {
        "jsonrpc": "2.0",
        "id": req_id
    }
    
    if method == "server.info":
        response["result"] = {
            "klippy_state": "ready",
            "version": "v0.0.1-bambu-shim"
        }
    elif method == "printer.objects.list":
        keys = list(state_manager.get_state().keys())
        response["result"] = {"objects": keys}
    elif method == "printer.objects.query":
        # Similar logic to HTTP query
        # For simplicity, returning all requested keys
        params = request.get("params", {}).get("objects", {})
        result_status = {}
        current_state = state_manager.get_state()
        for key in params.keys():
            if key in current_state:
                result_status[key] = current_state[key]
        response["result"] = {"status": result_status, "eventtime": time.time()}
        
    elif method == "printer.objects.subscribe":
        # In this simplified shim, we treat subscribe same as query + it enables updates (globally)
        # Improvements: track per-client subscriptions filtering
        params = request.get("params", {}).get("objects", {})
        result_status = {}
        current_state = state_manager.get_state()
        for key in params.keys():
            if key in current_state:
                result_status[key] = current_state[key]
        response["result"] = {"status": result_status, "eventtime": time.time()}
        
        response["result"] = {"status": result_status, "eventtime": time.time()}

    elif method == "server.database.get_item":
        namespace = request.get("params", {}).get("namespace")
        key = request.get("params", {}).get("key")
        val = database_manager.get_item(namespace, key)
        response["result"] = {"namespace": namespace, "key": key, "value": val}

    elif method == "server.database.post_item":
        namespace = request.get("params", {}).get("namespace")
        key = request.get("params", {}).get("key")
        value = request.get("params", {}).get("value")
        new_val = database_manager.post_item(namespace, key, value)
        response["result"] = {"namespace": namespace, "key": key, "value": new_val}

    elif method == "server.database.delete_item":
        namespace = request.get("params", {}).get("namespace")
        key = request.get("params", {}).get("key")
        val = database_manager.delete_item(namespace, key)
        response["result"] = {"namespace": namespace, "key": key, "value": val}

    elif method == "server.temperature_store":
        # Mock temperature store
        response["result"] = {
            "keys": ["extruder", "heater_bed"],
            "min": 0,
            "max": 300,
            "temperatures": {
                "extruder": [],
                "heater_bed": []
            } # Empty history for now
        }

    elif method == "server.files.metadata":
        filename = request.get("params", {}).get("filename")
        # Mock metadata
        response["result"] = {
            "filename": filename,
            "size": 1234,
            "modified": time.time(),
            "slicer": "BambuStudio",
            "slicer_version": "unknown",
            "layer_height": 0.2,
            "first_layer_height": 0.2,
            "object_height": 10.0,
            "filament_total": 1000.0,
            "estimated_time": 3600,
            "thumbnails": []
        }
        
    elif method == "printer.info":
        response["result"] = {
            "state": "ready",
            "hostname": "bambu-shim",
            "model": "Bambu",
            "firmware_version": "unknown",
            "software_version": "bambu-moonraker-shim",
            "cpu_info": "Mock CPU"
        }

    elif method == "server.connection.identify":
        response["result"] = {
            "connection_id": req_id
        }

    elif method == "server.gcode_store":
        response["result"] = {"gcode_store": []}
        
    elif method == "server.webcams.list":
        response["result"] = {"webcams": []}

    elif method == "server.config":
        response["result"] = {"config": {}}

    elif method == "machine.system_info":
         response["result"] = {
            "system_info": {
                "cpu": "unknown",
                "python": "3.x",
                "os": "mock_os"
            }
        }
    
    elif method == "machine.proc_stats":
        response["result"] = {
            "system_cpu_usage": {"cpu": 0.0},
            "system_memory": {"available": 1000, "total": 2000, "used": 1000},
            "websocket_connections": 1
        }
        
    elif method == "server.database.list":
         # Return empty list of namespaces or similar
         response["result"] = {"namespaces": []}

    else:
        # Ignore unknown methods or return null result to avoid errors
        # Mainsail calls a lot of things we might not implement yet.
        print(f"Unknown WS method: {method}")
        response["result"] = {} # Safe fallback

    return response
