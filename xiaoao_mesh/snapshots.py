from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .planner import query_key


class SnapshotStore:
    def __init__(self, path: str | None = None):
        self.path = Path(path or os.getenv("FLIGHT_MESH_SNAPSHOT_DB", "/data/flight-mesh.sqlite3"))
        self._lock = threading.Lock()
        self._ready = False

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5)
        if not self._ready:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS snapshots (
                    query_key TEXT PRIMARY KEY,
                    observed_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                )
            """)
            connection.commit()
            self._ready = True
        return connection

    def put(self, query: dict[str, Any], results: list[dict[str, Any]], observed_at: str) -> None:
        if not results:
            return
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO snapshots(query_key, observed_at, payload) VALUES (?, ?, ?)",
                (query_key(query), observed_at, json.dumps(results, ensure_ascii=False)),
            )
            connection.commit()

    def get(self, query: dict[str, Any], max_age_hours: int = 72) -> tuple[str, list[dict[str, Any]]] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT observed_at, payload FROM snapshots WHERE query_key = ?", (query_key(query),)
            ).fetchone()
        if not row:
            return None
        try:
            observed = datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - observed).total_seconds()
            if age < 0 or age > max(1, max_age_hours) * 3600:
                return None
            values = json.loads(row[1])
            return str(row[0]), values if isinstance(values, list) else []
        except (ValueError, TypeError, json.JSONDecodeError):
            return None
