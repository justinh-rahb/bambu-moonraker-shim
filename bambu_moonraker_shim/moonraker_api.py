import asyncio
import json
import time
import secrets
import uuid
import os
import tempfile
from typing import Dict, Any, List, Optional
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Request, UploadFile, File
from fastapi.responses import JSONResponse, PlainTextResponse
from bambu_moonraker_shim.state_manager import state_manager
from bambu_moonraker_shim.bambu_client import bambu_client
from bambu_moonraker_shim.config import Config
from bambu_moonraker_shim.database_manager import database_manager
from bambu_moonraker_shim.fan_control import FanTarget, build_fan_command
from bambu_moonraker_shim.ftps_client import ftps_client
from bambu_moonraker_shim.sqlite_manager import get_sqlite_manager

router = APIRouter()

CONFIG_FILES = {
    "printer.cfg": "\n".join(
        [
            "[printer]",
            "kinematics: corexy",
            "max_velocity: 500",
            "max_accel: 10000",
            "",
            "[extruder]",
            "min_temp: 0",
            "max_temp: 300",
            "nozzle_diameter: 0.4",
            "",
            "[heater_bed]",
            "min_temp: 0",
            "max_temp: 120",
            "",
            "[virtual_sdcard]",
            "path: /tmp/gcodes",
            "",
            "[display_status]",
            "",
            "[pause_resume]",
            "",
            "[gcode_macro CANCEL_PRINT]",
            "description: Cancel the actual running print",
            "gcode:",
            "  M117 Cancelled",
            "",
            "[gcode_macro PAUSE]",
            "description: Pause the actual running print",
            "gcode:",
            "  M117 Paused",
            "",
            "[gcode_macro RESUME]",
            "description: Resume the actual running print",
            "gcode:",
            "  M117 Resumed",
            "",
        ]
    )
}


def _config_file_listing():
    now = time.time()
    files = []
    for name, content in CONFIG_FILES.items():
        files.append(
            {
                "path": f"config/{name}",
                "size": len(content.encode("utf-8")),
                "modified": now,
                "permissions": "rw",
            }
        )
    return files


def _config_directory_listing():
    now = time.time()
    files = []
    for name, content in CONFIG_FILES.items():
        files.append(
            {
                "filename": name,
                "modified": now,
                "size": len(content.encode("utf-8")),
                "permissions": "rw",
                "path": _join_moonraker_path("config", name),
            }
        )
    return {
        "dirs": [],
        "files": files,
        "disk_usage": {
            "total": 32 * 1024 * 1024 * 1024,  # 32GB
            "used": 1 * 1024 * 1024 * 1024,  # 1GB
            "free": 31 * 1024 * 1024 * 1024,  # 31GB
        },
        "root_info": {
            "name": "config",
            "permissions": "rw",
            "path": "config",
        },
    }


def _join_moonraker_path(root: str, name: str) -> str:
    return f"{root.rstrip('/')}/{name}"


def _mock_gcode_file() -> Dict[str, Any]:
    return {
        "name": "mock_file.gcode",
        "size": 0,
        "modified": time.time(),
        "is_dir": False,
    }


def _mock_directory_listing(path: str) -> Dict[str, Any]:
    files = []
    if path == "gcodes":
        mock_file = _mock_gcode_file()
        files.append(
            {
                "filename": mock_file["name"],
                "modified": mock_file["modified"],
                "size": mock_file["size"],
                "permissions": "rw",
                "path": _join_moonraker_path(path, mock_file["name"]),
            }
        )

    return {
        "dirs": [],
        "files": files,
        "disk_usage": {
            "total": 32 * 1024 * 1024 * 1024,  # 32GB
            "used": 1 * 1024 * 1024 * 1024,  # 1GB
            "free": 31 * 1024 * 1024 * 1024,  # 31GB
        },
        "root_info": {
            "name": "gcodes",
            "permissions": "rw",
            "path": "gcodes",
        },
    }


