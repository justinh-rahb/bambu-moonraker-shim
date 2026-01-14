import time
import uuid
from typing import Dict, Any, List, Optional

class StateManager:
    def __init__(self):
        self._state: Dict[str, Any] = {
            "extruder": {"temperature": 0.0, "target": 0.0, "power": 0.0, "pressure_advance": 0.0, "smooth_time": 0.0},
            "heater_bed": {"temperature": 0.0, "target": 0.0, "power": 0.0},
            "fan": {"speed": 0.0},
            "virtual_sdcard": {"progress": 0.0, "is_active": False, "file_position": 0},
            "display_status": {"progress": 0.0, "message": ""},
            "heaters": {
                "available_heaters": ["extruder", "heater_bed"],
                "available_sensors": ["extruder", "heater_bed"]
            },
            "print_stats": {
                "state": "standby", # standby, printing, paused, complete, error, cancelling
                "filename": "",
                "print_duration": 0.0,
                "total_duration": 0.0,
                "filament_used": 0.0,
            },
            "toolhead": {
                "position": [0.0, 0.0, 0.0],
                "status": "Ready",
                "homed_axes": "xyz",
                "max_velocity": 500.0,
                "max_accel": 3000.0,
                "max_accel_to_decel": 1500.0,
                "square_corner_velocity": 5.0,
            },
            "configfile": {
                "settings": {
                    "printer": {
                        "kinematics": "corexy",
                        "max_velocity": 500,
                        "max_accel": 10000
                    },
                    "extruder": {
                        "min_temp": 0,
                        "max_temp": 300,
                        "nozzle_diameter": 0.4
                    },
                    "heater_bed": {
                        "min_temp": 0,
                        "max_temp": 120
                    },
                    "fan": {
                        "pin": "fan0"
                    },
                    "virtual_sdcard": {
                        "path": "/tmp/gcodes"
                    },
                    "pause_resume": {},
                    "display_status": {},
                    "gcode_macro pause": {},
                    "gcode_macro resume": {},
                    "gcode_macro cancel_print": {},
                    "output_pin caselight": {
                         "pin": "gpio1",
                         "pwm": False,
                         "value": 0,
                         "shutdown_value": 0
                    }
                },
                "config": {
                     "printer": {
                        "kinematics": "corexy",
                        "max_velocity": "500",
                        "max_accel": "10000"
                    },
                    "extruder": {
                        "min_temp": "0",
                        "max_temp": "300",
                        "nozzle_diameter": "0.4"
                    },
                    "heater_bed": {
                        "min_temp": "0",
                        "max_temp": "120"
                    },
                    "virtual_sdcard": {
                        "path": "/tmp/gcodes"
                    },
                    "pause_resume": {},
                    "display_status": {},
                    "gcode_macro pause": {},
                    "gcode_macro resume": {},
                    "gcode_macro cancel_print": {}
                }
            },
            "virtual_sdcard": {
                "progress": 0.0,
                "is_active": False,
                "file_position": 0,
            },
            "display_status": {
                "message": "",
                "progress": 0.0,
            },
            "fan": {"speed": 0.0},
            "output_pin caselight": {
                "value": 0.0
            },
            "webhooks": {
                "state": "ready",
                "state_message": "Printer is ready"
            }
        }
        self._max_temp_samples = 600
        self._temperature_objects = {"extruder", "heater_bed"}
        self._temperature_history: Dict[str, Dict[str, List[float]]] = {}
        for sensor in self._temperature_objects:
            self._record_temperature_sample(sensor)
        self._subscribers: List[Any] = [] # List[WebSocket]
        self._last_event_time = time.time()
        
        # Job tracking
        self._current_job_id: Optional[str] = None
        self._current_job_start: Optional[float] = None
        self._last_print_state: str = "standby"

    def get_state(self) -> Dict[str, Any]:
        return self._state

    def get_object(self, object_name: str) -> Optional[Dict[str, Any]]:
        return self._state.get(object_name)

    async def update_state(self, updates: Dict[str, Any]):
        """
        Updates the internal state and notifies subscribers of changes.
        """
        changed_objects = {}
        
        for category, values in updates.items():
            if category in self._state:
                current_category = self._state[category]
                category_changes = {}
                for key, value in values.items():
                    if current_category.get(key) != value:
                        current_category[key] = value
                        category_changes[key] = value
                
                if category_changes:
                    changed_objects[category] = category_changes
                if category in self._temperature_objects:
                    self._record_temperature_sample(category)
            elif category in self._temperature_objects:
                self._record_temperature_sample(category)
        
        # Track job history when print state changes
        if "print_stats" in changed_objects and "state" in changed_objects["print_stats"]:
            await self._handle_print_state_change(
                self._state["print_stats"]["state"],
                self._state["print_stats"].get("filename", ""),
                self._state["print_stats"].get("filament_used", 0.0)
            )
        
        if changed_objects:
            self._last_event_time = time.time()
            await self._notify_subscribers(changed_objects)

    def _append_history_value(self, sensor: str, field: str, value: Optional[float]):
        if value is None:
            return
        history = self._temperature_history.setdefault(sensor, {})
        series = history.setdefault(field, [])
        series.append(float(value))
        if len(series) > self._max_temp_samples:
            series.pop(0)

    def _record_temperature_sample(self, sensor: str):
        if sensor not in self._temperature_objects:
            return
        sensor_state = self._state.get(sensor)
        if not sensor_state:
            return
        self._append_history_value(sensor, "temperatures", sensor_state.get("temperature"))
        self._append_history_value(sensor, "targets", sensor_state.get("target"))
        self._append_history_value(sensor, "powers", sensor_state.get("power"))

    def get_temperature_history(self, include_monitors: bool = False) -> Dict[str, Any]:
        history: Dict[str, Any] = {}
        for sensor in self._temperature_objects:
            sensor_history = self._temperature_history.get(sensor)
            if sensor_history:
                history[sensor] = {key: list(values) for key, values in sensor_history.items() if values}
            if sensor not in history:
                sensor_state = self._state.get(sensor, {})
                seeded = {}
                if "temperature" in sensor_state:
                    seeded.setdefault("temperatures", []).append(float(sensor_state["temperature"]))
                if "target" in sensor_state:
                    seeded.setdefault("targets", []).append(float(sensor_state["target"]))
                if "power" in sensor_state:
                    seeded.setdefault("powers", []).append(float(sensor_state["power"]))
                if seeded:
                    history[sensor] = seeded
        return history

    async def _handle_print_state_change(self, new_state: str, filename: str, filament_used: float):
        """Track job history when print state changes."""
        # Import here to avoid circular dependency
        from bambu_moonraker_shim.sqlite_manager import get_sqlite_manager
        
        sqlite_manager = get_sqlite_manager()
        
        # Starting a new print
        if new_state == "printing" and self._last_print_state != "printing":
            self._current_job_id = str(uuid.uuid4())[:8]  # Short ID
            self._current_job_start = time.time()
            print(f"Job started: {self._current_job_id} - {filename}")
        
        # Print completed
        elif new_state == "complete" and self._current_job_id:
            end_time = time.time()
            duration = end_time - self._current_job_start if self._current_job_start else 0
            
            job_data = {
                "job_id": self._current_job_id,
                "filename": filename,
                "start_time": self._current_job_start,
                "end_time": end_time,
                "total_duration": duration,
                "status": "completed",
                "filament_used": filament_used,
                "metadata": {}
            }
            
            sqlite_manager.add_job(job_data)
            print(f"Job completed: {self._current_job_id} - {filename} ({duration:.0f}s)")
            
            self._current_job_id = None
            self._current_job_start = None
        
        # Print cancelled or error
        elif new_state in ("cancelled", "error", "standby") and self._current_job_id:
            end_time = time.time()
            duration = end_time - self._current_job_start if self._current_job_start else 0
            
            job_data = {
                "job_id": self._current_job_id,
                "filename": filename,
                "start_time": self._current_job_start,
                "end_time": end_time,
                "total_duration": duration,
                "status": new_state if new_state in ("cancelled", "error") else "cancelled",
                "filament_used": filament_used,
                "metadata": {}
            }
            
            sqlite_manager.add_job(job_data)
            print(f"Job {job_data['status']}: {self._current_job_id} - {filename}")
            
            self._current_job_id = None
            self._current_job_start = None
        
        self._last_print_state = new_state

    async def _notify_subscribers(self, changes: Dict[str, Any]):
        if not self._subscribers:
            return
            
        notification = {
            "jsonrpc": "2.0",
            "method": "notify_status_update",
            "params": [
                changes,
                self._last_event_time
            ]
        }
        
        # We need to handle potential disconnections locally in the API layer, 
        # but here we just iterate and send. Ideally this uses a callback or event bus.
        # For MVP, we will assume the API layer registers a callback instead of raw websockets if needed,
        # OR we just expose a method to 'emit' to all.
        # The API layer will poll or hook into this. 
        # Actually, better pattern: let API layer register a broadcast function.

    _broadcast_callback = None

    def set_broadcast_callback(self, callback):
        self._broadcast_callback = callback

    async def _notify_subscribers(self, changes: Dict[str, Any]):
        if self._broadcast_callback:
            notification = {
                "jsonrpc": "2.0",
                "method": "notify_status_update",
                "params": [
                    changes,
                    self._last_event_time
                ]
            }
            await self._broadcast_callback(notification)

state_manager = StateManager()
