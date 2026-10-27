import asyncio
import json
import time
import secrets
import shlex
import uuid
import os
import posixpath
import re
import tempfile
from typing import Dict, Any, List, Optional
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Request, UploadFile, File, Form
from fastapi.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from bambu_moonraker_shim.state_manager import state_manager
from bambu_moonraker_shim.bambu_client import bambu_client
from bambu_moonraker_shim.camera_manager import camera_manager
from bambu_moonraker_shim.config import Config
from bambu_moonraker_shim.database_manager import DatabaseError, database_manager
from bambu_moonraker_shim.external_spool import EXTERNAL_MACROS, MATERIAL_PRESETS, EXTERNAL_LOAD_CHOICES
from bambu_moonraker_shim.fan_control import FanTarget, build_fan_command
from bambu_moonraker_shim.ftps_client import ftps_client
from bambu_moonraker_shim.sqlite_manager import get_sqlite_manager
from bambu_moonraker_shim.file_metadata import MetadataError, validate_filename
from bambu_moonraker_shim.metadata_service import metadata_service

router = APIRouter()

_DEFAULT_DISK_USAGE = {
    "total": 32 * 1024 * 1024 * 1024,
    "used": 1 * 1024 * 1024 * 1024,
    "free": 31 * 1024 * 1024 * 1024,
}

_GCODE_FILE_SUFFIXES = (".gcode", ".gcode.3mf", ".3mf")
_CONFIG_THEME_FILES = {
    "maintenance.json": "{}",
}
_EXCLUDED_GCODES_ROOT_DIRS = {
    "logger",
    "recorder",
    "image",
    "ipcam",
    "timelapse",
    "cache",
    "language",
    "model",
    "corelogger",
    "verify_job",
    ".spotlight-v100",
    ".fseventsd",
}

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


for _macro_name, _definition in EXTERNAL_MACROS.items():
    CONFIG_FILES["printer.cfg"] += "\n\n" + "\n".join([
        f"[gcode_macro {_macro_name}]", f"description: {_definition['description']}", "gcode:",
        *["  " + line for line in _definition["gcode"].splitlines()],
    ])


def _config_file_listing():
    now = time.time()
    files = []
    for name, content in CONFIG_FILES.items():
        files.append(
            {
                "path": name,
                "size": len(content.encode("utf-8")),
                "modified": now,
                "permissions": "rw",
            }
        )
    for name, content in _CONFIG_THEME_FILES.items():
        files.append(
            {
                "path": f".theme/{name}",
                "size": len(content.encode("utf-8")),
                "modified": now,
                "permissions": "rw",
            }
        )
    return files


def _config_directory_listing(path: str = "config"):
    now = time.time()
    dirs = []
    files = []
    if path == "config":
        dirs.append(
            {
                "dirname": ".theme",
                "modified": now,
                "size": 0,
                "permissions": "rw",
                "path": ".theme",
            }
        )
        for name, content in CONFIG_FILES.items():
            files.append(
                {
                    "filename": name,
                    "modified": now,
                    "size": len(content.encode("utf-8")),
                    "permissions": "rw",
                    "path": name,
                }
            )
    elif path == "config/.theme":
        for name, content in _CONFIG_THEME_FILES.items():
            files.append(
                {
                    "filename": name,
                    "modified": now,
                    "size": len(content.encode("utf-8")),
                    "permissions": "rw",
                    "path": name,
                }
            )
    return {
        "dirs": dirs,
        "files": files,
        "disk_usage": dict(_DEFAULT_DISK_USAGE),
        "root_info": {
            "name": "config",
            "permissions": "rw",
            "path": "config",
        },
    }


def _join_moonraker_path(root: str, name: str) -> str:
    return f"{root.rstrip('/')}/{name}"


def _mjpeg_chunk(frame: bytes) -> bytes:
    return (
        b"--"
        + camera_manager.MJPEG_BOUNDARY.encode("ascii")
        + b"\r\n"
        + b"Content-Type: image/jpeg\r\n"
        + f"Content-Length: {len(frame)}\r\n\r\n".encode("ascii")
        + frame
        + b"\r\n"
    )


def _current_webcams() -> List[Dict[str, Any]]:
    webcams = database_manager.get_item("moonraker", "webcams") or []
    builtin_webcam = camera_manager.get_builtin_webcam()
    if not builtin_webcam:
        return webcams

    builtin_uid = builtin_webcam["uid"]
    stored_builtin = next((cam for cam in webcams if cam.get("uid") == builtin_uid), {})
    builtin_webcam = {**builtin_webcam, **{key: value for key, value in stored_builtin.items()
                       if key in {"name", "location", "enabled", "flip_horizontal", "flip_vertical", "rotation", "target_fps"}}}
    filtered = [cam for cam in webcams if cam.get("uid") != builtin_uid]
    return [builtin_webcam, *filtered]


def _mock_gcode_file() -> Dict[str, Any]:
    _ensure_mock_file_exists()
    mock_path = os.path.join(Config.GCODES_DIR, "mock_file.gcode")
    stat_result = os.stat(mock_path)
    return {
        "name": "mock_file.gcode",
        "size": stat_result.st_size,
        "modified": stat_result.st_mtime,
        "is_dir": False,
    }


def _ensure_mock_file_exists():
    os.makedirs(Config.GCODES_DIR, exist_ok=True)
    mock_path = os.path.join(Config.GCODES_DIR, "mock_file.gcode")
    if not os.path.exists(mock_path):
        with open(mock_path, "w", encoding="utf-8") as fp:
            fp.write("G28\nM104 T0 S200\nM140 S60\n")


def _list_mock_local_files() -> List[Dict[str, Any]]:
    _ensure_mock_file_exists()
    files: List[Dict[str, Any]] = []
    for entry in os.scandir(Config.GCODES_DIR):
        if not entry.is_file():
            continue
        stat_result = entry.stat()
        files.append(
            {
                "name": entry.name,
                "size": stat_result.st_size,
                "modified": stat_result.st_mtime,
                "is_dir": False,
            }
        )
    files.sort(key=lambda item: item["name"].lower())
    return files


