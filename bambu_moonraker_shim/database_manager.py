"""Moonraker namespace semantics with transactional settings persistence.

The legacy JSON file is imported once and never rewritten. SQLite transactions
protect concurrent read/modify/write operations and survive interrupted saves.
"""

import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterable, Optional, Union, List

Key = Union[str, List[str]]
logger = logging.getLogger(__name__)


class DatabaseError(Exception):
    def __init__(self, code: int, message: str):
        self.code = code
        super().__init__(message)


class DatabaseManager:
    def __init__(self, db_path: str = "moonraker.json", sqlite_path: Optional[str] = None):
        self.db_path = db_path  # Legacy import source; kept for existing installations.
        self.sqlite_path = sqlite_path or ("bambu_shim.db" if db_path == "moonraker.json" else os.path.splitext(db_path)[0] + ".db")
        with self._connection() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS shim_namespace_store "
                         "(namespace TEXT PRIMARY KEY, value TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS shim_database_info "
                         "(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            if not conn.execute("SELECT 1 FROM shim_database_info WHERE key='json_import'").fetchone():
                self._import_legacy(conn)

    @contextmanager
    def _connection(self):
        conn = sqlite3.connect(self.sqlite_path, timeout=30)
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
        finally:
            conn.close()

    @staticmethod
    def _namespace(namespace: str):
        if not isinstance(namespace, str) or not namespace:
            raise DatabaseError(400, "Namespace must be a non-empty string")

    @staticmethod
    def _parts(key: Key):
        parts = key.split(".") if isinstance(key, str) else key
        if not isinstance(parts, list) or not parts or any(not isinstance(p, str) or not p for p in parts):
            raise DatabaseError(400, "Key must be a non-empty string or array of non-empty strings")
        return parts

    @staticmethod
    def _assign(value, parts, item):
        current = value
        for part in parts[:-1]:
            if part not in current:
                current[part] = {}
            if not isinstance(current[part], dict):
                raise DatabaseError(400, f"Cannot insert beneath non-object key: {part}")
            current = current[part]
        current[parts[-1]] = item

    def _import_legacy(self, conn):
        legacy = {}
        if os.path.exists(self.db_path) and os.path.getsize(self.db_path):
            # Fail visibly on corrupt input; never overwrite a damaged source or
            # mark it as imported, so it can be repaired and retried.
            with open(self.db_path, encoding="utf-8") as source:
                legacy = json.load(source)
            if not isinstance(legacy, dict) or any(not isinstance(v, dict) for v in legacy.values()):
                raise ValueError("Legacy database must contain namespace objects")
        for namespace, value in legacy.items():
            if namespace in {"mainsail", "fluidd"}:
                # Old releases stored dotted keys literally. Replaying in write
                # order restores parent replacement and later child updates.
                nested = {}
                for key, item in value.items():
                    parts = self._parts(key)
                    try:
                        self._assign(nested, parts, item)
                    except DatabaseError:
                        # Retain the conflicting entry as a literal key rather
                        # than losing user data during migration.
                        logger.warning("Preserving conflicting legacy key %s.%s", namespace, key)
                        nested[key] = item
                value = nested
            self._store(conn, namespace, value)
        conn.execute("INSERT INTO shim_database_info VALUES ('json_import', 'complete')")

    @staticmethod
    def _store(conn, namespace, value):
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        conn.execute("INSERT OR REPLACE INTO shim_namespace_store VALUES (?, ?)", (namespace, encoded))

    @staticmethod
    def _read(conn, namespace):
        row = conn.execute("SELECT value FROM shim_namespace_store WHERE namespace=?", (namespace,)).fetchone()
        return json.loads(row[0]) if row else None

    def get_item(self, namespace: str, key: Optional[Key] = None, *, strict: bool = False) -> Any:
        self._namespace(namespace)
        parts = self._parts(key) if key is not None else []
        with self._connection() as conn:
            value = self._read(conn, namespace)
        missing = value is None
        for part in parts:
            if not isinstance(value, dict) or part not in value:
                missing = True
                break
            value = value[part]
        if missing:
            if strict:
                raise DatabaseError(404, f"Database item not found: {namespace} {key!r}")
            return None
        return value

    def post_item(self, namespace: str, key: Key, value: Any):
        self._namespace(namespace)
        parts = self._parts(key)
        with self._connection() as conn:
            data = self._read(conn, namespace)
            data = {} if data is None else data
            self._assign(data, parts, value)
            self._store(conn, namespace, data)
        return value

    def delete_item(self, namespace: str, key: Key):
        self._namespace(namespace)
        parts = self._parts(key)
        with self._connection() as conn:
            data = self._read(conn, namespace)
            current = data
            for part in parts[:-1]:
                if not isinstance(current, dict) or part not in current:
                    raise DatabaseError(404, "Database item not found")
                current = current[part]
            if not isinstance(current, dict) or parts[-1] not in current:
                raise DatabaseError(404, "Database item not found")
            removed = current.pop(parts[-1])
            if data:
                self._store(conn, namespace, data)
            else:
                conn.execute("DELETE FROM shim_namespace_store WHERE namespace=?", (namespace,))
        return removed

    def get_namespaces(self):
        with self._connection() as conn:
            return [row[0] for row in conn.execute("SELECT namespace FROM shim_namespace_store ORDER BY namespace")]

    def ensure_namespaces(self, namespaces: Iterable[str]):
        with self._connection() as conn:
            for namespace in namespaces:
                self._namespace(namespace)
                conn.execute("INSERT OR IGNORE INTO shim_namespace_store VALUES (?, '{}')", (namespace,))


database_manager = DatabaseManager(
    os.getenv("MOONRAKER_DB_PATH", "moonraker.json"),
    os.getenv("SHIM_DB_PATH", "bambu_shim.db"),
)