def _build_file_list(root: str) -> List[Dict[str, Any]]:
    if root == "config":
        return _config_file_listing()

    if root != "gcodes":
        return []

    if not Config.BAMBU_SERIAL:
        mock_file = _mock_gcode_file()
        return [
            {
                "path": _join_moonraker_path("gcodes", mock_file["name"]),
                "size": mock_file["size"],
                "modified": mock_file["modified"],
                "permissions": "rw",
            }
        ]

    # List files from printer via FTPS
    remote_files = ftps_client.list_files(Config.BAMBU_FTPS_UPLOADS_DIR)

    # Filter to only show gcode files (not directories)
    # Mainsail expects a flat list with "path" starting with "gcodes/"
    files = []
    for f in remote_files:
        if not f["is_dir"]:
            # Filter by extension if desired
            name = f["name"]
            if name.endswith((".gcode", ".gcode.3mf", ".3mf")):
                files.append({
                    "path": _join_moonraker_path("gcodes", name),
                    "size": f["size"],
                    "modified": f["modified"],
                    "permissions": "rw",
                })

    return files


@router.get("/access/oneshot_token")
async def access_oneshot_token():
    token = secrets.token_urlsafe(32)
    expires = int(time.time()) + 60
    return success_response({"token": token, "expires": expires})


# --- HTTP Helpers ---
def success_response(data: Any) -> Dict[str, Any]:
    return {"result": data}


def error_response(code: int, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=code, content={"error": {"code": code, "message": message}}
    )


def flatten_to_nested(flat_dict: dict) -> dict:
    """Convert flat dotted keys to nested dict structure.
    E.g., {"dashboard.layout": []} -> {"dashboard": {"layout": []}}
    """
    nested = {}
    for key, value in flat_dict.items():
        parts = key.split(".")
        current = nested
        for part in parts[:-1]:
            if part not in current:
                current[part] = {}
            current = current[part]
        current[parts[-1]] = value
    return nested


# --- HTTP Endpoints ---


@router.get("/server/info")
async def server_info():
    return success_response(
        {
            "state": "ready",
            "klippy_state": "ready",
            "components": [
                "printer",
                "websocket",
                "database",
                "file_manager",
                "webcams",
            ],
            "version": "v0.0.1-bambu-shim",
            "api_version": [1, 0, 0],
        }
    )


@router.get("/printer/info")
async def printer_info():
    return success_response(
        {
            "state": "ready",
            "hostname": "bambu-shim",
            "model": "Bambu",
            "firmware_version": "unknown",
            "software_version": "bambu-moonraker-shim",
        }
    )


@router.get("/server/temperature_store")
async def http_temperature_store(include_monitors: bool = False):
    data = state_manager.get_temperature_history(include_monitors)
    return success_response(data)


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

    return success_response({"status": result_status, "eventtime": time.time()})


@router.get("/server/files/list")
async def file_list(root: str = "gcodes"):
    try:
        files = _build_file_list(root)
        return success_response(files)
    except Exception as e:
        print(f"Error listing files: {e}")
        # Return empty list on error rather than failing
        return success_response([])


