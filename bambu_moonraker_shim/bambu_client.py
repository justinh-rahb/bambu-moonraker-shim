import asyncio
import json
import ssl
import time
from typing import Optional, Dict, Any
import aiomqtt
from bambu_moonraker_shim.config import Config
from bambu_moonraker_shim.state_manager import state_manager

class BambuClient:
    def __init__(self):
        self.host = Config.BAMBU_HOST
        self.access_code = Config.BAMBU_ACCESS_CODE
        self.serial = Config.BAMBU_SERIAL
        self.connected = False
        self._mqtt_client: Optional[aiomqtt.Client] = None

    async def start(self):
        """Starts the MQTT loop."""
        if not self.serial:
             print("Warning: BAMBU_SERIAL not set. Running in mock mode.")
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
            extruder_update["target"] = float(data.get("nozzle_target_temper", 0))
        if extruder_update:
            updates["extruder"] = extruder_update

        # Bed
        bed_update = {}
        if "bed_temper" in data:
            bed_update["temperature"] = float(data.get("bed_temper", 0))
        if "bed_target_temper" in data:
            bed_update["target"] = float(data.get("bed_target_temper", 0))
        if bed_update:
            updates["heater_bed"] = bed_update

        # Fan
        if "cooling_fan_speed" in data:
            # Bambu sends 0-15 (sometimes strings), but if on/off only, expose as 100%.
            try:
                speed = float(data.get("cooling_fan_speed", 0))
                updates["fan"] = {"speed": 1.0 if speed > 0 else 0.0}
            except:
                pass

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

        # Progress
        if "mc_percent" in data:
            progress = float(data.get("mc_percent", 0)) / 100.0
            updates["virtual_sdcard"] = {"progress": progress}
            if klipper_state:
                 updates["virtual_sdcard"]["is_active"] = klipper_state == "printing"

            updates["display_status"] = {"progress": progress}
            
            # Duration (Bambu sends minutes_remaining, logic needed for elapsed)
            # For MVP we might just use time.time() difference if we tracked start,
            # or just ignore duration for now if not easily available.
            
        await state_manager.update_state(updates)

    async def publish_command(self, command: Dict[str, Any]):
        """Sends a JSON command to the printer request topic."""
        if not self._mqtt_client or not self.connected:
            print("Cannot send command: MQTT disconnected.")
            return

        print(f"Sending MQTT Command: {json.dumps(command)}")
        topic = f"device/{self.serial}/request"
        payload = json.dumps(command)
        await self._mqtt_client.publish(topic, payload)

    # --- Actions ---
    
    async def pause_print(self):
        cmd = {
            "print": {
                "sequence_id": "0",
                "command": "pause"
            }
        }
        await self.publish_command(cmd)

    async def resume_print(self):
        cmd = {
            "print": {
                "sequence_id": "0",
                "command": "resume"
            }
        }
        await self.publish_command(cmd)
        
    async def cancel_print(self):
        cmd = {
            "print": {
                "sequence_id": "0",
                "command": "stop"
            }
        }
        await self.publish_command(cmd)

    async def send_gcode_line(self, gcode: str):
        """Sends a raw G-code line to the printer."""
        cmd = {
            "print": {
                "sequence_id": "0",
                "command": "gcode_line",
                "param": gcode
            }
        }
        await self.publish_command(cmd)

    async def send_temperature_command(
        self, heater: str, target_temp: float, wait: bool = False
    ) -> Dict[str, Any]:
        """Sends a temperature command via MQTT gcode_line."""
        temp_limits = {
            "bed": {"min": 0, "max": 120, "safe_max": 100},
            "extruder": {"min": 0, "max": 300, "safe_max": 280},
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

        if not self._mqtt_client or not self.connected:
            return {"error": "Printer not connected"}

        rounded = int(round(target_value))
        if heater == "bed":
            cmd = "M190"
        else:
            cmd = "M109" if wait or rounded == 0 else "M104"

        gcode = f"{cmd} S{rounded} \n"
        await self.send_gcode_line(gcode)
        return {"result": "ok"}

    async def set_nozzle_temp(self, temp_c: float, wait: bool = False) -> Dict[str, Any]:
        return await self.send_temperature_command("extruder", temp_c, wait=wait)

    async def set_bed_temp(self, temp_c: float, wait: bool = False) -> Dict[str, Any]:
        return await self.send_temperature_command("bed", temp_c, wait=wait)

    async def set_light(self, on: bool):
        """Turns the chamber light on or off using MQTT."""
        # Send both 'print' and 'system' variants to ensure compatibility
        cmd_print = {
            "print": {
                "sequence_id": "0",
                "command": "ledctrl",
                "led_node": "chamber_light",
                "led_mode": "on" if on else "off",
                "led_on_time": 500,
                "led_off_time": 500,
                "loop_times": 0,
                "interval_time": 0
            }
        }
        await self.publish_command(cmd_print)
        
        cmd_system = {
            "system": {
                "sequence_id": "0",
                "command": "ledctrl",
                "led_node": "chamber_light",
                "led_mode": "on" if on else "off",
                "led_on_time": 500,
                "led_off_time": 500,
                "loop_times": 0,
                "interval_time": 0
            }
        }
        await self.publish_command(cmd_system)

    # --- Mock Mode ---
    async def _mock_loop(self):
        print("Starting Mock Bambu Printer loop...")
        import random
        
        target_nozzle = 0
        current_nozzle = 20
        target_bed = 0
        current_bed = 20
        progress = 0
        state = "standby"

        while True:
            await asyncio.sleep(1)
            
            # Simulate heating
            if target_nozzle > current_nozzle: current_nozzle += 5
            elif target_nozzle < current_nozzle: current_nozzle -= 2
            
            if target_bed > current_bed: current_bed += 2
            elif target_bed < current_bed: current_bed -= 1

            # Simulate printing
            if state == "printing":
                progress += 0.01
                if progress >= 1.0:
                    state = "complete"
                    progress = 1.0
                    target_nozzle = 0
                    target_bed = 0
            
            updates = {
                "extruder": {"temperature": current_nozzle, "target": target_nozzle},
                "heater_bed": {"temperature": current_bed, "target": target_bed},
                "print_stats": {"state": state, "filename": "mock_file.gcode"},
                "virtual_sdcard": {"progress": progress, "is_active": state == "printing"}
            }
            await state_manager.update_state(updates)

bambu_client = BambuClient()
