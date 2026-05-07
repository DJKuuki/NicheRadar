from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import time


class HttpCache:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def get(self, key: str) -> str | None:
        now = time.time()
        with closing(sqlite3.connect(self.path)) as connection:
            row = connection.execute(
                "SELECT response_text, expires_at FROM http_cache WHERE key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        response_text, expires_at = row
        if float(expires_at) <= now:
            return None
        return str(response_text)

    def set(self, key: str, response_text: str, ttl_seconds: float) -> None:
        if ttl_seconds <= 0:
            return
        expires_at = time.time() + ttl_seconds
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(
                """
                INSERT INTO http_cache (key, response_text, expires_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    response_text = excluded.response_text,
                    expires_at = excluded.expires_at,
                    updated_at = excluded.updated_at
                """,
                (key, response_text, expires_at, time.time()),
            )
            connection.commit()

    def _init_schema(self) -> None:
        with closing(sqlite3.connect(self.path)) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS http_cache (
                    key TEXT PRIMARY KEY,
                    response_text TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_http_cache_expires_at
                ON http_cache(expires_at);
                """
            )
            connection.commit()


class RateLimiter:
    def __init__(self, min_interval_seconds: float) -> None:
        self.min_interval_seconds = max(0.0, min_interval_seconds)
        self._last_request_at = 0.0

    def wait(self) -> None:
        if self.min_interval_seconds <= 0:
            return
        now = time.time()
        wait_seconds = self.min_interval_seconds - (now - self._last_request_at)
        if wait_seconds > 0:
            time.sleep(wait_seconds)
        self._last_request_at = time.time()