@router.get("/server/files/directory")
async def get_directory(path: str = "gcodes", extended: bool = False):
    """
    Get directory contents with caching support.
    Used by Mainsail's file browser.
    """
    sqlite_manager = get_sqlite_manager()

    if path == "config":
        return success_response(_config_directory_listing())

    if not Config.BAMBU_SERIAL and path == "gcodes":
        return success_response(_mock_directory_listing(path))
    
    # Determine actua FTPS path to check
    ftps_path = Config.BAMBU_FTPS_UPLOADS_DIR
    
    # If user is browsing a subdirectory of gcodes
    if path.startswith("gcodes/") and len(path) > 7:
        subdir = path[7:] # Strip "gcodes/"
        ftps_path = f"{Config.BAMBU_FTPS_UPLOADS_DIR}/{subdir}".replace("//", "/")

    # NOTE: Same cache logic as WebSocket endpoint
    cached_files = None
    if path == "gcodes":
            cached_files = sqlite_manager.get_cached_files(max_age=300)
    
    if cached_files is None:
        # Cache miss - fetch from FTPS
        print(f"Fetching files from FTPS for path: {ftps_path} (requested: {path})")
        try:
            remote_files = ftps_client.list_files(ftps_path)
            # Only cache the root listing for now
            if path == "gcodes":
                sqlite_manager.cache_files(remote_files)
            cached_files = remote_files
        except Exception as e:
            print(f"Error fetching files from FTPS: {e}")
            cached_files = []
    
    # Transform to Moonraker format
    dirs = []
    files = []
    
    for f in cached_files:
        if f["is_dir"]:
            dirs.append({
                "dirname": f["name"],
                "modified": f["modified"],
                "size": f["size"],
                "permissions": "rw",
                "path": _join_moonraker_path(path, f["name"]),
            })
        else:
            files.append({
                "filename": f["name"],
                "modified": f["modified"],
                "size": f["size"],
                "permissions": "rw",
                "path": _join_moonraker_path(path, f["name"]),
            })
    
    result = {
        "dirs": dirs,
        "files": files,
        "disk_usage": {
            "total": 32 * 1024 * 1024 * 1024, # 32GB
            "used": 1 * 1024 * 1024 * 1024,   # 1GB
            "free": 31 * 1024 * 1024 * 1024   # 31GB
        },
        "root_info": {
            "name": "gcodes",
            "permissions": "rw",
            "path": "gcodes",
        }
    }
    
    return success_response(result)


@router.post("/server/files/upload")
async def file_upload(file: UploadFile = File(...), path: str = None):
    try:
        # Save uploaded file to a temp location first
        temp_fd, temp_path = tempfile.mkstemp(suffix=".gcode")
        try:
            # Write the uploaded file to temp
            with os.fdopen(temp_fd, 'wb') as tmp:
                content = await file.read()
                tmp.write(content)
            
            # Upload to printer via FTPS
            ftps_client.upload_file(temp_path, file.filename)
            
            # Invalidate file cache so next list is fresh
            sqlite_manager = get_sqlite_manager()
            sqlite_manager.clear_file_cache()
            
            # Get file size
            file_size = len(content)
            
            return success_response({
                "item": {
                    "path": f"gcodes/{file.filename}",
                    "size": file_size,
                    "modified": time.time()
                },
                "print_started": False
            })
        finally:
            # Clean up temp file
            if os.path.exists(temp_path):
                os.unlink(temp_path)
    except Exception as e:
        print(f"Upload error: {e}")
        return error_response(500, f"Upload failed: {str(e)}")




@router.delete("/server/files/gcodes/{filename:path}")
async def file_delete(filename: str):
    """Delete a file from the printer via FTPS."""
    try:
        ftps_client.delete_file(filename)
        
        # Invalidate file cache so next list is fresh
        sqlite_manager = get_sqlite_manager()
        sqlite_manager.clear_file_cache()
        
        return success_response("ok")
    except Exception as e:
        print(f"Delete error: {e}")
        return error_response(500, f"Delete failed: {str(e)}")


@router.get("/server/files/{root}/{path:path}")
async def file_download(root: str, path: str):
    # Mocking theme files to avoid 404s for Mainsail
    if root == "config" and ".theme" in path:
        # Return empty JSON for theme files to satisfy Mainsail
        return success_response({})
    if root == "config":
        content = CONFIG_FILES.get(path)
        if content is not None:
            return PlainTextResponse(content)

    # Generic 404 for now unless checked against real files
    return error_response(404, "File not found")


@router.get("/server/database/item")
async def database_get(namespace: str, key: str = None):
    # namespace is required, key is optional query param
    val = database_manager.get_item(namespace, key)
    # Convert flat dotted keys to nested structure for mainsail namespace
    if namespace == "mainsail" and key is None and isinstance(val, dict):
        val = flatten_to_nested(val)
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


