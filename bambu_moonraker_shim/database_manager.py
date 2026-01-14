import json
import os
from typing import Any, Dict, Iterable, Optional

class DatabaseManager:
    def __init__(self, db_path: str = "moonraker.json"):
        self.db_path = db_path
        self._db: Dict[str, Any] = {}
        self._load()

    def _load(self):
        if os.path.exists(self.db_path):
            try:
                with open(self.db_path, "r") as f:
                    self._db = json.load(f)
            except Exception as e:
                print(f"Error loading database: {e}")
                self._db = {}
        else:
            self._db = {}

    def _save(self):
        try:
            with open(self.db_path, "w") as f:
                json.dump(self._db, f, indent=4)
        except Exception as e:
            print(f"Error saving database: {e}")

    def get_item(self, namespace: str, key: Optional[str] = None) -> Any:
        if namespace not in self._db:
            return None
        
        if key:
            return self._db[namespace].get(key)
        else:
            return self._db[namespace]

    def post_item(self, namespace: str, key: Optional[str], value: Any):
        if namespace not in self._db:
            self._db[namespace] = {}
        
        if key:
            self._db[namespace][key] = value
        else:
            # If key is None, value should be a dict to merge or replace?
            # Moonraker docs: "If the key is omitted the value must be an object, which is then merged into the namespace."
            if isinstance(value, dict):
                 self._db[namespace].update(value)
            else:
                 # Fallback/Error?
                 pass
        self._save()
        return self._db[namespace].get(key) if key else self._db[namespace]

    def delete_item(self, namespace: str, key: str):
        if namespace in self._db:
            if key in self._db[namespace]:
                del self._db[namespace][key]
                self._save()
                return  self._db[namespace]
        return None

        return None

    def get_namespaces(self):
        return list(self._db.keys())

    def ensure_namespaces(self, namespaces: Iterable[str]):
        updated = False
        for namespace in namespaces:
            if namespace not in self._db:
                self._db[namespace] = {}
                updated = True
        if updated:
            self._save()

# Global instance
database_manager = DatabaseManager()