def _mock_directory_listing(path: str) -> Dict[str, Any]:
    files = []
    if path == "gcodes":
        for mock_file in _list_mock_local_files():
            files.append(
                {
                    "filename": mock_file["name"],
                    "modified": mock_file["modified"],
                    "size": mock_file["size"],
                    "permissions": "rw",
                    "path": _directory_entry_path(path, mock_file["name"]),
                }
            )

    return {
        "dirs": [],
        "files": files,
        "disk_usage": dict(_DEFAULT_DISK_USAGE),
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
        files = []
        for mock_file in _list_mock_local_files():
            files.append(
                {
                    "path": mock_file["name"],
                    "size": mock_file["size"],
                    "modified": mock_file["modified"],
                    "permissions": "rw",
                }
            )
        return files

    # List files from printer via FTPS
    remote_files = ftps_client.list_files(Config.BAMBU_FTPS_UPLOADS_DIR)

    # Filter to only show gcode files (not directories)
    # server.files.list returns paths relative to the selected root.
    files = []
    for f in remote_files:
        if "/" in f["name"]:
            continue
        if not f["is_dir"]:
            # Filter by extension if desired
            name = f["name"]
            if name.lower().endswith(_GCODE_FILE_SUFFIXES):
                display_name = name
                files.append({
                    "path": display_name,
                    "size": f["size"],
                    "modified": f["modified"],
                    "permissions": "rw",
                })

    return files


def _file_roots_payload() -> List[Dict[str, str]]:
    return [
        {
            "name": "gcodes",
            "path": Config.GCODES_DIR or "/tmp/gcodes",
            "permissions": "rw",
        },
        {
            "name": "config",
            "path": "/app/config",
            "permissions": "rw",
        },
    ]


def _server_info_payload(include_history: bool = True) -> Dict[str, Any]:
    components = [
        "printer",
        "websocket",
        "database",
        "file_manager",
        "webcam",
    ]
    if include_history:
        components.extend(["history", "job_queue"])

    connected = bambu_client.connected
    return {
        "state": "ready" if connected else "disconnected",
        "state_message": "Printer is ready" if connected else "Printer MQTT disconnected",
        "klippy_connected": connected,
        "klippy_state": "ready" if connected else "disconnected",
        "components": components,
        "registered_directories": ["gcodes", "config"],
        "failed_components": [],
        "warnings": [],
        "version": "v0.0.1-bambu-shim",
        "api_version": [1, 0, 0],
        "api_version_string": "1.0.0",
    }


def _get_disk_usage(root: str) -> Dict[str, int]:
    if root == "config" or not Config.BAMBU_SERIAL:
        return dict(_DEFAULT_DISK_USAGE)
    try:
        return ftps_client.get_storage_info()
    except Exception as exc:
        print(f"Disk usage lookup failed: {exc}")
        return dict(_DEFAULT_DISK_USAGE)


def _include_gcodes_directory_entry(path: str, name: str) -> bool:
    if path != "gcodes":
        return True
    lower_name = name.lower()
    if lower_name.startswith("."):
        return False
    return lower_name not in _EXCLUDED_GCODES_ROOT_DIRS


def _include_gcodes_file_entry(path: str, name: str) -> bool:
    if path != "gcodes":
        return True
    return name.lower().endswith(_GCODE_FILE_SUFFIXES)


def _is_root_direct_child(entry: Dict[str, Any]) -> bool:
    raw_path = str(entry.get("path") or entry.get("name") or "").strip()
    if not raw_path:
        return False
    normalized = raw_path.lstrip("/")
    if normalized.startswith("gcodes/"):
        normalized = normalized[7:]
    return "/" not in normalized


def _directory_entry_path(path: str, name: str) -> str:
    if path.startswith("gcodes/"):
        subdir = path[7:].strip("/")
        return f"{subdir}/{name}".strip("/")
    if path.startswith("config/"):
        subdir = path[7:].strip("/")
        return f"{subdir}/{name}".strip("/")
    return name


def _cache_has_non_root_entries(entries: Optional[List[Dict[str, Any]]]) -> bool:
    if not entries:
        return False
    return any(not _is_root_direct_child(entry) for entry in entries)


def _legacy_display_filename(name: str) -> str:
    lower_name = name.lower()
    if lower_name.endswith(".gcode.3mf"):
        # Compatibility for requests from pre-patch browser tabs.
        return name[:-4]
    return name


def _cached_file_exists(name: str) -> bool:
    sqlite_manager = get_sqlite_manager()
    cached = sqlite_manager.get_cached_files(max_age=300) or []
    target = name.lower()
    for entry in cached:
        if entry.get("is_dir"):
            continue
        if str(entry.get("name", "")).lower() == target:
            return True
    return False


def _m220_percent_to_mode(percent: float) -> int:
    # Bambu fixed speed presets: 1=silent (50), 2=standard (100), 3=sport (124), 4=ludicrous (166).
    presets = {1: 50.0, 2: 100.0, 3: 124.0, 4: 166.0}
    return min(presets, key=lambda mode: abs(percent - presets[mode]))


def _extract_skip_object_ids(params: Dict[str, str]) -> List[int]:
    ids: List[int] = []
    for key in ("OBJECT", "OBJ", "ID"):
        value = params.get(key)
        if value is None:
            continue
        try:
            ids.append(int(value))
        except (TypeError, ValueError):
            pass
    name_value = params.get("NAME")
    if name_value:
        digits = "".join(ch for ch in name_value if ch.isdigit())
        if digits:
            try:
                ids.append(int(digits))
            except (TypeError, ValueError):
                pass
    # Preserve order while deduplicating.
    deduped: List[int] = []
    for object_id in ids:
        if object_id not in deduped:
            deduped.append(object_id)
    return deduped


@router.get("/access/oneshot_token")
async def access_oneshot_token():
    token = secrets.token_urlsafe(32)
    expires = int(time.time()) + 60
    return success_response({"token": token, "expires": expires})


# --- HTTP Helpers ---
def success_response(data: Any) -> Dict[str, Any]:
    return {"result": data}


def _is_macro_command(command: str) -> bool:
    if not command:
        return False
    first_word = command.split()[0].upper()
    non_macro_commands = {
        "SAVE_GCODE_STATE",
        "RESTORE_GCODE_STATE",
        "SET_PIN",
        "SET_FAN_SPEED",
        "SET_HEATER_TEMPERATURE",
    }
    if first_word in non_macro_commands:
        return False
    known_macros = {
        "PRINT_START",
        "START_PRINT",
        "PRINT_END",
        "END_PRINT",
        "HEATERS_OFF",
        "TURN_OFF_HEATERS",
        "PAUSE",
        "RESUME",
        "CANCEL_PRINT",
        "LOAD_FILAMENT",
        "UNLOAD_FILAMENT",
        "BED_MESH_CALIBRATE",
        "EXCLUDE_OBJECT",
    }
    if first_word in known_macros:
        return True
    if not first_word.startswith(("M", "G", "T")) and first_word[0].isalpha():
        return True
    return False


def _parse_macro(command: str) -> tuple[str, Dict[str, str]]:
    parts = shlex.split(command, comments=False)
    macro_name = parts[0].upper()
    params: Dict[str, str] = {}
    for part in parts[1:]:
        if "=" in part:
            key, value = part.split("=", 1)
            params[key.upper()] = value
    return macro_name, params


def _parse_macro_param(
    params: Dict[str, str], keys: List[str]
) -> tuple[Optional[float], Optional[str]]:
    for key in keys:
        if key in params:
            value = params[key]
            try:
                return float(value), None
            except (TypeError, ValueError):
                return None, f"Invalid value for {key}: {value}"
    return None, None


def _normalize_filename(filename: str) -> str:
    if filename.startswith("gcodes/"):
        filename = filename[7:]
    normalized = filename.lstrip("/")
    if (
        Config.BAMBU_SERIAL
        and normalized.lower().endswith(".gcode")
        and not normalized.lower().endswith(".gcode.3mf")
    ):
        if _cached_file_exists(normalized):
            return normalized
        candidate = f"{normalized}.3mf"
        if _cached_file_exists(candidate):
            return candidate
    return normalized


async def _handle_macro(macro_name: str, params: Dict[str, str]) -> Dict[str, Any]:
    if not bambu_client.connected and Config.BAMBU_SERIAL:
        return {"error": "Printer not connected"}

    if macro_name in ("PRINT_START", "START_PRINT"):
        bed_temp, error = _parse_macro_param(params, ["BED_TEMP", "BED"])
        if error:
            return {"error": error}
        hotend_temp, error = _parse_macro_param(
            params, ["HOTEND", "EXTRUDER", "EXTRUDER_TEMP"]
        )
        if error:
            return {"error": error}
        chamber_temp, error = _parse_macro_param(params, ["CHAMBER"])
        if error:
            return {"error": error}

        await bambu_client.send_gcode_line("G28 \n")

        if bed_temp is not None:
            result = await bambu_client.set_bed_temp(bed_temp)
            if "error" in result:
                return result
            await state_manager.update_state({"heater_bed": {"target": bed_temp}})
        if hotend_temp is not None:
            result = await bambu_client.set_nozzle_temp(hotend_temp)
            if "error" in result:
                return result
            await state_manager.update_state({"extruder": {"target": hotend_temp}})
        if chamber_temp is not None:
            result = await bambu_client.set_chamber_temp(chamber_temp)
            if "error" in result:
                return result
            await state_manager.update_state({"heater_chamber": {"target": chamber_temp}})

        return {"result": "ok", "action": "print_start"}

    if macro_name in ("PRINT_END", "END_PRINT"):
        result = await bambu_client.set_nozzle_temp(0)
        if "error" in result:
            return result
        await state_manager.update_state({"extruder": {"target": 0}})
        result = await bambu_client.set_bed_temp(0)
        if "error" in result:
            return result
        await state_manager.update_state({"heater_bed": {"target": 0}})
        result = await bambu_client.set_chamber_temp(0)
        if "error" in result:
            return result
        await state_manager.update_state({"heater_chamber": {"target": 0}})

        await bambu_client.send_gcode_line("M106 P1 S0 \n")
        await bambu_client.send_gcode_line("M106 P2 S0 \n")
        await bambu_client.send_gcode_line("M84 \n")
        return {"result": "ok", "action": "print_end"}

    if macro_name in ("HEATERS_OFF", "TURN_OFF_HEATERS"):
        result = await bambu_client.set_nozzle_temp(0)
        if "error" in result:
            return result
        await state_manager.update_state({"extruder": {"target": 0}})
        result = await bambu_client.set_bed_temp(0)
        if "error" in result:
            return result
        await state_manager.update_state({"heater_bed": {"target": 0}})
        result = await bambu_client.set_chamber_temp(0)
        if "error" in result:
            return result
        await state_manager.update_state({"heater_chamber": {"target": 0}})
        return {"result": "ok", "action": "heaters_off"}

    if macro_name == "PAUSE":
        await bambu_client.pause_print()
        return {"result": "ok", "action": "pause"}

    if macro_name == "RESUME":
        await bambu_client.resume_print()
        return {"result": "ok", "action": "resume"}

    if macro_name == "CANCEL_PRINT":
        await bambu_client.cancel_print()
        return {"result": "ok", "action": "cancel"}

    if macro_name == "BED_MESH_CALIBRATE":
        print("BED_MESH_CALIBRATE requested; Bambu handles leveling automatically.")
        return {"result": "ok", "action": "bed_mesh_calibrate"}

    if macro_name == "SET_EXTERNAL_FILAMENT":
        return await bambu_client.set_external_filament(params)

    if macro_name in MATERIAL_PRESETS:
        if params:
            return {"error": f"{macro_name} takes no parameters; use SET_EXTERNAL_FILAMENT for custom settings"}
        return await bambu_client.set_external_filament(dict(MATERIAL_PRESETS[macro_name]))

    if macro_name == "LOAD_EXTERNAL_FILAMENT":
        if params:
            return {"error": "LOAD_EXTERNAL_FILAMENT takes no parameters"}
        return await bambu_client.load_filament()

    if macro_name in EXTERNAL_LOAD_CHOICES:
        if params:
            return {"error": f"{macro_name} takes no parameters"}
        return await bambu_client.answer_external_extrusion(EXTERNAL_LOAD_CHOICES[macro_name])

    if macro_name == "UNLOAD_EXTERNAL_FILAMENT":
        if set(params) - {"TEMP"}:
            return {"error": "UNLOAD_EXTERNAL_FILAMENT only accepts TEMP"}
        return await bambu_client.unload_filament(params.get("TEMP") or None)

    if macro_name == "LOAD_FILAMENT":
        has_explicit_ams_params = any(
            key in params for key in ("TRAY_ID", "TRAY", "AMS_ID", "SLOT_ID", "SLOT")
        )
        if has_explicit_ams_params:
            tray = params.get("TRAY_ID") or params.get("TRAY") or params.get("SLOT") or "0"
            ams_id = params.get("AMS_ID") or "0"
            slot_id = params.get("SLOT_ID") or params.get("SLOT") or "0"
            try:
                tray_id = int(tray)
                ams_index = int(ams_id)
                slot_index = int(slot_id)
            except (TypeError, ValueError):
                return {"error": "LOAD_FILAMENT expects numeric TRAY_ID/AMS_ID/SLOT_ID"}
            result = await bambu_client.ams_load_filament(
                tray_id=tray_id,
                ams_id=ams_index,
                slot_id=slot_index,
            )
        else:
            result = await bambu_client.load_filament()
        if "error" in result:
            return result
        return {"result": "ok", "action": "load_filament"}

    if macro_name == "UNLOAD_FILAMENT":
        has_explicit_ams_params = any(key in params for key in ("AMS", "AMS_ID", "SLOT_ID", "TRAY_ID"))
        if has_explicit_ams_params:
            result = await bambu_client.ams_unload_filament()
        else:
            result = await bambu_client.unload_filament(params.get("TEMP") or None)
        if "error" in result:
            return result
        return {"result": "ok", "action": "unload_filament"}

    if macro_name == "EXCLUDE_OBJECT":
        object_ids = _extract_skip_object_ids(params)
        if not object_ids:
            return {"error": "EXCLUDE_OBJECT requires OBJECT, ID, or NAME with digits"}
        result = await bambu_client.skip_objects(object_ids)
        if "error" in result:
            return result
        return {"result": "ok", "action": "exclude_object", "objects": object_ids}

    return {"error": f"Unsupported macro: {macro_name}"}


def error_response(code: int, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=code, content={"error": {"code": code, "message": message}}
    )


def _database_request(method: str, params: Dict[str, Any]):
    if method == "server.database.list":
        return {"namespaces": database_manager.get_namespaces(), "backups": []}
    namespace = params.get("namespace")
    key = params.get("key")
    if method in {"server.database.post_item", "server.database.delete_item"}:
        if namespace in {"moonraker", "database", "authorization", "history"}:
            raise DatabaseError(403, "Namespace is reserved for internal use")
    if method == "server.database.get_item":
        value = database_manager.get_item(namespace, key, strict=True)
    elif method == "server.database.post_item":
        if "value" not in params:
            raise DatabaseError(400, "Value required")
        value = database_manager.post_item(namespace, key, params["value"])
    elif method == "server.database.delete_item":
        value = database_manager.delete_item(namespace, key)
    else:
        raise DatabaseError(404, "Unknown database method")
    return {"namespace": namespace, "key": key, "value": value}


async def _database_http(request: Request, method: str):
    try:
        params = {}
        for name, value in request.query_params.items():
            name, _, hint = name.partition(":")
            if hint == "json":
                value = json.loads(value)
            elif hint == "int":
                value = int(value)
            elif hint == "float":
                value = float(value)
            elif hint == "bool":
                if value.lower() not in {"true", "false"}:
                    raise ValueError("Invalid boolean")
                value = value.lower() == "true"
            params[name] = value
        if await request.body():
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("Request body must be an object")
            params.update(body)
        return success_response(_database_request(method, params))
    except DatabaseError as exc:
        return error_response(exc.code, str(exc))
    except (ValueError, TypeError) as exc:
        return error_response(400, str(exc))
    except Exception:
        return error_response(500, "Database operation failed")


# --- HTTP Endpoints ---


@router.get("/server/info")
async def server_info():
    return success_response(_server_info_payload(include_history=True))


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


@router.get("/webcam")
async def webcam_proxy(request: Request, action: str):
    if action == "snapshot":
        frame = await camera_manager.wait_for_frame(timeout=10.0)
        if frame is None:
            return PlainTextResponse("Camera frame unavailable", status_code=503)
        return Response(content=frame, media_type="image/jpeg")

    if action != "stream":
        return PlainTextResponse("Unsupported webcam action", status_code=400)

    if not camera_manager.is_configured:
        return PlainTextResponse("Camera streaming not configured", status_code=503)

    async def frame_generator():
        queue = await camera_manager.subscribe()
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    frame = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    continue
                yield _mjpeg_chunk(frame)
        finally:
            camera_manager.unsubscribe(queue)

    return StreamingResponse(
        frame_generator(),
        media_type=f"multipart/x-mixed-replace; boundary={camera_manager.MJPEG_BOUNDARY}",
    )


_GCODE_HELP = {
    "PAUSE": "Pause the current print", "RESUME": "Resume a paused print",
    "CANCEL_PRINT": "Cancel the current print", "SET_HEATER_TEMPERATURE": "Set a heater target",
    "SET_FAN_SPEED": "Set a fan speed", "SET_PIN": "Set the chamber light",
    "PRINT_START": "Home and set print temperatures", "PRINT_END": "Turn off heaters, fans, and motors",
    "LOAD_FILAMENT": "Load filament", "UNLOAD_FILAMENT": "Unload filament",
    "EXCLUDE_OBJECT": "Skip an object by its numeric identifier",
    **{name: definition["description"] for name, definition in EXTERNAL_MACROS.items()},
}


@router.get("/printer/gcode/help")
async def gcode_help():
    return success_response(_GCODE_HELP)


@router.get("/server/history/list")
async def history_list(limit: int = 50, start: int = 0, before: Optional[int] = None,
                       since: Optional[int] = None, order: str = "desc"):
    try:
        return success_response(get_sqlite_manager().get_job_history(limit, before, since, order, start))
    except ValueError as exc:
        return error_response(400, str(exc))


@router.get("/server/history/totals")
async def history_totals():
    return success_response({"job_totals": get_sqlite_manager().get_job_totals()})


@router.get("/server/temperature_store")
async def http_temperature_store(include_monitors: bool = False):
    data = state_manager.get_temperature_history(include_monitors)
    return success_response(data)


@router.get("/printer/objects/list")
async def objects_list():
    # Return keys from our observable state
    keys = list(state_manager.get_state().keys())
    return success_response({"objects": keys})


def _query_objects(objects):
    if not isinstance(objects, dict):
        raise ValueError("Objects must be an object")
    current_state = state_manager.get_state()
    status = {}
    for name, fields in objects.items():
        if fields is not None and (not isinstance(fields, list) or any(not isinstance(f, str) for f in fields)):
            raise ValueError("Object fields must be an array of strings or null")
        if name in current_state:
            status[name] = current_state[name] if fields is None else {
                field: current_state[name][field] for field in fields if field in current_state[name]
            }
    return {"status": status, "eventtime": time.monotonic()}


@router.get("/printer/objects/query")
async def objects_query(request: Request):
    try:
        params = request.query_params
        if "objects:json" in params or "objects" in params:
            objects = json.loads(params.get("objects:json", params.get("objects")))
        else:
            objects = {name: fields.split(",") if fields else None for name, fields in params.items()}
        return success_response(_query_objects(objects))
    except (ValueError, TypeError) as exc:
        return error_response(400, str(exc))


@router.get("/server/files/list")
async def file_list(root: str = "gcodes"):
    try:
        files = await asyncio.to_thread(_build_file_list, root)
        return success_response(files)
    except Exception as e:
        print(f"Error listing files: {e}")
        # Return empty list on error rather than failing
        return success_response([])


@router.get("/server/files/roots")
async def file_roots():
    return success_response(_file_roots_payload())


@router.get("/server/files/directory")
async def get_directory(path: str = "gcodes", extended: bool = False):
    """
    Get directory contents with caching support.
    Used by Mainsail's file browser.
    """
    sqlite_manager = get_sqlite_manager()

    if path == "config" or path == "config/.theme":
        return _config_directory_listing(path)

    if not Config.BAMBU_SERIAL and path == "gcodes":
        return await _directory_metadata(_mock_directory_listing(path), path, extended)
    
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
            if _cache_has_non_root_entries(cached_files):
                cached_files = None
    
    if cached_files is None:
        # Cache miss - fetch from FTPS
        print(f"Fetching files from FTPS for path: {ftps_path} (requested: {path})")
        try:
            remote_files = await asyncio.to_thread(ftps_client.list_files, ftps_path)
            # Only cache the root listing for now
            if path == "gcodes":
                sqlite_manager.clear_file_cache()
                sqlite_manager.cache_files(remote_files)
            cached_files = remote_files
        except Exception as e:
            print(f"Error fetching files from FTPS: {e}")
            cached_files = []
    
    # Transform to Moonraker format
    dirs = []
    files = []
    
    for f in cached_files:
        if path == "gcodes" and not _is_root_direct_child(f):
            continue
        if f["is_dir"]:
            if not _include_gcodes_directory_entry(path, f["name"]):
                continue
            dirs.append({
                "dirname": f["name"],
                "modified": f["modified"],
                "size": f["size"],
                "permissions": "rw",
                "path": _directory_entry_path(path, f["name"]),
            })
        else:
            if not _include_gcodes_file_entry(path, f["name"]):
                continue
            display_name = f["name"]
            files.append({
                "filename": display_name,
                "modified": f["modified"],
                "size": f["size"],
                "permissions": "rw",
                "path": _directory_entry_path(path, display_name),
            })
    
    result = {
        "dirs": dirs,
        "files": files,
        "disk_usage": await asyncio.to_thread(_get_disk_usage, "gcodes"),
        "root_info": {
            "name": "gcodes",
            "permissions": "rw",
            "path": "gcodes",
        }
    }
    
    return success_response(await _directory_metadata(result, path, extended))


@router.post("/server/files/upload")
async def file_upload(
    request: Request,
    file: UploadFile = File(...),
    path: Optional[str] = Form(None),
    start_print: bool = Form(False, alias="print"),
    plate: int = Form(1),
    root: str = Form("gcodes"),
):
    try:
        if root != "gcodes":
            return error_response(400, "Only gcodes uploads are supported")
        if "print" in request.query_params:
            raw = request.query_params["print"].lower()
            if raw not in {"true", "false", "1", "0"}:
                return error_response(400, "Invalid print flag")
            start_print = raw in {"true", "1"}
        if "plate" in request.query_params:
            plate = int(request.query_params["plate"])
        if plate < 1:
            return error_response(400, "Plate must be positive")
        if path or request.query_params.get("path"):
            return error_response(400, "Subdirectory uploads are not supported")
        if not Config.BAMBU_SERIAL:
            _ensure_mock_file_exists()
            target_name = file.filename or "upload.gcode"
            target_path = os.path.join(Config.GCODES_DIR, os.path.basename(target_name))
            content = await file.read()
            with open(target_path, "wb") as fp:
                fp.write(content)
            await asyncio.to_thread(get_sqlite_manager().invalidate_parsed_metadata, os.path.basename(target_path))

            print_started = False
            if start_print:
                result = await bambu_client.start_print(
                    filename=os.path.basename(target_path),
                    plate_number=plate,
                    bed_leveling=True,
                )
                print_started = "error" not in result

            return success_response(
                {
                    "item": {
                        "path": f"gcodes/{os.path.basename(target_path)}",
                        "size": len(content),
                        "modified": time.time(),
                    },
                    "print_started": print_started,
                }
            )

        # Save uploaded file to a temp location first
        suffix = os.path.splitext(file.filename or "")[1] or ".gcode"
        temp_fd, temp_path = tempfile.mkstemp(suffix=suffix)
        try:
            # Write the uploaded file to temp
            with os.fdopen(temp_fd, 'wb') as tmp:
                content = await file.read()
                tmp.write(content)
            
            # Upload to printer via FTPS (offload blocking FTP call)
            await asyncio.to_thread(
                ftps_client.upload_file,
                temp_path,
                file.filename,
            )
            
            # Invalidate file cache so next list is fresh
            sqlite_manager = get_sqlite_manager()
            sqlite_manager.clear_file_cache()
            await asyncio.to_thread(sqlite_manager.invalidate_parsed_metadata, file.filename)
            
            # Get file size
            file_size = len(content)

            print_started = False
            if start_print:
                result = await bambu_client.start_print(
                    filename=file.filename,
                    plate_number=plate,
                    bed_leveling=True,
                )
                if "error" not in result:
                    print_started = True
                else:
                    print(f"Auto-start failed: {result['error']}")

            return success_response({
                "item": {
                    "path": f"gcodes/{file.filename}",
                    "size": file_size,
                    "modified": time.time()
                },
                "print_started": print_started
            })
        finally:
            # Clean up temp file
            if os.path.exists(temp_path):
                os.unlink(temp_path)
    except Exception as e:
        print(f"Upload error: {e}")
        return error_response(500, f"Upload failed: {str(e)}")




class FileOperationError(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _validate_delete_path(path):
    if not isinstance(path, str) or not path or any(char in path for char in ("\\", "\r", "\n", "\x00")):
        raise FileOperationError(400, "A valid file path is required")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts) or len(parts) < 2:
        raise FileOperationError(400, "Expected root/filename without traversal")
    if parts[0] != "gcodes":
        raise FileOperationError(403, "Only gcodes files can be deleted")
    return "/".join(parts[1:])


def _resolve_remote_delete(filename):
    # The old browser aliases foo.gcode.3mf as foo.gcode. Resolve against a
    # fresh parent listing, preferring an exact file over a legacy alias.
    parent, basename = posixpath.split(filename)
    remote_parent = posixpath.join(Config.BAMBU_FTPS_UPLOADS_DIR, parent)
    entries = ftps_client.list_files(remote_parent)
    files = {posixpath.basename(entry["name"]) for entry in entries if not entry.get("is_dir")}
    if basename in files:
        return filename
    aliases = [name for name in files if _legacy_display_filename(name) == basename]
    if len(aliases) == 1:
        return posixpath.join(parent, aliases[0])
    if len(aliases) > 1:
        raise FileOperationError(409, "Ambiguous file alias; use the full filename")
    raise FileOperationError(404, "File not found")


async def _delete_gcode_file(path):
    filename = _validate_delete_path(path)
    if state_manager.get_state().get("print_stats", {}).get("state") in {"printing", "paused"}:
        raise FileOperationError(409, "File deletion is unavailable while printing or paused")
    try:
        if not Config.BAMBU_SERIAL:
            root = os.path.realpath(Config.GCODES_DIR)
            local = os.path.join(root, filename)
            if os.path.commonpath([root, os.path.realpath(local)]) != root:
                raise FileOperationError(400, "File path escapes the gcodes root")
            if os.path.isdir(local):
                raise FileOperationError(400, "Path is a directory, not a file")
            await asyncio.to_thread(os.unlink, local)
        else:
            target = await asyncio.to_thread(_resolve_remote_delete, filename)
            await asyncio.to_thread(ftps_client.delete_file, target)
    except FileOperationError:
        raise
    except FileNotFoundError:
        raise FileOperationError(404, "File not found")
    except IsADirectoryError:
        raise FileOperationError(400, "Path is a directory, not a file")
    except PermissionError:
        raise FileOperationError(403, "File deletion denied")
    except Exception as exc:
        raise FileOperationError(502, f"File deletion failed: {exc}")
    get_sqlite_manager().clear_file_cache()
    await asyncio.to_thread(get_sqlite_manager().invalidate_parsed_metadata, filename)
    if Config.BAMBU_SERIAL and target != filename:
        await asyncio.to_thread(get_sqlite_manager().invalidate_parsed_metadata, target)
    result = {"item": {"path": filename, "root": "gcodes", "size": 0, "modified": 0, "permissions": ""},
              "action": "delete_file"}
    await manager.broadcast({"jsonrpc": "2.0", "method": "notify_filelist_changed", "params": [result]})
    return result


@router.delete("/server/files/{root}/{filename:path}")
async def file_delete(filename: str, root: str = "gcodes"):
    try:
        return success_response(await _delete_gcode_file(f"{root}/{filename}"))
    except FileOperationError as exc:
        return error_response(exc.code, str(exc))


@router.get("/server/files/{root}/{path:path}")
async def file_download(root: str, path: str):
    if root == "config":
        if path.startswith(".theme/"):
            theme_name = path.split("/", 1)[1]
            content = _CONFIG_THEME_FILES.get(theme_name)
            if content is not None:
                return Response(content=content, media_type="application/json")
        content = CONFIG_FILES.get(path)
        if content is not None:
            return PlainTextResponse(content)
    if root == "gcodes":
        if re.fullmatch(r'(?:[^/]+/)*\.thumbs/[a-f0-9]{64}\.(?:png|jpg)', path):
            key = posixpath.basename(path)
            payload = await asyncio.to_thread(get_sqlite_manager().get_thumbnail, key)
            if payload is None:
                return error_response(404, "Thumbnail not found")
            return Response(content=payload, media_type="image/png" if key.endswith('.png') else "image/jpeg",
                            headers={"Cache-Control": "public, max-age=31536000, immutable"})
        if not Config.BAMBU_SERIAL:
            local_name = os.path.basename(path)
            local_path = os.path.join(Config.GCODES_DIR, local_name)
            if not os.path.exists(local_path):
                return error_response(404, "File not found")
            with open(local_path, "rb") as fp:
                payload = fp.read()
            return Response(
                content=payload,
                media_type="application/octet-stream",
                headers={"Content-Disposition": f'attachment; filename="{local_name}"'},
            )
        try:
            target_path = _normalize_filename(path)
            payload = await asyncio.to_thread(ftps_client.download_file, target_path)
        except FileNotFoundError:
            return error_response(404, "File not found")
        except Exception as exc:
            print(f"Download error ({path}): {exc}")
            return error_response(500, "File download failed")
        return Response(
            content=payload,
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{os.path.basename(path)}"'},
        )

    # Generic 404 for now unless checked against real files
    return error_response(404, "File not found")


@router.get("/server/files/metadata")
async def file_metadata(filename: str, plate: Optional[int] = None):
    try:
        return success_response(await _read_file_metadata(filename, plate))
    except MetadataError as exc:
        return error_response(exc.code, str(exc))


async def _read_file_metadata(filename, plate=None, force=False):
    filename = _normalize_filename(validate_filename(filename))
    return await metadata_service.get(filename, plate, force, ftps_client, get_sqlite_manager())


async def _directory_metadata(result, path, extended):
    if not extended or not (path == 'gcodes' or path.startswith('gcodes/')):
        return result
    parent = path[7:] if path.startswith('gcodes/') else ''
    async def enrich(item):
        if not item['filename'].lower().endswith(_GCODE_FILE_SUFFIXES):
            return item
        try:
            metadata = await _read_file_metadata(posixpath.join(parent, item['filename']))
            return {**item, **{key: value for key, value in metadata.items() if key != 'filename'}}
        except MetadataError as exc:
            print(f'Metadata unavailable for {item["filename"]}: {exc}')
            return item
    result['files'] = await asyncio.gather(*(enrich(item) for item in result['files']))
    return result


def _thumbnail_details(metadata):
    parent = posixpath.dirname(metadata['filename'])
    return [{**{k: v for k, v in thumbnail.items() if k != 'relative_path'},
             'thumbnail_path': posixpath.join(parent, thumbnail['relative_path'])}
            for thumbnail in metadata['thumbnails']]


@router.get("/server/files/thumbnails")
async def file_thumbnails(filename: str, plate: Optional[int] = None):
    try:
        return success_response(_thumbnail_details(await _read_file_metadata(filename, plate)))
    except MetadataError as exc:
        return error_response(exc.code, str(exc))


@router.post("/server/files/metascan")
async def file_metascan(request: Request):
    try:
        params = dict(request.query_params)
        if request.headers.get('content-type', '').startswith('application/json'):
            params.update(await request.json())
        plate = int(params['plate']) if params.get('plate') is not None else None
        return success_response(await _read_file_metadata(params.get('filename'), plate, True))
    except MetadataError as exc:
        return error_response(exc.code, str(exc))
    except (ValueError, TypeError):
        return error_response(400, 'Invalid metadata scan parameters')


@router.get("/server/database/item")
async def database_get(request: Request):
    return await _database_http(request, "server.database.get_item")


@router.post("/server/database/item")
async def database_post(request: Request):
    return await _database_http(request, "server.database.post_item")


@router.delete("/server/database/item")
async def database_delete(request: Request):
    return await _database_http(request, "server.database.delete_item")


@router.get("/server/database/list")
async def database_list():
    return success_response(_database_request("server.database.list", {}))


@router.post("/server/jsonrpc")
async def http_jsonrpc(request: Request):
    try:
        body = await request.json()
    except ValueError:
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
    response = await handle_jsonrpc(body, 0)
    return response if response is not None else Response(status_code=204)


@router.post("/printer/print/start")
async def print_start(request: Request):
    try:
        body = await request.json()
        filename = body.get("filename")
        if not filename:
            return error_response(400, "Filename required")
        filename = _normalize_filename(filename)
        plate = body.get("plate", 1)
        use_ams = bool(body.get("use_ams", False))
        bed_leveling = bool(body.get("bed_leveling", body.get("auto_bed_leveling", True)))
        flow_calibration = bool(body.get("flow_calibration", body.get("flow_cali", False)))
        timelapse = bool(body.get("timelapse", False))
        vibration_cali = bool(body.get("vibration_cali", True))
        layer_inspect = bool(body.get("layer_inspect", False))
        cfg = str(body.get("cfg", ""))
        extrude_cali_flag = bool(body.get("extrude_cali_flag", False))
        ams_mapping = body.get("ams_mapping")
        ams_mapping2 = body.get("ams_mapping2")
        print(f"Requested start print: {filename}")
        result = await bambu_client.start_print(
            filename=filename,
            plate_number=plate,
            use_ams=use_ams,
            bed_leveling=bed_leveling,
            flow_calibration=flow_calibration,
            timelapse=timelapse,
            vibration_cali=vibration_cali,
            layer_inspect=layer_inspect,
            cfg=cfg,
            extrude_cali_flag=extrude_cali_flag,
            ams_mapping=ams_mapping,
            ams_mapping2=ams_mapping2,
        )
        if "error" in result:
            return error_response(500, result["error"])
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
            await asyncio.sleep(2)  # 2s interval to beat Mainsail's 10s watchdog
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
        self.start()
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
            except Exception:
                self.disconnect(connection)


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
            try:
                data = await websocket.receive_json()
            except ValueError:
                await websocket.send_json({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
                continue
            # Process JSON-RPC request
            response = await handle_jsonrpc(data, connection_id)
            if response:
                await websocket.send_json(response)
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        print(f"WS Error: {e}")
        manager.disconnect(websocket)


async def handle_jsonrpc(request, connection_id: int):
    if isinstance(request, list):
        if not request:
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request"}}
        responses = [await handle_jsonrpc(item, connection_id) for item in request]
        return [response for response in responses if response is not None] or None
    if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request"}}
    response = {"jsonrpc": "2.0", "id": request.get("id")}
    try:
        params = request.get("params", {})
        if not isinstance(params, dict):
            response["error"] = {"code": -32602, "message": "Params must be an object"}
        elif request["method"].startswith("server.database."):
            response["result"] = _database_request(request["method"], params)
        else:
            response = await _dispatch_jsonrpc(request, connection_id)
    except (DatabaseError, FileOperationError, MetadataError) as exc:
        response["error"] = {"code": exc.code, "message": str(exc)}
    except (ValueError, TypeError, KeyError) as exc:
        response["error"] = {"code": -32602, "message": str(exc)}
    except Exception as exc:
        print(f"RPC operation failed: {type(exc).__name__}: {exc}")
        response["error"] = {"code": -32603, "message": "Internal error"}
    return response if "id" in request else None


async def _dispatch_jsonrpc(
    request: Dict[str, Any], connection_id: int
) -> Optional[Dict[str, Any]]:
    req_id = request.get("id")
    method = request.get("method")

    print(f"RPC Request: {method} params={request.get('params')}")

    response = {"jsonrpc": "2.0", "id": req_id}

    if method == "server.info":
        response["result"] = _server_info_payload(include_history=True)
    elif method == "printer.objects.list":
        keys = list(state_manager.get_state().keys())
        response["result"] = {"objects": keys}
    elif method in {"printer.objects.query", "printer.objects.subscribe"}:
        response["result"] = _query_objects(request.get("params", {}).get("objects", {}))

    elif method == "server.temperature_store":
        include_monitors = request.get("params", {}).get("include_monitors", False)
        response["result"] = state_manager.get_temperature_history(include_monitors)

    elif method in {"server.files.metadata", "server.files.metascan", "server.files.thumbnails"}:
        params = request.get("params", {})
        response["result"] = await _read_file_metadata(params.get("filename"), params.get('plate'), method.endswith('metascan'))
        if method.endswith('thumbnails'):
            response['result'] = _thumbnail_details(response['result'])

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

    elif method == "printer.gcode.help":
        response["result"] = _GCODE_HELP

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
        response["result"] = {"webcams": _current_webcams()}

    elif method == "server.webcams.post_item":
        params = request.get("params", {})
        if "name" in params and (not isinstance(params["name"], str) or not params["name"].strip()):
            raise DatabaseError(400, "Webcam name must be a nonempty string")
        builtin_webcam = camera_manager.get_builtin_webcam()
        # Load existing
        webcams = database_manager.get_item("moonraker", "webcams") or []

        uid = params.get("uid")
        target_cam = None
        if uid is not None and (not isinstance(uid, str) or not uid.strip()):
            raise DatabaseError(400, "Webcam UID must be a nonempty string")

        # Check if updating existing
        if uid:
            for cam in webcams:
                if cam["uid"] == uid:
                    target_cam = cam
                    break
        elif isinstance(params.get("name"), str):
            matches = [cam for cam in _current_webcams() if cam.get("name") == params["name"]]
            if len(matches) > 1:
                raise DatabaseError(409, "Multiple webcams have this name; update by UID or remove the duplicates first")
            if matches:
                uid = matches[0]["uid"]
                target_cam = next((cam for cam in webcams if cam.get("uid") == uid), None)

        if target_cam is None and builtin_webcam and uid == builtin_webcam["uid"]:
            target_cam = dict(builtin_webcam)
            webcams.append(target_cam)

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
            if not isinstance(params.get("name"), str) or not params["name"].strip():
                raise DatabaseError(400, "A nonempty name is required for a new webcam")
            uid = uid or str(uuid.uuid4())
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

        if builtin_webcam and uid == builtin_webcam["uid"]:
            for field in ("uid", "stream_url", "snapshot_url", "service"):
                new_cam[field] = builtin_webcam[field]
        database_manager.post_item("moonraker", "webcams", webcams)

        await notify_webcams_changed()
        response["result"] = {"item": new_cam}

    elif method == "server.webcams.delete_item":
        params = request.get("params", {})
        uid = params.get("uid")
        name = params.get("name")
        if uid is not None:
            if not isinstance(uid, str) or not uid.strip():
                raise DatabaseError(400, "Webcam UID must be a nonempty string")
            matches = lambda cam: cam.get("uid") == uid
        elif isinstance(name, str) and name.strip():
            # Older clients address cameras by name. Remove all legacy duplicates
            # with that identity; UID requests always remove just that UID.
            matches = lambda cam: cam.get("name") == name
        else:
            raise DatabaseError(400, "Webcam UID or name required")
        builtin_webcam = camera_manager.get_builtin_webcam()
        builtin = next((cam for cam in _current_webcams()
                        if builtin_webcam and cam.get("uid") == builtin_webcam["uid"]), None)
        if builtin and matches(builtin):
            raise DatabaseError(403, "The built-in Bambu Camera cannot be deleted; disable it instead")
        webcams = database_manager.get_item("moonraker", "webcams") or []

        # Filter out the one to delete
        removed = [cam for cam in webcams if matches(cam)]
        new_list = [cam for cam in webcams if not matches(cam)]

        if len(new_list) < len(webcams):
            database_manager.post_item("moonraker", "webcams", new_list)
            await notify_webcams_changed()
            response["result"] = {"item": removed[0]}
        else:
            # Item not found, but successful deletion (idempotent)
            response["result"] = {"item": {"uid": uid} if uid is not None else {"name": name}}

    elif method == "server.webcams.test":
        response["result"] = {"can_stream": camera_manager.get_builtin_webcam() is not None}

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

    elif method == "printer.gcode.script":
        script = request.get("params", {}).get("script", "")
        # Mainsail functionality often depends on this returning successfully
        # We split by newlines and send each as a separate command
        lines = script.split("\n")
        handled_heater_targets: set[tuple[str, int]] = set()
        for line in lines:
            line = line.strip()
            if not line:
                continue

            if _is_macro_command(line):
                macro_name, params = _parse_macro(line)
                result = await _handle_macro(macro_name, params)
                if "error" in result:
                    response["error"] = {"code": 400, "message": result["error"]}
                    return response
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

            # Intercept heater commands (M104/M109/M140/M190/M141/M191).
            if line.upper().startswith(("M104", "M109", "M140", "M190", "M141", "M191")):
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
                        heater_object = "extruder"
                    elif cmd in ("M140", "M190"):
                        heater_object = "heater_bed"
                    elif cmd in ("M141", "M191"):
                        heater_object = "heater_chamber"
                    else:
                        heater_object = ""

                    dedupe_key = (heater_object, int(round(temp)))
                    if dedupe_key in handled_heater_targets:
                        print(f"Skipping duplicate heater command in script: {line}")
                        continue

                    if cmd in ("M104", "M109"):
                        result = await bambu_client.set_nozzle_temp(temp)
                    elif cmd in ("M140", "M190"):
                        result = await bambu_client.set_bed_temp(temp)
                    elif cmd in ("M141", "M191"):
                        result = await bambu_client.set_chamber_temp(temp)
                    else:
                        result = {"error": f"Unsupported heater command: {cmd}"}

                    if "error" in result:
                        response["error"] = {"code": 400, "message": result["error"]}
                        return response
                    handled_heater_targets.add(dedupe_key)
                    if cmd in ("M104", "M109"):
                        await state_manager.update_state({"extruder": {"target": temp}})
                    elif cmd in ("M140", "M190"):
                        await state_manager.update_state({"heater_bed": {"target": temp}})
                    elif cmd in ("M141", "M191"):
                        await state_manager.update_state({"heater_chamber": {"target": temp}})

                    continue
                except Exception as e:
                    print(f"Heater parse error: {e}")

            # Intercept print speed scaling and map to Bambu speed modes.
            if line.upper().startswith("M220"):
                try:
                    parts = line.upper().split()
                    speed_percent = None
                    for part in parts[1:]:
                        if part.startswith("S"):
                            speed_percent = float(part[1:])
                            break
                    if speed_percent is None:
                        response["error"] = {
                            "code": 400,
                            "message": f"Missing S parameter in print speed command: {line}",
                        }
                        return response
                    mode = _m220_percent_to_mode(speed_percent)
                    result = await bambu_client.set_print_speed(mode)
                    if "error" in result:
                        response["error"] = {"code": 400, "message": result["error"]}
                        return response
                    continue
                except Exception as e:
                    print(f"Print speed parse error: {e}")

            # Intercept SET_HEATER_TEMPERATURE for Moonraker compatibility
            # Format: SET_HEATER_TEMPERATURE HEATER=<name> TARGET=<value>
            if line.upper().startswith("SET_HEATER_TEMPERATURE"):
                try:
                    parts = line.split()
                    heater_name = None
                    target = None
                    for part in parts:
                        upper = part.upper()
                        if upper.startswith("HEATER="):
                            heater_name = part.split("=", 1)[1]
                        elif upper.startswith("TARGET="):
                            target = float(part.split("=", 1)[1])

                    if heater_name is None or target is None:
                        response["error"] = {
                            "code": 400,
                            "message": f"Invalid SET_HEATER_TEMPERATURE command: {line}",
                        }
                        return response

                    heater_name = heater_name.lower()
                    if heater_name == "extruder":
                        heater_object = "extruder"
                    elif heater_name in ("heater_bed", "bed"):
                        heater_object = "heater_bed"
                    elif heater_name in ("heater_chamber", "chamber"):
                        heater_object = "heater_chamber"
                    else:
                        heater_object = ""

                    dedupe_key = (heater_object, int(round(target)))
                    if dedupe_key in handled_heater_targets:
                        print(f"Skipping duplicate heater command in script: {line}")
                        continue

                    if heater_name == "extruder":
                        result = await bambu_client.set_nozzle_temp(target)
                    elif heater_name in ("heater_bed", "bed"):
                        result = await bambu_client.set_bed_temp(target)
                    elif heater_name in ("heater_chamber", "chamber"):
                        result = await bambu_client.set_chamber_temp(target)
                    else:
                        result = {"error": f"Unknown heater: {heater_name}"}

                    if "error" in result:
                        response["error"] = {"code": 400, "message": result["error"]}
                        return response
                    handled_heater_targets.add(dedupe_key)
                    if heater_name == "extruder":
                        await state_manager.update_state({"extruder": {"target": target}})
                    elif heater_name in ("heater_bed", "bed"):
                        await state_manager.update_state({"heater_bed": {"target": target}})
                    elif heater_name in ("heater_chamber", "chamber"):
                        await state_manager.update_state({"heater_chamber": {"target": target}})
                    continue
                except Exception as e:
                    print(f"SET_HEATER_TEMPERATURE parse error: {e}")

            # Basic logging for now
            print(f"Executing G-code: {line}")
            await bambu_client.send_gcode_line(line)
        response["result"] = "ok"

    elif method == "printer.print.start":
        params = request.get("params", {})
        filename = params.get("filename")
        if not filename:
            response["error"] = {"code": 400, "message": "Filename required"}
            return response
        filename = _normalize_filename(filename)
        plate = params.get("plate", 1)
        use_ams = bool(params.get("use_ams", False))
        bed_leveling = bool(params.get("bed_leveling", params.get("auto_bed_leveling", True)))
        flow_calibration = bool(params.get("flow_calibration", params.get("flow_cali", False)))
        timelapse = bool(params.get("timelapse", False))
        vibration_cali = bool(params.get("vibration_cali", True))
        layer_inspect = bool(params.get("layer_inspect", False))
        cfg = str(params.get("cfg", ""))
        extrude_cali_flag = bool(params.get("extrude_cali_flag", False))
        ams_mapping = params.get("ams_mapping")
        ams_mapping2 = params.get("ams_mapping2")
        result = await bambu_client.start_print(
            filename=filename,
            plate_number=plate,
            use_ams=use_ams,
            bed_leveling=bed_leveling,
            flow_calibration=flow_calibration,
            timelapse=timelapse,
            vibration_cali=vibration_cali,
            layer_inspect=layer_inspect,
            cfg=cfg,
            extrude_cali_flag=extrude_cali_flag,
            ams_mapping=ams_mapping,
            ams_mapping2=ams_mapping2,
        )
        if "error" in result:
            response["error"] = {"code": 500, "message": result["error"]}
        else:
            response["result"] = "ok"

    elif method in ("printer.print.pause", "printer.pause"):
        result = await bambu_client.pause_print()
        if isinstance(result, dict) and "error" in result:
            response["error"] = {"code": 500, "message": result["error"]}
        else:
            current_filename = state_manager.get_state().get("print_stats", {}).get("filename", "")
            await state_manager.update_state(
                {
                    "print_stats": {"state": "paused", "filename": current_filename},
                    "virtual_sdcard": {"is_active": False},
                }
            )
            response["result"] = "ok"

    elif method in ("printer.print.resume", "printer.resume"):
        result = await bambu_client.resume_print()
        if isinstance(result, dict) and "error" in result:
            response["error"] = {"code": 500, "message": result["error"]}
        else:
            current_filename = state_manager.get_state().get("print_stats", {}).get("filename", "")
            await state_manager.update_state(
                {
                    "print_stats": {"state": "printing", "filename": current_filename},
                    "virtual_sdcard": {"is_active": True},
                }
            )
            response["result"] = "ok"

    elif method in ("printer.print.cancel", "printer.cancel"):
        result = await bambu_client.cancel_print()
        if isinstance(result, dict) and "error" in result:
            response["error"] = {"code": 500, "message": result["error"]}
        else:
            current_filename = state_manager.get_state().get("print_stats", {}).get("filename", "")
            await state_manager.update_state(
                {
                    "print_stats": {"state": "cancelled", "filename": current_filename},
                    "virtual_sdcard": {"is_active": False},
                }
            )
            response["result"] = "ok"

    elif method in ("printer.print.set_speed", "printer.print.speed"):
        params = request.get("params", {})
        mode = params.get("mode")
        result = await bambu_client.set_print_speed(mode)
        if "error" in result:
            response["error"] = {"code": 400, "message": result["error"]}
        else:
            response["result"] = "ok"

    elif method in ("printer.exclude_object", "printer.print.exclude_object"):
        params = request.get("params", {})
        object_ids = params.get("object_ids") or params.get("obj_list") or []
        if not object_ids and "object_id" in params:
            object_ids = [params.get("object_id")]
        if not object_ids and "id" in params:
            object_ids = [params.get("id")]
        result = await bambu_client.skip_objects(object_ids)
        if "error" in result:
            response["error"] = {"code": 400, "message": result["error"]}
        else:
            response["result"] = "ok"

    elif method == "server.files.delete_file":
        response["result"] = await _delete_gcode_file(request.get("params", {}).get("path"))

    elif method == "server.files.roots":
        response["result"] = _file_roots_payload()

    elif method == "server.files.list":
        params = request.get("params", {})
        root = params.get("root", "gcodes")
        try:
            files = await asyncio.to_thread(_build_file_list, root)
            response["result"] = files
        except Exception as e:
            print(f"Error listing files: {e}")
            response["result"] = []

    elif method == "server.files.get_directory":
        # Get file listing with caching
        params = request.get("params", {})
        path = params.get("path", "gcodes")

        if path == "config" or path == "config/.theme":
            response["result"] = _config_directory_listing(path)
            return response

        if not Config.BAMBU_SERIAL and path == "gcodes":
            response["result"] = await _directory_metadata(_mock_directory_listing(path), path, params.get('extended', False))
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
             if _cache_has_non_root_entries(cached_files):
                 cached_files = None
        
        if cached_files is None:
            # Cache miss - fetch from FTPS
            print(f"Fetching files from FTPS for path: {ftps_path} (requested: {path})")
            try:
                remote_files = await asyncio.to_thread(ftps_client.list_files, ftps_path)
                # Only cache the root listing for now to avoid complexity
                if path == "gcodes":
                    sqlite_manager.clear_file_cache()
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
            if path == "gcodes" and not _is_root_direct_child(f):
                continue
            if f["is_dir"]:
                if not _include_gcodes_directory_entry(path, f["name"]):
                    continue
                dirs.append({
                    "dirname": f["name"],
                    "modified": f["modified"],
                    "size": f["size"],
                    "permissions": "rw",
                    "path": _directory_entry_path(path, f["name"]),
                })
            else:
                if not _include_gcodes_file_entry(path, f["name"]):
                    continue
                display_name = f["name"]
                files.append({
                    "filename": display_name,
                    "modified": f["modified"],
                    "size": f["size"],
                    "permissions": "rw",
                    "path": _directory_entry_path(path, display_name),
                })
        
        response["result"] = {
            "dirs": dirs,
            "files": files,
            "disk_usage": await asyncio.to_thread(_get_disk_usage, "gcodes"),
            "root_info": {
                "name": "gcodes", # Always the root name, even for subdirs
                "permissions": "rw",
                "path": "gcodes",
            }
        }
        response['result'] = await _directory_metadata(response['result'], path, params.get('extended', False))

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
            order=order,
            start=start
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
        response["error"] = {"code": -32601, "message": f"Method not found: {method}"}

    print(f"RPC Response: {json.dumps(response)}")
    return response


async def notify_webcams_changed():
    webcams = _current_webcams()
    await manager.broadcast(
        {
            "jsonrpc": "2.0",
            "method": "notify_webcams_changed",
            "params": [{"webcams": webcams}],
        }
    )