@router.get("/server/database/list")
async def database_list():
    namespaces = database_manager.get_namespaces()
    return success_response({"namespaces": namespaces, "backups": []})


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
        self._keepalive_task = None

    def start(self):
        if not self._keepalive_task:
            self._keepalive_task = asyncio.create_task(self._keepalive_loop())

    async def _keepalive_loop(self):
        """Sends periodic heartbeats to all clients to prevent disconnects."""
        while True:
            await asyncio.sleep(20)  # 20s interval
            if self.active_connections:
                # Minimal heartbeat that Mainsail accepts/ignores but keeps socket alive
                # notify_proc_stat_update is standard Moonraker
                msg = {
                    "jsonrpc": "2.0",
                    "method": "notify_proc_stat_update",
                    "params": [
                        {
                            "moonraker_stats": {
                                "cpu_usage": 0.0,
                                "memory": 0,
                                "mem_units": "kB",
                            },
                            "cpu_temp": 0.0,
                            "network": {},
                            "websocket_connections": len(self.active_connections),
                            "time": time.time(),
                        }
                    ],
                }
                await self.broadcast(msg)

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        # Notify readiness immediately
        await websocket.send_json(
            {"jsonrpc": "2.0", "method": "notify_klippy_ready", "params": []}
        )

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        # Broadcast to all connected clients
        for connection in list(self.active_connections):
            try:
                await connection.send_json(message)
            except:
                pass  # Handle broken pipe?


manager = ConnectionManager()


@router.on_event("startup")
async def start_websocket_keepalive():
    manager.start()



# Hook state manager to broadcast
async def broadcast_state_update(notification: dict):
    await manager.broadcast(notification)


state_manager.set_broadcast_callback(broadcast_state_update)


@router.websocket("/websocket")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    connection_id = id(websocket)
    try:
        while True:
            data = await websocket.receive_json()
            # Process JSON-RPC request
            response = await handle_jsonrpc(data, connection_id)
            if response:
                await websocket.send_json(response)
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        print(f"WS Error: {e}")
        manager.disconnect(websocket)


