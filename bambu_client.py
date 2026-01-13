import asyncio
import json
import ssl
import time
from typing import Optional, Dict, Any
import aiomqtt
from config import Config
from state_manager import state_manager

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
        if "nozzle_temper" in data and "nozzle_target_temper" in data:
            updates["extruder"] = {
                "temperature": float(data.get("nozzle_temper", 0)),
                "target": float(data.get("nozzle_target_temper", 0))
            }

        # Bed
        if "bed_temper" in data and "bed_target_temper" in data:
            updates["heater_bed"] = {
                "temperature": float(data.get("bed_temper", 0)),
                "target": float(data.get("bed_target_temper", 0))
            }

        # Fan
        if "cooling_fan_speed" in data:
            # Bambu sends 0-15 (sometimes strings), map to 0.0-1.0
            try:
                speed = float(data.get("cooling_fan_speed", 0))
                updates["fan"] = {"speed": speed / 15.0 if speed > 0 else 0.0}
            except:
                pass

        # Print Stats & Progress
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
                updates["virtual_sdcard"] = {
                    "progress": progress,
                    "is_active": klipper_state == "printing"
                }
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
