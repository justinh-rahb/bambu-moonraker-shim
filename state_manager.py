import asyncio
import time
from typing import Dict, Any, List, Optional

class StateManager:
    def __init__(self):
        self._state: Dict[str, Any] = {
            "extruder": {"temperature": 0.0, "target": 0.0},
            "heater_bed": {"temperature": 0.0, "target": 0.0},
            "print_stats": {
                "state": "standby", # standby, printing, paused, complete, error, cancelling
                "filename": "",
                "print_duration": 0.0,
                "total_duration": 0.0,
                "filament_used": 0.0,
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
            "webhooks": {
                "state": "ready",
                "state_message": "Printer is ready"
            }
        }
        self._subscribers: List[Any] = [] # List[WebSocket]
        self._last_event_time = time.time()

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
        
        if changed_objects:
            self._last_event_time = time.time()
            await self._notify_subscribers(changed_objects)

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
        pass # The API layer will poll or hook into this. 
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