async def handle_jsonrpc(
    request: Dict[str, Any], connection_id: int
) -> Optional[Dict[str, Any]]:
    req_id = request.get("id")
    method = request.get("method")

    print(f"RPC Request: {method} params={request.get('params')}")

    response = {"jsonrpc": "2.0", "id": req_id}

    if method == "server.info":
        response["result"] = {
            "state": "ready",
            "klippy_state": "ready",
            "components": [
                "printer",
                "websocket",
                "database",
                "file_manager",
                "webcams",
                "history",
                "job_queue",
            ],
            "version": "v0.0.1-bambu-shim",
            "api_version": [1, 0, 0],
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

    elif method == "server.database.get_item":
        namespace = request.get("params", {}).get("namespace")
        key = request.get("params", {}).get("key")

        # Block maintenance reads - return empty to prevent UI errors
        if namespace == "maintenance":
            val = {}
        else:
            val = database_manager.get_item(namespace, key)
            # Convert flat dotted keys to nested structure for mainsail namespace
            # This is required because Mainsail's setDataDeep expects nested objects
            if namespace == "mainsail" and key is None and isinstance(val, dict):
                val = flatten_to_nested(val)

        response["result"] = {"namespace": namespace, "key": key, "value": val}

    elif method == "server.database.post_item":
        namespace = request.get("params", {}).get("namespace")
        key = request.get("params", {}).get("key")
        value = request.get("params", {}).get("value")

        # Block maintenance writes - Mainsail creates incomplete entries that break the UI
        if namespace == "maintenance":
            response["result"] = {"namespace": namespace, "key": key, "value": value}
        else:
            new_val = database_manager.post_item(namespace, key, value)
            response["result"] = {"namespace": namespace, "key": key, "value": new_val}

    elif method == "server.database.delete_item":
        namespace = request.get("params", {}).get("namespace")
        key = request.get("params", {}).get("key")
        val = database_manager.delete_item(namespace, key)
        response["result"] = {"namespace": namespace, "key": key, "value": val}

    elif method == "server.temperature_store":
        include_monitors = request.get("params", {}).get("include_monitors", False)
        response["result"] = state_manager.get_temperature_history(include_monitors)

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
            "thumbnails": [],
        }

    elif method == "printer.info":
        response["result"] = {
            "state": "ready",
            "hostname": "bambu-shim",
            "model": "Bambu",
            "firmware_version": "unknown",
            "software_version": "bambu-moonraker-shim",
            "cpu_info": "Mock CPU",
        }

    elif method == "server.connection.identify":
        response["result"] = {"connection_id": connection_id}

    elif method == "server.gcode_store":
        response["result"] = {"gcode_store": []}

    elif method == "printer.fan.set_speed":
        params = request.get("params", {})
        fan_name = params.get("fan")
        speed = params.get("speed")

        try:
            command = build_fan_command(fan_name, speed)
        except ValueError as exc:
            response["error"] = {"code": 400, "message": str(exc)}
            return response

        await bambu_client.send_gcode_line(command.gcode)

        speed_ratio = command.speed / 255.0 if command.speed > 0 else 0.0
        if command.target == FanTarget.PART:
            await state_manager.update_state({"fan": {"speed": speed_ratio}})
        elif command.target == FanTarget.AUX:
            await state_manager.update_state(
                {"fan_generic aux": {"speed": speed_ratio}, "fan_aux": {"speed": speed_ratio}}
            )
        elif command.target == FanTarget.CHAMBER:
            await state_manager.update_state(
                {
                    "fan_generic chamber": {"speed": speed_ratio},
                    "fan_chamber": {"speed": speed_ratio},
                }
            )

        response["result"] = "ok"

    elif method == "server.webcams.list":
        # Retrieve webcams from database
        # We'll store them in namespace "moonraker", key "webcams" as a list
        webcams = database_manager.get_item("moonraker", "webcams")
        if not webcams:
            webcams = []
        response["result"] = {"webcams": webcams}

    elif method == "server.webcams.post_item":
        params = request.get("params", {})
        # Load existing
        webcams = database_manager.get_item("moonraker", "webcams") or []

        uid = params.get("uid")
        target_cam = None

        # Check if updating existing
        if uid:
            for cam in webcams:
                if cam["uid"] == uid:
                    target_cam = cam
                    break

        if target_cam:
            # Update existing
            target_cam.update(
                {
                    "name": params.get("name", target_cam.get("name")),
                    "location": params.get("location", target_cam.get("location")),
                    "service": params.get("service", target_cam.get("service")),
                    "target_fps": params.get(
                        "target_fps", target_cam.get("target_fps")
                    ),
                    "stream_url": params.get(
                        "stream_url", target_cam.get("stream_url", "")
                    ),
                    "snapshot_url": params.get(
                        "snapshot_url", target_cam.get("snapshot_url", "")
                    ),
                    "flip_horizontal": params.get(
                        "flip_horizontal", target_cam.get("flip_horizontal")
                    ),
                    "flip_vertical": params.get(
                        "flip_vertical", target_cam.get("flip_vertical")
                    ),
                    "rotation": params.get("rotation", target_cam.get("rotation")),
                    "enabled": params.get("enabled", target_cam.get("enabled", True)),
                }
            )
            # Handle potential legacy keys if needed, but standardizing on stream_url/snapshot_url
            new_cam = target_cam
        else:
            # New webcam
            uid = str(uuid.uuid4())
            new_cam = {
                "name": params.get("name", "New Webcam"),
                "location": params.get("location", "printer"),
                "service": params.get("service", "mjpegstreamer"),
                "target_fps": params.get("target_fps", 15),
                "stream_url": params.get("stream_url", ""),
                "snapshot_url": params.get("snapshot_url", ""),
                "flip_horizontal": params.get("flip_horizontal", False),
                "flip_vertical": params.get("flip_vertical", False),
                "rotation": params.get("rotation", 0),
                "source": "database",
                "uid": uid,
                "enabled": True,
            }
            webcams.append(new_cam)

        database_manager.post_item("moonraker", "webcams", webcams)

        await notify_webcams_changed()
        response["result"] = {"item": new_cam}

    elif method == "server.webcams.delete_item":
        uid = request.get("params", {}).get("uid")
        webcams = database_manager.get_item("moonraker", "webcams") or []

        # Filter out the one to delete
        new_list = [cam for cam in webcams if cam["uid"] != uid]

        if len(new_list) < len(webcams):
            database_manager.post_item("moonraker", "webcams", new_list)
            await notify_webcams_changed()
            response["result"] = {"item": {"uid": uid}}  # Return deleted ID
        else:
            # Item not found, but successful deletion (idempotent)
            response["result"] = {"item": {"uid": uid}}

    elif method == "server.webcams.test":
        # Just return ok for now
        response["result"] = {"can_stream": True}

    elif method == "server.config":
        response["result"] = {"config": {}}

    elif method == "machine.system_info":
        response["result"] = {
            "system_info": {"cpu": "unknown", "python": "3.x", "os": "mock_os"}
        }

    elif method == "machine.proc_stats":
        response["result"] = {
            "system_cpu_usage": {"cpu": 0.0},
            "system_memory": {"available": 1000, "total": 2000, "used": 1000},
            "websocket_connections": 1,
        }

    elif method == "server.database.list":
        namespaces = database_manager.get_namespaces()
        response["result"] = {"namespaces": namespaces, "backups": []}

    elif method == "printer.gcode.script":
        script = request.get("params", {}).get("script", "")
        # Mainsail functionality often depends on this returning successfully
        # We split by newlines and send each as a separate command
        lines = script.split("\n")
        for line in lines:
            line = line.strip()
            if not line:
                continue
            
            # Intercept SET_PIN command for LED control
            # Format: SET_PIN PIN=caselight VALUE=1.00
            if line.upper().startswith("SET_PIN"):
                try:
                    parts = line.split()
                    pin_name = ""
                    value = 0.0
                    for part in parts:
                        if part.upper().startswith("PIN="):
                            pin_name = part.split("=")[1]
                        elif part.upper().startswith("VALUE="):
                            value = float(part.split("=")[1])
                    
                    if pin_name == "caselight":
                        is_on = value > 0
                        await bambu_client.set_light(is_on)
                        # Updates state locally so UI reflects change immediately
                        await state_manager.update_state({"output_pin caselight": {"value": 1.0 if is_on else 0.0}})
                        continue # Don't send this to printer as raw gcode
                except Exception as e:
                    print(f"Error parsing SET_PIN: {e}")

            # Intercept SET_FAN_SPEED for fan control
            # Format: SET_FAN_SPEED FAN=<name> SPEED=<value>
            if line.upper().startswith("SET_FAN_SPEED"):
                try:
                    parts = line.split()
                    fan_name = None
                    speed = None
                    for part in parts:
                        upper = part.upper()
                        if upper.startswith("FAN="):
                            fan_name = part.split("=", 1)[1]
                        elif upper.startswith("SPEED="):
                            speed = part.split("=", 1)[1]

                    command = build_fan_command(fan_name, speed)
                    await bambu_client.send_gcode_line(command.gcode)

                    speed_ratio = command.speed / 255.0 if command.speed > 0 else 0.0
                    if command.target == FanTarget.PART:
                        await state_manager.update_state({"fan": {"speed": speed_ratio}})
                    elif command.target == FanTarget.AUX:
                        await state_manager.update_state(
                            {"fan_generic aux": {"speed": speed_ratio}, "fan_aux": {"speed": speed_ratio}}
                        )
                    elif command.target == FanTarget.CHAMBER:
                        await state_manager.update_state(
                            {
                                "fan_generic chamber": {"speed": speed_ratio},
                                "fan_chamber": {"speed": speed_ratio},
                            }
                        )
                    continue # Don't send this to printer as raw gcode
                except Exception as e:
                    print(f"Error parsing SET_FAN_SPEED: {e}")

            # Intercept heater commands (M104/M109/M140/M190)
            if line.upper().startswith(("M104", "M109", "M140", "M190")):
                try:
                    parts = line.upper().split()
                    cmd = parts[0]
                    temp = None
                    for part in parts:
                        if part.startswith("S"):
                            temp = float(part[1:])
                            break

                    if temp is None:
                        response["error"] = {
                            "code": 400,
                            "message": f"Missing S parameter in heater command: {line}",
                        }
                        return response

                    if cmd in ("M104", "M109"):
                        result = await bambu_client.set_nozzle_temp(temp, wait=(cmd == "M109"))
                    elif cmd in ("M140", "M190"):
                        result = await bambu_client.set_bed_temp(temp, wait=(cmd == "M190"))
                    else:
                        result = {"error": f"Unsupported heater command: {cmd}"}

                    if "error" in result:
                        response["error"] = {"code": 400, "message": result["error"]}
                        return response
                    if cmd in ("M104", "M109"):
                        await state_manager.update_state({"extruder": {"target": temp}})
                    elif cmd in ("M140", "M190"):
                        await state_manager.update_state({"heater_bed": {"target": temp}})

                    continue
                except Exception as e:
                    print(f"Heater parse error: {e}")

            # Intercept SET_HEATER_TEMPERATURE for Moonraker compatibility
            # Format: SET_HEATER_TEMPERATURE HEATER=<name> TARGET=<value>
            if line.upper().startswith("SET_HEATER_TEMPERATURE"):
                try:
                    parts = line.split()
                    heater_name = None
                    target = None
                    wait = False
                    for part in parts:
                        upper = part.upper()
                        if upper.startswith("HEATER="):
                            heater_name = part.split("=", 1)[1]
                        elif upper.startswith("TARGET="):
                            target = float(part.split("=", 1)[1])
                        elif upper.startswith("WAIT="):
                            wait_value = part.split("=", 1)[1].strip().lower()
                            wait = wait_value in {"1", "true", "yes", "on"}

                    if heater_name is None or target is None:
                        response["error"] = {
                            "code": 400,
                            "message": f"Invalid SET_HEATER_TEMPERATURE command: {line}",
                        }
                        return response

                    if heater_name == "extruder":
                        result = await bambu_client.set_nozzle_temp(target, wait=wait)
                    elif heater_name in ("heater_bed", "bed"):
                        result = await bambu_client.set_bed_temp(target, wait=wait)
                    else:
                        result = {"error": f"Unknown heater: {heater_name}"}

                    if "error" in result:
                        response["error"] = {"code": 400, "message": result["error"]}
                        return response
                    if heater_name == "extruder":
                        await state_manager.update_state({"extruder": {"target": target}})
                    elif heater_name in ("heater_bed", "bed"):
                        await state_manager.update_state({"heater_bed": {"target": target}})
                    continue
                except Exception as e:
                    print(f"SET_HEATER_TEMPERATURE parse error: {e}")

            # Basic logging for now
            print(f"Executing G-code: {line}")
            await bambu_client.send_gcode_line(line)
        response["result"] = "ok"

    elif method == "server.files.roots":
        response["result"] = {
            "roots": [
                {
                    "name": "gcodes",
                    "path": "gcodes",
                    "permissions": "rw"
                },
                {
                    "name": "config",
                    "path": "config",
                    "permissions": "rw"
                }
            ]
        }

    elif method == "server.files.list":
        params = request.get("params", {})
        root = params.get("root", "gcodes")
        try:
            files = _build_file_list(root)
            response["result"] = {"root": root, "files": files}
        except Exception as e:
            print(f"Error listing files: {e}")
            response["result"] = {"root": root, "files": []}

    elif method == "server.files.get_directory":
        # Get file listing with caching
        params = request.get("params", {})
        path = params.get("path", "gcodes")

        if not Config.BAMBU_SERIAL and path == "gcodes":
            response["result"] = _mock_directory_listing(path)
            return response
        
        # Determine actual FTPS path to check
        # Moonraker path is "gcodes/subdirectory", but we need relative for FTPS
        ftps_path = Config.BAMBU_FTPS_UPLOADS_DIR
        
        # If user is browsing a subdirectory of gcodes
        if path.startswith("gcodes/") and len(path) > 7:
            subdir = path[7:] # Strip "gcodes/"
            ftps_path = f"{Config.BAMBU_FTPS_UPLOADS_DIR}/{subdir}".replace("//", "/")
        
        sqlite_manager = get_sqlite_manager()
        
        # NOTE: Current simple cache implementation assumes key is always "gcodes" list
        # We need to update cache key to be path-specific
        # For now, we only cache the root. Subdirs will bypass cache or we need to fix cache key.
        # Let's simple check: if root, leverage cache. If subdir, fetch fresh (or update cache key logic).
        
        cached_files = None
        if path == "gcodes":
             cached_files = sqlite_manager.get_cached_files(max_age=300)
        
        if cached_files is None:
            # Cache miss - fetch from FTPS
            print(f"Fetching files from FTPS for path: {ftps_path} (requested: {path})")
            try:
                remote_files = ftps_client.list_files(ftps_path)
                # Only cache the root listing for now to avoid complexity
                if path == "gcodes":
                    sqlite_manager.cache_files(remote_files)
                cached_files = remote_files
            except Exception as e:
                print(f"Error fetching files from FTPS: {e}")
                cached_files = []
        else:
            print(f"Cache hit - returning {len(cached_files)} cached files")
        
        # Transform to Moonraker format
        dirs = []
        files = []
        
        for f in cached_files:
            if f["is_dir"]:
                dirs.append({
                    "dirname": f["name"],
                    "modified": f["modified"],
                    "size": f["size"],
                    "permissions": "rw",
                    "path": _join_moonraker_path(path, f["name"]),
                })
            else:
                files.append({
                    "filename": f["name"],
                    "modified": f["modified"],
                    "size": f["size"],
                    "permissions": "rw",
                    "path": _join_moonraker_path(path, f["name"]),
                })
        
        response["result"] = {
            "dirs": dirs,
            "files": files,
            "disk_usage": {
                "total": 32 * 1024 * 1024 * 1024, # 32GB Fake Total
                "used": 1 * 1024 * 1024 * 1024,   # 1GB Fake Used
                "free": 31 * 1024 * 1024 * 1024   # 31GB Fake Free
            },
            "root_info": {
                "name": "gcodes", # Always the root name, even for subdirs
                "permissions": "rw",
                "path": "gcodes",
            }
        }

    elif method == "server.history.list":
        # Get job history from SQLite
        params = request.get("params", {})
        limit = params.get("limit", 50)
        start = params.get("start", 0)
        before = params.get("before")
        since = params.get("since")
        order = params.get("order", "desc")
        
        sqlite_manager = get_sqlite_manager()
        history = sqlite_manager.get_job_history(
            limit=limit,
            before=before,
            since=since,
            order=order
        )
        
        response["result"] = history

    elif method == "server.history.totals":
        # Get job totals
        sqlite_manager = get_sqlite_manager()
        totals = sqlite_manager.get_job_totals()
        
        response["result"] = {
            "job_totals": totals
        }

    elif method == "server.job_queue.status":
        # Bambu doesn't support native job queuing
        # Return empty queue
        response["result"] = {
            "queued_jobs": [],
            "queue_state": "ready"
        }

    else:
        # Ignore unknown methods or return null result to avoid errors
        # Mainsail calls a lot of things we might not implement yet.
        print(f"Unknown WS method: {method}")
        response["result"] = {}  # Safe fallback

    print(f"RPC Response: {json.dumps(response)}")
    return response


async def notify_webcams_changed():
    webcams = database_manager.get_item("moonraker", "webcams") or []
    await manager.broadcast(
        {
            "jsonrpc": "2.0",
            "method": "notify_webcams_changed",
            "params": {"webcams": webcams},
        }
    )
