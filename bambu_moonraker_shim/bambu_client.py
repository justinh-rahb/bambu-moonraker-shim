import asyncio
import json
import ssl
import time
from typing import Optional, Dict, Any, List
import aiomqtt
from bambu_moonraker_shim.config import Config
from bambu_moonraker_shim.state_manager import state_manager

class BambuClient:
    def __init__(self):
        self.host = Config.BAMBU_HOST
        self.access_code = Config.BAMBU_ACCESS_CODE
        self.serial = Config.BAMBU_SERIAL
        self.user_id = str(Config.BAMBU_USER_ID or "")
        self.connected = False
        self._mock_mode = False
        self._mqtt_client: Optional[aiomqtt.Client] = None
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

    async def start(self):
        """Starts the MQTT loop."""
        if not self.serial:
             print("Warning: BAMBU_SERIAL not set. Running in mock mode.")
             self._mock_mode = True
             self.connected = True
             asyncio.create_task(self._mock_loop())
             return

        asyncio.create_task(self._connect_loop())

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
                    self.connected = True
                    print("Connected to Bambu Printer MQTT!")
                    
                    # Subscribe to report topic
                    topic = f"device/{self.serial}/report"
                    await client.subscribe(topic)
                    
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

    async def _handle_message(self, message):
        try:
            payload = json.loads(message.payload.decode())
            # print(f"Received: {payload.keys()}") # Debug
            
            # Bambu payload structure is complex. We look for 'print' object usually
            if "print" in payload:
                data = payload["print"]
                if isinstance(data, dict):
                    command_name = data.get("command")
                    if command_name and command_name != "push_status":
                        print(f"MQTT report message: {json.dumps(data)}")
                await self._parse_telemetry(data)
                
        except json.JSONDecodeError:
            print("Failed to decode JSON payload")

    async def _parse_telemetry(self, data: Dict[str, Any]):
        """Maps Bambu telemetry to Moonraker state."""
        updates = {}

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
            klipper_state = "standby"
            
            if bambu_state == "RUNNING":
                klipper_state = "printing"
            elif bambu_state == "PAUSE":
                klipper_state = "paused"
            elif bambu_state == "FINISH":
                klipper_state = "complete" # Or standby?
            elif bambu_state == "IDLE":
                klipper_state = "standby"
            
            updates["print_stats"] = {"state": klipper_state}
            
            # Filename
            if "subtask_name" in data:
                updates["print_stats"]["filename"] = data["subtask_name"]

            updates["virtual_sdcard"] = {
                "is_active": klipper_state == "printing"
            }

        # Progress
        if "mc_percent" in data:
            progress = float(data.get("mc_percent", 0)) / 100.0
            updates.setdefault("virtual_sdcard", {})
            updates["virtual_sdcard"]["progress"] = progress
            if klipper_state:
                updates["virtual_sdcard"]["is_active"] = klipper_state == "printing"

            updates["display_status"] = {"progress": progress}
            
            # Duration (Bambu sends minutes_remaining, logic needed for elapsed)
            # For MVP we might just use time.time() difference if we tracked start,
            # or just ignore duration for now if not easily available.
            
        await state_manager.update_state(updates)

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

    async def load_filament(self) -> Dict[str, Any]:
        cmd = {"print": {"command": "load_filament"}}
        await self.publish_command(cmd)
        return {"result": "ok"}

    async def unload_filament(self) -> Dict[str, Any]:
        cmd = {"print": {"command": "unload_filament"}}
        await self.publish_command(cmd)
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
