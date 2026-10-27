import asyncio
import json
import os
import ssl
import time
from typing import Optional, Dict, Any, List
import aiomqtt
from bambu_moonraker_shim.config import Config
from bambu_moonraker_shim.state_manager import state_manager
from bambu_moonraker_shim.external_spool import filament_settings, EXTRUSION_PROMPT_ERRORS

class BambuClient:
    def __init__(self):
        self.host = Config.BAMBU_HOST
        self.access_code = Config.BAMBU_ACCESS_CODE
        self.serial = Config.BAMBU_SERIAL
        self.user_id = str(Config.BAMBU_USER_ID or "")
        self.connected = False
        self._mock_mode = False
        self._mqtt_client: Optional[aiomqtt.Client] = None
        self._loop_task: Optional[asyncio.Task] = None
        self._sequence_id = 0
        self._prefer_qos0_for_print = False
        self._local_targets: Dict[str, Dict[str, Any]] = {
            "extruder": {"target": None, "set_time": 0.0},
            "heater_bed": {"target": None, "set_time": 0.0},
            "heater_chamber": {"target": None, "set_time": 0.0},
        }
        self._local_target_max_age_seconds = 20 * 60
        self._temperature_probe_window_seconds = 12.0
        self._preferred_temp_variant_index: Dict[str, int] = {
            "extruder": 0,
            "bed": 0,
            "chamber": 0,
        }
        self._pending_temp_commands: Dict[str, Dict[str, Any]] = {
            "extruder": {"target": None, "set_time": 0.0, "variant_index": 0, "fallback_sent": False},
            "bed": {"target": None, "set_time": 0.0, "variant_index": 0, "fallback_sent": False},
            "chamber": {"target": None, "set_time": 0.0, "variant_index": 0, "fallback_sent": False},
        }
        self._mock_target_nozzle = 0.0
        self._mock_current_nozzle = 20.0
        self._mock_target_bed = 0.0
        self._mock_current_bed = 20.0
        self._mock_target_chamber = 0.0
        self._mock_current_chamber = 20.0
        self._mock_progress = 0.0
        self._mock_state = "standby"
        self._mock_filename = "mock_file.gcode"
        self._job_finished_at: Optional[float] = None
        self._job_started_at: Optional[float] = None
        self._job_paused_at: Optional[float] = None
        self._job_paused_seconds = 0.0
        self._active_job_filename = ""
        self._active_gcode_file = ""
        self._latest_remaining_time = 0
        self._latest_layer_num = 0
        self._latest_total_layer_num = 0
        self._reported_print_state: Optional[str] = None
        self._external_settings: Dict[str, Any] = {}
        self._external_reported: Dict[str, Any] = {}
        self._external_pending: Optional[Dict[str, Any]] = None
        self._external_setting_sequence: Optional[str] = None
        self._external_rejected: Optional[str] = None
        self._external_tray_now: Optional[int] = None
        self._external_tray_target: Optional[int] = None
        self._multiple_external_spools = False
        self._external_extrusion_prompt = False

    async def start(self):
        """Starts the MQTT loop."""
        if self._loop_task and not self._loop_task.done():
            return
        await self.restore_external_filament()
        if not self.serial:
             print("Warning: BAMBU_SERIAL not set. Running in mock mode.")
             self._mock_mode = True
             self.connected = True
             self._loop_task = asyncio.create_task(self._mock_loop())
             return

        self._loop_task = asyncio.create_task(self._connect_loop())

    async def stop(self):
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            self._loop_task = None
        self.connected = False
        self._mqtt_client = None

    async def _connect_loop(self):
        while True:
            try:
                print(f"Connecting to Bambu Printer at {self.host}...")
                
                # TLS context is required for Bambu
                tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                tls_context.check_hostname = False
                tls_context.verify_mode = ssl.CERT_NONE

                async with aiomqtt.Client(
                    hostname=self.host,
                    port=8883,
                    username="bblp",
                    password=self.access_code,
                    tls_context=tls_context
                ) as client:
                    self._mqtt_client = client
                    self._reported_print_state = None
                    self._external_tray_now = None
                    self._external_tray_target = None
                    self._external_extrusion_prompt = False
                    await self._external_state({"awaiting_extrusion_confirmation": False})
                    self.connected = True
                    print("Connected to Bambu Printer MQTT!")
                    
                    # Subscribe to report topic
                    topic = f"device/{self.serial}/report"
                    await client.subscribe(topic)
                    # Bambu reports are deltas. Seed state after every reconnect.
                    for query in ({"pushing": {"command": "pushall"}}, {"info": {"command": "get_version"}}):
                        await client.publish(f"device/{self.serial}/request", json.dumps(self._inject_sequence_id(query)), qos=0)
                    
                    async for message in client.messages:
                        await self._handle_message(message)
                        
            except aiomqtt.MqttError as e:
                print(f"MQTT Error: {e}")
                self.connected = False
                await asyncio.sleep(5)  # Retry delay
            except Exception as e:
                print(f"Unexpected Error: {e}")
                self.connected = False
                await asyncio.sleep(5)
            finally:
                self.connected = False
                self._mqtt_client = None

    async def _handle_message(self, message):
        try:
            payload = json.loads(message.payload.decode())
            # print(f"Received: {payload.keys()}") # Debug
            
            # Bambu payload structure is complex. We look for 'print' object usually
            if not isinstance(payload, dict):
                return
            if isinstance(payload.get("print"), dict):
                data = payload["print"]
                if isinstance(data, dict):
                    command_name = data.get("command")
                    if command_name and command_name != "push_status":
                        print(f"MQTT report message: {json.dumps(data)}")
                    if (command_name == "ams_filament_setting"
                            and self._external_pending is not None
                            and str(data.get("sequence_id")) == self._external_setting_sequence
                            and str(data.get("result", "")).lower() in {"fail", "failed", "error"}):
                        self._external_rejected = str(data.get("reason") or "Printer rejected filament settings")
                        self._external_pending = None
                        await self._external_state({"sync_state": "failed", "last_error": self._external_rejected})
                await self._parse_telemetry(data)
                
        except (UnicodeDecodeError, ValueError, TypeError) as exc:
            print(f"Ignoring malformed telemetry: {exc}")

    async def _parse_telemetry(self, data: Dict[str, Any]):
        """Maps Bambu telemetry to Moonraker state."""
        updates = {}
        await self._parse_external_filament(data)
        now = time.time()
        current_filename = str(data.get("subtask_name") or "")
        current_gcode_file = str(data.get("gcode_file") or "")

        if current_gcode_file:
            self._active_gcode_file = current_gcode_file

        # Extruder (nozzle)
        extruder_update = {}
        if "nozzle_temper" in data:
            extruder_update["temperature"] = float(data.get("nozzle_temper", 0))
        if "nozzle_target_temper" in data:
            reported_target = float(data.get("nozzle_target_temper", 0))
            extruder_update["target"] = reported_target
            await self._handle_temperature_target_report("extruder", reported_target)
            self._clear_local_target("extruder")
        else:
            local_target = self._get_local_target("extruder")
            if local_target is not None:
                extruder_update["target"] = local_target
        if extruder_update:
            updates["extruder"] = extruder_update

        # Bed
        bed_update = {}
        if "bed_temper" in data:
            bed_update["temperature"] = float(data.get("bed_temper", 0))
        if "bed_target_temper" in data:
            reported_target = float(data.get("bed_target_temper", 0))
            bed_update["target"] = reported_target
            await self._handle_temperature_target_report("bed", reported_target)
            self._clear_local_target("heater_bed")
        else:
            local_target = self._get_local_target("heater_bed")
            if local_target is not None:
                bed_update["target"] = local_target
        if bed_update:
            updates["heater_bed"] = bed_update

        # Chamber
        chamber_update = {}
        if "chamber_temper" in data:
            chamber_update["temperature"] = float(data.get("chamber_temper", 0))
        if "chamber_target_temper" in data:
            reported_target = float(data.get("chamber_target_temper", 0))
            chamber_update["target"] = reported_target
            await self._handle_temperature_target_report("chamber", reported_target)
            self._clear_local_target("heater_chamber")
        else:
            local_target = self._get_local_target("heater_chamber")
            if local_target is not None:
                chamber_update["target"] = local_target
        if chamber_update:
            updates["heater_chamber"] = chamber_update

        # Fan telemetry: Bambu reports fan steps as 0-15.
        part_fan = self._normalize_fan_ratio(data.get("cooling_fan_speed"))
        if part_fan is not None:
            updates["fan"] = {"speed": part_fan}

        aux_fan = self._normalize_fan_ratio(data.get("big_fan1_speed"))
        if aux_fan is not None:
            updates["fan_generic aux"] = {"speed": aux_fan}
            updates["fan_aux"] = {"speed": aux_fan}

        chamber_fan = self._normalize_fan_ratio(data.get("big_fan2_speed"))
        if chamber_fan is not None:
            updates["fan_generic chamber"] = {"speed": chamber_fan}
            updates["fan_chamber"] = {"speed": chamber_fan}

        # Print Stats & Progress
        klipper_state = None
        if "gcode_state" in data:
            # IDLE, RUNNING, PAUSE, FINISH, etc.
            bambu_state = data.get("gcode_state", "IDLE")
            self._reported_print_state = str(bambu_state)
            klipper_state = "standby"
            
            if bambu_state in {"RUNNING", "PREPARE", "SLICING"}:
                klipper_state = "printing"
            elif bambu_state == "PAUSE":
                klipper_state = "paused"
            elif bambu_state == "FINISH":
                klipper_state = "complete" # Or standby?
            elif bambu_state == "FAILED":
                klipper_state = "error"
            elif bambu_state == "IDLE":
                klipper_state = "standby"

            self._update_job_timing(klipper_state, current_filename, now)

            updates["print_stats"] = {
                "state": klipper_state,
                "print_duration": self._current_print_duration(now),
                "total_duration": self._current_total_duration(now),
            }

            if current_filename:
                updates["print_stats"]["filename"] = current_filename

            updates["virtual_sdcard"] = {
                "is_active": klipper_state == "printing"
            }

        if "mc_remaining_time" in data:
            try:
                # P1 telemetry reports remaining time in minutes.
                self._latest_remaining_time = max(0, int(data.get("mc_remaining_time", 0))) * 60
            except (TypeError, ValueError):
                self._latest_remaining_time = 0

        if "layer_num" in data or "total_layer_num" in data:
            if "layer_num" in data:
                try:
                    self._latest_layer_num = max(0, int(data.get("layer_num", 0)))
                except (TypeError, ValueError):
                    self._latest_layer_num = 0
            if "total_layer_num" in data:
                try:
                    self._latest_total_layer_num = max(0, int(data.get("total_layer_num", 0)))
                except (TypeError, ValueError):
                    self._latest_total_layer_num = 0
            updates.setdefault("print_stats", {})
            updates["print_stats"]["info"] = {
                "current_layer": self._latest_layer_num,
                "total_layer": self._latest_total_layer_num,
            }

        # Progress
        if "mc_percent" in data:
            progress = float(data.get("mc_percent", 0)) / 100.0
            file_size_hint = self._lookup_file_size_hint(current_gcode_file or current_filename)
            updates.setdefault("virtual_sdcard", {})
            updates["virtual_sdcard"]["progress"] = progress
            updates["virtual_sdcard"]["file_position"] = int(round(progress * file_size_hint))
            if klipper_state:
                updates["virtual_sdcard"]["is_active"] = klipper_state == "printing"

            updates["display_status"] = {"progress": progress}

        if "mc_remaining_time" in data and self._job_started_at is not None:
            updates.setdefault("print_stats", {})
            progress = float(data.get("mc_percent", 0)) / 100.0 if "mc_percent" in data else 0.0
            inferred_print_duration = self._inferred_print_duration(progress, now)
            updates["print_stats"]["print_duration"] = inferred_print_duration
            updates["print_stats"]["total_duration"] = inferred_print_duration

        filament_total_hint = self._lookup_filament_total_hint(current_gcode_file or current_filename)
        if filament_total_hint > 0 and "mc_percent" in data:
            updates.setdefault("print_stats", {})
            progress = float(data.get("mc_percent", 0)) / 100.0
            updates["print_stats"]["filament_used"] = round(
                max(0.0, min(filament_total_hint, filament_total_hint * progress)),
                3,
            )

        await state_manager.update_state(updates)

    def _update_job_timing(self, state: str, filename: str, now: float):
        if state == "printing":
            if self._job_started_at is None or self._job_finished_at is not None or (filename and filename != self._active_job_filename):
                self._job_finished_at = None
                self._job_started_at = now
                self._job_paused_at = None
                self._job_paused_seconds = 0.0
                self._active_job_filename = filename
            elif self._job_started_at is None:
                self._job_started_at = now
                self._active_job_filename = filename

            if self._job_paused_at is not None:
                self._job_paused_seconds += max(0.0, now - self._job_paused_at)
                self._job_paused_at = None
            return

        if state == "paused":
            if filename and filename != self._active_job_filename:
                self._active_job_filename = filename
            if self._job_started_at is None:
                self._job_started_at = now
            if self._job_paused_at is None:
                self._job_paused_at = now
            return

        if state in {"complete", "error", "cancelled"}:
            if self._job_started_at is not None and self._job_finished_at is None:
                self._job_finished_at = now
            return
        self._job_finished_at = None
        self._job_started_at = None
        self._job_paused_at = None
        self._job_paused_seconds = 0.0
        if state == "standby":
            self._active_job_filename = ""

    def _current_total_duration(self, now: Optional[float] = None) -> float:
        if self._job_started_at is None:
            return 0.0
        now = time.time() if now is None else now
        now = min(now, self._job_finished_at) if self._job_finished_at is not None else now
        return max(0.0, now - self._job_started_at)

    def _estimated_total_duration(self, now: Optional[float] = None) -> float:
        now = time.time() if now is None else now
        if self._latest_remaining_time > 0 and self._job_started_at is not None:
            current_print_duration = self._current_print_duration(now)
            progress = 0.0
            state = state_manager.get_state().get("display_status", {})
            try:
                progress = float(state.get("progress") or 0.0)
            except (TypeError, ValueError):
                progress = 0.0
            inferred_print_duration = self._inferred_print_duration(progress, now)
            return max(current_print_duration, inferred_print_duration) + float(self._latest_remaining_time)
        return self._current_total_duration(now)

    def _inferred_print_duration(self, progress: float, now: Optional[float] = None) -> float:
        now = time.time() if now is None else now
        local_print_duration = self._current_print_duration(now)
        progress = max(0.0, min(1.0, float(progress)))
        if self._latest_remaining_time <= 0 or progress <= 0.0 or progress >= 1.0:
            return local_print_duration
        inferred_total_duration = float(self._latest_remaining_time) / max(1e-6, (1.0 - progress))
        inferred_print_duration = max(0.0, inferred_total_duration - float(self._latest_remaining_time))
        return max(local_print_duration, inferred_print_duration)

    def _current_print_duration(self, now: Optional[float] = None) -> float:
        if self._job_started_at is None:
            return 0.0
        now = time.time() if now is None else now
        now = min(now, self._job_finished_at) if self._job_finished_at is not None else now
        paused_seconds = self._job_paused_seconds
        if self._job_paused_at is not None:
            paused_seconds += max(0.0, now - self._job_paused_at)
        return max(0.0, (now - self._job_started_at) - paused_seconds)

    @staticmethod
    def _normalized_filename_key(filename: str) -> str:
        value = os.path.basename(str(filename or "")).strip().lower()
        for suffix in (".gcode.3mf", ".3mf", ".gcode"):
            if value.endswith(suffix):
                return value[: -len(suffix)]
        return value

    def _filename_matches_active(self, filename: str) -> bool:
        candidate = self._normalized_filename_key(filename)
        if not candidate:
            return False
        active_names = {
            self._normalized_filename_key(self._active_job_filename),
            self._normalized_filename_key(self._active_gcode_file),
        }
        active_names.discard("")
        return candidate in active_names

    def _lookup_cached_file_info(self, filename: str) -> Dict[str, Any]:
        from bambu_moonraker_shim.sqlite_manager import get_sqlite_manager

        sqlite_manager = get_sqlite_manager()
        candidates = []
        for value in (filename, self._active_gcode_file, self._active_job_filename):
            if value:
                candidates.append(str(value))

        for value in candidates:
            metadata = sqlite_manager.get_file_metadata(value)
            if metadata:
                return metadata

        cached_files = sqlite_manager.get_cached_files(max_age=3600) or []
        candidate_keys = {self._normalized_filename_key(value) for value in candidates if value}
        for item in cached_files:
            item_name = str(item.get("name") or item.get("path") or "")
            if self._normalized_filename_key(item_name) in candidate_keys:
                return item
        return {}

    def _lookup_file_size_hint(self, filename: str) -> int:
        cached = self._lookup_cached_file_info(filename)
        try:
            size_value = int(cached.get("size") or 0)
        except (TypeError, ValueError, AttributeError):
            size_value = 0
        return max(size_value, 0)

    def _lookup_filament_total_hint(self, filename: str) -> float:
        cached = self._lookup_cached_file_info(filename)
        try:
            filament_total = float(cached.get("filament_total") or 0.0)
        except (TypeError, ValueError, AttributeError):
            filament_total = 0.0
        return max(filament_total, 0.0)

    @staticmethod
    def _normalize_fan_ratio(raw_value: Any) -> Optional[float]:
        if raw_value is None:
            return None
        try:
            speed = float(raw_value)
        except (TypeError, ValueError):
            return None
        speed = max(0.0, min(15.0, speed))
        return speed / 15.0

    def _next_sequence_id(self) -> str:
        self._sequence_id += 1
        return str(self._sequence_id)

    def _inject_sequence_id(self, command: Dict[str, Any]) -> Dict[str, Any]:
        sequence_id = self._next_sequence_id()
        for top_key in ("print", "system", "info", "pushing"):
            payload = command.get(top_key)
            if isinstance(payload, dict):
                payload["sequence_id"] = sequence_id
        if isinstance(command.get("print"), dict) and self.user_id:
            command.setdefault("user_id", self.user_id)
        return command

    def _set_local_target(self, heater_object: str, target: float):
        self._local_targets[heater_object] = {"target": float(target), "set_time": time.time()}

    def _clear_local_target(self, heater_object: str):
        self._local_targets[heater_object] = {"target": None, "set_time": 0.0}

    def _get_local_target(self, heater_object: str) -> Optional[float]:
        target_info = self._local_targets.get(heater_object)
        if not target_info:
            return None
        target = target_info.get("target")
        set_time = float(target_info.get("set_time") or 0.0)
        if target is None:
            return None
        if time.time() - set_time > self._local_target_max_age_seconds:
            self._clear_local_target(heater_object)
            return None
        return float(target)

    async def _track_local_target(self, heater: str, target: float):
        object_map = {
            "extruder": "extruder",
            "bed": "heater_bed",
            "chamber": "heater_chamber",
        }
        object_name = object_map[heater]
        self._set_local_target(object_name, target)
        await state_manager.update_state({object_name: {"target": float(target)}})

    @staticmethod
    def _temperature_gcode_variants(heater: str, target: int) -> List[str]:
        # P1-series firmware accepts M109/M190 while often ignoring M104/M140.
        if heater == "extruder":
            return [f"M109 S{target}\n"]
        if heater == "bed":
            return [f"M190 S{target}\n"]
        return [f"M191 S{target}\n"]

    def _track_pending_temp_command(self, heater: str, target: float, variant_index: int):
        self._pending_temp_commands[heater] = {
            "target": float(target),
            "set_time": time.time(),
            "variant_index": int(variant_index),
            "fallback_sent": False,
        }

    def _clear_pending_temp_command(self, heater: str):
        self._pending_temp_commands[heater] = {
            "target": None,
            "set_time": 0.0,
            "variant_index": 0,
            "fallback_sent": False,
        }

    async def _handle_temperature_target_report(self, heater: str, reported_target: float):
        pending = self._pending_temp_commands.get(heater)
        if not pending:
            return

        expected_target = pending.get("target")
        if expected_target is None:
            return

        age_seconds = time.time() - float(pending.get("set_time") or 0.0)
        if age_seconds > self._temperature_probe_window_seconds:
            self._clear_pending_temp_command(heater)
            return

        expected_value = float(expected_target)
        if reported_target <= 0 and expected_value > 0:
            print(
                f"Telemetry reported {heater} target as 0 after command "
                f"(expected {expected_value:.1f}, age={age_seconds:.1f}s)"
            )
            if heater == "extruder" and not bool(pending.get("fallback_sent")):
                variants = self._temperature_gcode_variants("extruder", int(round(expected_value)))
                current_variant = int(pending.get("variant_index") or 0)
                fallback_variant = current_variant + 1
                if fallback_variant < len(variants):
                    fallback_gcode = variants[fallback_variant]
                    print(
                        "Retrying extruder target with fallback gcode format: "
                        f"{fallback_gcode.strip()}"
                    )
                    pending["fallback_sent"] = True
                    pending["variant_index"] = fallback_variant
                    pending["set_time"] = time.time()
                    self._preferred_temp_variant_index["extruder"] = fallback_variant
                    await self.send_gcode_line(fallback_gcode)
            return

        if reported_target > 0:
            self._preferred_temp_variant_index[heater] = int(pending.get("variant_index") or 0)

        self._clear_pending_temp_command(heater)

    async def publish_command(self, command: Dict[str, Any]):
        """Sends a JSON command to the printer request topic."""
        if self._mock_mode:
            command_with_sequence = self._inject_sequence_id(command)
            print(f"[MOCK] MQTT Command: {json.dumps(command_with_sequence)}")
            return

        if not self._mqtt_client or not self.connected:
            print("Cannot send command: MQTT disconnected.")
            return

        command_with_sequence = self._inject_sequence_id(command)
        print(f"Sending MQTT Command: {json.dumps(command_with_sequence)}")
        topic = f"device/{self.serial}/request"
        payload = json.dumps(command_with_sequence)
        qos = self._select_publish_qos(command_with_sequence)
        print(f"Publishing MQTT command with qos={qos}")
        # Keep RPC handlers responsive; publish runs in the background.
        asyncio.create_task(self._publish_background(topic, payload, qos))

    def _select_publish_qos(self, command: Dict[str, Any]) -> int:
        # System commands (ledctrl, etc.) are prone to delayed/missing PUBACKs.
        if "system" in command:
            return 0
        if self._prefer_qos0_for_print:
            return 0
        # Print and gcode commands should use reliable delivery.
        return 1

    async def _publish_background(self, topic: str, payload: str, qos: int):
        if not self._mqtt_client:
            return
        try:
            await self._mqtt_client.publish(topic, payload, qos=qos)
        except Exception as exc:
            if qos == 1:
                if not self._prefer_qos0_for_print:
                    print("MQTT qos=1 appears unsupported/reliable on this printer; using qos=0 for print commands.")
                self._prefer_qos0_for_print = True
                print(f"MQTT publish error at qos=1, retrying with qos=0: {exc}")
                try:
                    await self._mqtt_client.publish(topic, payload, qos=0)
                    return
                except Exception as fallback_exc:
                    print(f"MQTT fallback publish failed: {fallback_exc}")
                    return
            print(f"MQTT publish error: {exc}")

    # --- Actions ---
    
    async def pause_print(self):
        if self._mock_mode:
            self._mock_state = "paused"
            await state_manager.update_state(
                {
                    "print_stats": {"state": "paused", "filename": self._mock_filename},
                    "virtual_sdcard": {
                        "is_active": False,
                        "progress": self._mock_progress,
                    },
                }
            )
            return
        cmd = {
            "print": {
                "command": "pause"
            }
        }
        await self.publish_command(cmd)

    async def resume_print(self):
        if self._mock_mode:
            self._mock_state = "printing"
            await state_manager.update_state(
                {
                    "print_stats": {"state": "printing", "filename": self._mock_filename},
                    "virtual_sdcard": {
                        "is_active": True,
                        "progress": self._mock_progress,
                    },
                }
            )
            return
        cmd = {
            "print": {
                "command": "resume"
            }
        }
        await self.publish_command(cmd)
        
    async def cancel_print(self):
        if self._mock_mode:
            self._mock_state = "standby"
            self._mock_progress = 0.0
            await state_manager.update_state(
                {
                    "print_stats": {"state": "standby", "filename": self._mock_filename},
                    "virtual_sdcard": {"is_active": False, "progress": 0.0},
                    "display_status": {"progress": 0.0},
                }
            )
            return
        cmd = {
            "print": {
                "command": "stop"
            }
        }
        await self.publish_command(cmd)

    async def send_gcode_line(self, gcode: str):
        """Sends a raw G-code line to the printer."""
        cmd = {
            "print": {
                "command": "gcode_line",
                "param": gcode
            }
        }
        await self.publish_command(cmd)

    async def start_print(
        self,
        filename: str,
        plate_number: int = 1,
        use_ams: bool = False,
        bed_leveling: bool = True,
        flow_calibration: bool = False,
        timelapse: bool = False,
        vibration_cali: bool = True,
        layer_inspect: bool = False,
        cfg: str = "",
        extrude_cali_flag: bool = False,
        ams_mapping: Optional[Any] = None,
        ams_mapping2: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Start a print for a file already uploaded to the printer."""
        if not self.connected:
            return {"error": "Printer not connected"}

        if not filename:
            return {"error": "Filename required"}

        normalized = filename.lstrip("/")
        if not normalized:
            return {"error": "Filename required"}
        lower_name = normalized.lower()
        is_3mf = lower_name.endswith(".3mf")
        is_gcode = lower_name.endswith(".gcode")

        if not (is_3mf or is_gcode):
            return {"error": "File must be .3mf or .gcode"}

        try:
            plate = max(int(plate_number), 1)
        except (TypeError, ValueError):
            return {"error": f"Invalid plate number: {plate_number}"}
        param = f"Metadata/plate_{plate}.gcode" if is_3mf else ""
        model = str(Config.BAMBU_MODEL or "").upper()
        is_h2d = "H2D" in model
        is_p2s_or_n7 = "P2S" in model or "N7" in model
        effective_vibration_cali = False if is_p2s_or_n7 else bool(vibration_cali)

        def calibration_value(value: bool) -> Any:
            boolean_value = bool(value)
            if is_h2d:
                return 1 if boolean_value else 0
            return boolean_value

        cleaned_ams_mapping = self._normalize_ams_mapping(ams_mapping)
        cleaned_ams_mapping2 = self._normalize_ams_mapping2(ams_mapping2)
        subtask_name = self._subtask_name(normalized)

        if self._mock_mode:
            self._mock_filename = normalized
            self._mock_state = "printing"
            self._mock_progress = 0.0
            await state_manager.update_state(
                {
                    "print_stats": {"state": "printing", "filename": normalized},
                    "virtual_sdcard": {"is_active": True, "progress": 0.0},
                    "display_status": {"progress": 0.0},
                }
            )
            return {"result": "ok", "mock": True}

        cmd = {
            "print": {
                "command": "project_file",
                "param": param,
                "project_id": "0",
                "profile_id": "0",
                "task_id": "0",
                "subtask_id": "0",
                "subtask_name": subtask_name,
                "file": normalized,
                "url": f"ftp://{normalized}",
                "md5": "",
                "timelapse": bool(timelapse),
                "bed_type": "auto",
                "bed_levelling": calibration_value(bed_leveling),
                "auto_bed_leveling": calibration_value(bed_leveling),
                "flow_cali": calibration_value(flow_calibration),
                "vibration_cali": calibration_value(effective_vibration_cali),
                "layer_inspect": calibration_value(layer_inspect),
                "cfg": cfg or "",
                "extrude_cali_flag": calibration_value(extrude_cali_flag),
                "ams_mapping": cleaned_ams_mapping,
                "use_ams": bool(use_ams),
            }
        }
        if cleaned_ams_mapping2 is not None:
            cmd["print"]["ams_mapping2"] = cleaned_ams_mapping2

        try:
            await self.publish_command(cmd)
        except Exception as exc:
            return {"error": str(exc)}

        return {"result": "ok"}

    @staticmethod
    def _subtask_name(filename: str) -> str:
        lower_name = filename.lower()
        if lower_name.endswith(".gcode.3mf"):
            return filename[:-10]
        if lower_name.endswith(".3mf"):
            return filename[:-4]
        if lower_name.endswith(".gcode"):
            return filename[:-6]
        return filename

    @staticmethod
    def _normalize_ams_mapping(ams_mapping: Any) -> str:
        if ams_mapping is None:
            return ""
        if isinstance(ams_mapping, str):
            return ams_mapping
        if isinstance(ams_mapping, (list, tuple)):
            return ",".join(str(item) for item in ams_mapping)
        return str(ams_mapping)

    @staticmethod
    def _normalize_ams_mapping2(ams_mapping2: Any) -> Optional[List[Dict[str, int]]]:
        if ams_mapping2 is None:
            return None
        if not isinstance(ams_mapping2, list):
            return None
        mappings: List[Dict[str, int]] = []
        for item in ams_mapping2:
            if not isinstance(item, dict):
                continue
            if "ams_id" not in item or "slot_id" not in item:
                continue
            try:
                mapped = {"ams_id": int(item["ams_id"]), "slot_id": int(item["slot_id"])}
            except (TypeError, ValueError):
                continue
            mappings.append(mapped)
        return mappings

    async def send_temperature_command(self, heater: str, target_temp: float) -> Dict[str, Any]:
        """Sends a temperature command via MQTT gcode_line."""
        temp_limits = {
            "bed": {"min": 0, "max": 120, "safe_max": 100},
            "extruder": {"min": 0, "max": 300, "safe_max": 280},
            "chamber": {"min": 0, "max": 70, "safe_max": 65},
        }

        if heater not in temp_limits:
            return {"error": f"Unknown heater: {heater}"}

        try:
            target_value = float(target_temp)
        except (TypeError, ValueError):
            return {"error": f"Invalid temperature value: {target_temp}"}

        limits = temp_limits[heater]
        if target_value < limits["min"] or target_value > limits["max"]:
            return {
                "error": (
                    f"Temperature {target_value}°C out of range "
                    f"({limits['min']}-{limits['max']}°C)"
                )
            }

        if target_value > limits["safe_max"]:
            print(
                f"Warning: {heater} temperature {target_value}°C exceeds safe max "
                f"{limits['safe_max']}°C."
            )

        if not self.connected:
            return {"error": "Printer not connected"}

        rounded = int(round(target_value))
        if self._mock_mode:
            if heater == "extruder":
                self._mock_target_nozzle = target_value
            elif heater == "bed":
                self._mock_target_bed = target_value
            else:
                self._mock_target_chamber = target_value
            await self._track_local_target(heater, target_value)
            return {"result": "ok", "mock": True}

        variants = self._temperature_gcode_variants(heater, rounded)
        variant_index = int(self._preferred_temp_variant_index.get(heater, 0))
        if variant_index < 0 or variant_index >= len(variants):
            variant_index = 0
        gcode = variants[variant_index]
        await self.send_gcode_line(gcode)
        self._track_pending_temp_command(heater, target_value, variant_index)
        await self._track_local_target(heater, target_value)
        return {"result": "ok"}

    async def set_nozzle_temp(self, temp_c: float) -> Dict[str, Any]:
        return await self.send_temperature_command("extruder", temp_c)

    async def set_bed_temp(self, temp_c: float) -> Dict[str, Any]:
        return await self.send_temperature_command("bed", temp_c)

    async def set_chamber_temp(self, temp_c: float) -> Dict[str, Any]:
        return await self.send_temperature_command("chamber", temp_c)

    async def set_print_speed(self, mode: int) -> Dict[str, Any]:
        try:
            speed_mode = int(mode)
        except (TypeError, ValueError):
            return {"error": f"Invalid print speed mode: {mode}"}
        if speed_mode < 1 or speed_mode > 4:
            return {"error": "Print speed mode must be 1-4"}

        cmd = {"print": {"command": "print_speed", "param": str(speed_mode)}}
        await self.publish_command(cmd)
        return {"result": "ok"}

    async def restore_external_filament(self):
        from bambu_moonraker_shim.database_manager import database_manager

        saved = database_manager.get_item("bambu_shim", "external_filament")
        if isinstance(saved, dict) and saved:
            try:
                self._external_settings = filament_settings({}, {
                    "MATERIAL": saved.get("material"), "COLOR": saved.get("color"),
                    "PROFILE_ID": saved.get("profile_id"), "TEMP_MIN": saved.get("nozzle_temp_min"),
                    "TEMP_MAX": saved.get("nozzle_temp_max"),
                })
            except ValueError as exc:
                print(f"Ignoring invalid saved external filament: {exc}")
                return
            await self._external_state({**self._external_settings, "sync_state": "saved"})

    async def _external_state(self, values):
        from bambu_moonraker_shim.external_spool import EXTERNAL_MACROS

        await state_manager.update_state({f"gcode_macro {name}": dict(values) for name in EXTERNAL_MACROS})

    async def _parse_external_filament(self, data):
        # Command echoes are not printer status. Preserve the prompt across deltas.
        if data.get("command") not in (None, "push_status"):
            return
        if "print_error" in data:
            try:
                code = int(data["print_error"])
            except (TypeError, ValueError):
                code = 0
            self._external_extrusion_prompt = code in EXTRUSION_PROMPT_ERRORS
            await self._external_state({
                "awaiting_extrusion_confirmation": self._external_extrusion_prompt,
            })
        tray = data.get("vt_tray")
        if isinstance(tray, list):
            self._multiple_external_spools = len(tray) > 1
            tray = tray[0] if len(tray) == 1 else None
        if isinstance(tray, dict):
            reported = {}
            for source, target in (("tray_type", "material"), ("tray_color", "color"),
                                   ("tray_info_idx", "profile_id")):
                if tray.get(source):
                    reported[target] = str(tray[source]).strip().upper()
            for field in ("nozzle_temp_min", "nozzle_temp_max"):
                try:
                    if field in tray:
                        reported[field] = int(tray[field])
                except (TypeError, ValueError):
                    pass
            self._external_reported.update(reported)
            if reported:
                values = {**self._external_reported, "sync_state": "reported", "last_error": ""}
                if self._external_rejected:
                    values.update(sync_state="failed", last_error=self._external_rejected)
                if self._external_pending:
                    matches = all(self._external_reported.get(k) == v for k, v in self._external_pending.items())
                    values["sync_state"] = "confirmed" if matches else "pending"
                    if matches:
                        self._external_pending = None
                await self._external_state(values)
        ams = data.get("ams")
        if isinstance(ams, dict) and "tray_tar" in ams:
            try:
                self._external_tray_target = int(ams["tray_tar"])
            except (TypeError, ValueError):
                self._external_tray_target = None
        if isinstance(ams, dict) and "tray_now" in ams:
            try:
                self._external_tray_now = int(ams["tray_now"])
            except (TypeError, ValueError):
                return
            # Only printer telemetry (or mock mode) changes loaded status.
            await self._external_state({"loaded": self._external_tray_now == 254})

    def _external_operation_error(self):
        if not self.connected or (not self._mock_mode and self._mqtt_client is None):
            return "Printer not connected"
        model = str(Config.BAMBU_MODEL or "").upper()
        if self._multiple_external_spools or model.startswith(("H2D", "H2C")):
            return "External-spool macros currently support one external spool on a single-nozzle printer"
        if not self._mock_mode and self._reported_print_state not in {"IDLE", "FINISH", "FAILED"}:
            return "Filament commands require confirmed idle printer telemetry; printing, paused, and unknown states are blocked"
        if self._mock_mode and self._mock_state in {"printing", "paused"}:
            return "Filament commands are unavailable during a mock print"
        if self._external_tray_now is not None and 0 <= self._external_tray_now < 254:
            return "An AMS tray is active; unload it using the AMS controls first"
        return None

    async def _publish_external_command(self, fields):
        """Await transport delivery, without replaying a physical operation.

        QoS 0 avoids the missing PUBACK behavior seen on this P1S. A publish
        completing is transport success, not confirmation of firmware action.
        """
        if self._mock_mode:
            return
        command = self._inject_sequence_id({"print": fields})
        if fields["command"] == "ams_filament_setting":
            self._external_setting_sequence = command["print"]["sequence_id"]
        await asyncio.wait_for(self._mqtt_client.publish(
            f"device/{self.serial}/request", json.dumps(command), qos=0,
        ), timeout=10)

    async def set_external_filament(self, params) -> Dict[str, Any]:
        error = self._external_operation_error()
        if error:
            return {"error": error}
        current = {**self._external_settings, **self._external_reported}
        try:
            if not current.get("material") and not params.get("MATERIAL"):
                raise ValueError("MATERIAL is required until the external spool type is known")
            settings = filament_settings(current, params)
        except ValueError as exc:
            return {"error": str(exc)}
        fields = {
            "command": "ams_filament_setting", "ams_id": 255, "tray_id": 254, "slot_id": 0,
            "tray_info_idx": settings["profile_id"], "tray_type": settings["material"],
            "tray_sub_brands": "", "tray_color": settings["color"],
            "nozzle_temp_min": settings["nozzle_temp_min"], "nozzle_temp_max": settings["nozzle_temp_max"],
        }
        from bambu_moonraker_shim.database_manager import database_manager

        # Persist the request before sending, so it is not lost on a restart.
        database_manager.post_item("bambu_shim", "external_filament", settings)
        self._external_settings = settings
        self._external_pending = None if self._mock_mode else settings
        self._external_rejected = None
        try:
            await self._publish_external_command(fields)
        except Exception as exc:
            self._external_pending = None
            self._external_rejected = str(exc)
            await self._external_state({**settings, "sync_state": "failed", "last_error": str(exc)})
            return {"error": f"Filament settings delivery failed: {exc}"}
        if self._external_rejected:
            return {"error": self._external_rejected}
        sync = "mock" if self._mock_mode else ("pending" if self._external_pending else "confirmed")
        await self._external_state({**settings, "sync_state": sync, "last_error": ""})
        return {"result": "ok"}

    async def load_filament(self) -> Dict[str, Any]:
        error = self._external_operation_error()
        if error:
            return {"error": error}
        try:
            await self._publish_external_command({
                "command": "ams_change_filament", "ams_id": 255, "slot_id": 254,
                "target": 254, "curr_temp": -1, "tar_temp": -1,
            })
        except Exception as exc:
            return {"error": f"External load delivery failed: {exc}"}
        values = {"last_action": "load_requested", "last_error": ""}
        if self._mock_mode:
            values["loaded"] = True
        await self._external_state(values)
        return {"result": "ok"}

    async def answer_external_extrusion(self, action) -> Dict[str, Any]:
        error = self._external_operation_error()
        if error:
            return {"error": error}
        if action not in {"done", "resume"}:
            return {"error": "Invalid external extrusion choice"}
        if not self._external_extrusion_prompt:
            return {"error": "Printer is not asking whether filament extruded"}
        # Unknown tray selection could be an AMS operation; never answer that here.
        if not self._mock_mode and not (self._external_tray_now == 254 or self._external_tray_target == 254):
            return {"error": "External spool selection is not confirmed"}
        try:
            await self._publish_external_command({"command": "ams_control", "param": action})
        except Exception as exc:
            return {"error": f"External load response delivery failed: {exc}"}
        await self._external_state({
            "last_action": "extrusion_confirm_requested" if action == "done" else "extrusion_retry_requested",
            "last_error": "",
        })
        # Firmware telemetry clears the question; publish success does not.
        return {"result": "ok"}

    async def unload_filament(self, temperature=None) -> Dict[str, Any]:
        error = self._external_operation_error()
        if error:
            return {"error": error}
        settings = {**self._external_settings, **self._external_reported}
        try:
            if temperature is None:
                if not settings:
                    raise ValueError("Set external filament first or supply TEMP for unloading")
                temperature = (settings["nozzle_temp_min"] + settings["nozzle_temp_max"]) // 2
            target = int(temperature)
            if not 170 <= target <= 300:
                raise ValueError("TEMP must be 170–300 Celsius")
        except (KeyError, TypeError, ValueError) as exc:
            return {"error": str(exc)}
        nozzle = state_manager.get_object("extruder") or {}
        try:
            current = int(round(float(nozzle.get("temperature", 0))))
            if current < 170:
                current = target
            await self._publish_external_command({
                "command": "ams_change_filament", "ams_id": 255, "slot_id": 255,
                "target": 255, "curr_temp": current, "tar_temp": target,
            })
        except Exception as exc:
            return {"error": f"External unload delivery failed: {exc}"}
        values = {"last_action": "unload_requested", "last_error": ""}
        if self._mock_mode:
            values["loaded"] = False
        await self._external_state(values)
        return {"result": "ok"}

    async def ams_load_filament(self, tray_id: int, ams_id: int = 0, slot_id: int = 0) -> Dict[str, Any]:
        try:
            tray = int(tray_id)
            ams = int(ams_id)
            slot = int(slot_id)
        except (TypeError, ValueError):
            return {"error": "Invalid AMS load parameters"}

        cmd = {
            "print": {
                "command": "ams_change_filament",
                "ams_id": ams,
                "slot_id": slot,
                "target": tray,
                "curr_temp": -1,
                "tar_temp": -1,
            }
        }
        await self.publish_command(cmd)
        return {"result": "ok"}

    async def ams_unload_filament(self) -> Dict[str, Any]:
        nozzle_temp = 0
        extruder_state = state_manager.get_object("extruder") or {}
        try:
            nozzle_temp = int(round(float(extruder_state.get("temperature", 0))))
        except (TypeError, ValueError):
            nozzle_temp = 0

        cmd = {
            "print": {
                "command": "ams_change_filament",
                "slot_id": 255,
                "target": 255,
                "curr_temp": nozzle_temp,
                "tar_temp": nozzle_temp,
            }
        }
        await self.publish_command(cmd)
        return {"result": "ok"}

    async def skip_objects(self, object_ids: List[int]) -> Dict[str, Any]:
        parsed_ids: List[int] = []
        for object_id in object_ids:
            try:
                parsed_ids.append(int(object_id))
            except (TypeError, ValueError):
                continue
        if not parsed_ids:
            return {"error": "No valid object IDs provided"}

        cmd = {"print": {"command": "skip_objects", "obj_list": parsed_ids}}
        await self.publish_command(cmd)
        return {"result": "ok"}

    async def home_axes(self, axes: str = "XYZ") -> Dict[str, Any]:
        axis_tokens = []
        for axis in str(axes).upper():
            if axis in {"X", "Y", "Z"}:
                axis_tokens.append(axis)
        if not axis_tokens:
            axis_tokens = ["X", "Y", "Z"]
        gcode = "G28 " + " ".join(axis_tokens) + "\n"
        await self.send_gcode_line(gcode)
        return {"result": "ok"}

    async def move_axis(self, axis: str, distance: float, speed: float) -> Dict[str, Any]:
        axis_name = str(axis).upper().strip()
        if axis_name not in {"X", "Y", "Z", "E"}:
            return {"error": f"Invalid axis: {axis}"}
        try:
            travel = float(distance)
            feedrate = float(speed)
        except (TypeError, ValueError):
            return {"error": "Distance and speed must be numeric"}
        gcode = f"G91\nG0 {axis_name}{travel:g} F{feedrate:g}\nG90\n"
        await self.send_gcode_line(gcode)
        return {"result": "ok"}

    async def disable_motors(self) -> Dict[str, Any]:
        await self.send_gcode_line("M18\n")
        return {"result": "ok"}

    async def set_chamber_light(self, on: bool):
        """Turns the chamber light on or off using MQTT."""
        for light_node in ("chamber_light", "chamber_light2"):
            cmd_system = {
                "system": {
                    "command": "ledctrl",
                    "led_node": light_node,
                    "led_mode": "on" if on else "off",
                    "led_on_time": 500,
                    "led_off_time": 500,
                    "loop_times": 0,
                    "interval_time": 0,
                }
            }
            await self.publish_command(cmd_system)

    async def set_light(self, on: bool):
        await self.set_chamber_light(on)

    # --- Mock Mode ---
    async def _mock_loop(self):
        print("Starting Mock Bambu Printer loop...")

        while True:
            await asyncio.sleep(1)
            
            # Simulate heating
            if self._mock_target_nozzle > self._mock_current_nozzle:
                self._mock_current_nozzle += 5
            elif self._mock_target_nozzle < self._mock_current_nozzle:
                self._mock_current_nozzle -= 2
            
            if self._mock_target_bed > self._mock_current_bed:
                self._mock_current_bed += 2
            elif self._mock_target_bed < self._mock_current_bed:
                self._mock_current_bed -= 1

            if self._mock_target_chamber > self._mock_current_chamber:
                self._mock_current_chamber += 1
            elif self._mock_target_chamber < self._mock_current_chamber:
                self._mock_current_chamber -= 1

            # Simulate printing
            if self._mock_state == "printing":
                self._mock_progress += 0.01
                if self._mock_progress >= 1.0:
                    self._mock_state = "complete"
                    self._mock_progress = 1.0
                    self._mock_target_nozzle = 0
                    self._mock_target_bed = 0
                    self._mock_target_chamber = 0
            
            updates = {
                "extruder": {
                    "temperature": self._mock_current_nozzle,
                    "target": self._mock_target_nozzle,
                },
                "heater_bed": {
                    "temperature": self._mock_current_bed,
                    "target": self._mock_target_bed,
                },
                "heater_chamber": {
                    "temperature": self._mock_current_chamber,
                    "target": self._mock_target_chamber,
                },
                "print_stats": {"state": self._mock_state, "filename": self._mock_filename},
                "virtual_sdcard": {
                    "progress": self._mock_progress,
                    "is_active": self._mock_state == "printing",
                },
                "display_status": {"progress": self._mock_progress},
            }
            await state_manager.update_state(updates)

bambu_client = BambuClient()
