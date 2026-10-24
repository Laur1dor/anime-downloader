"""SQLite-кэш: hash -> telegram file_id, чтобы не скачивать одно и то же дважды."""
from __future__ import annotations

import sqlite3
from pathlib import Path


class CacheStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._conn = sqlite3.connect(str(db_path))
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cache (
                hash        TEXT PRIMARY KEY,
                file_id     TEXT NOT NULL,
                title       TEXT,
                quality     INTEGER,
                size_bytes  INTEGER,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self._conn.execute("""CREATE TABLE IF NOT EXISTS upload_parts (
            session TEXT, part INTEGER, file_id TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (session, part))""")
        self._conn.execute("DELETE FROM upload_parts WHERE created_at < datetime('now', '-2 days')")
        self._conn.commit()

    def get(self, key: str, expected_quality: int | None = None) -> str | None:
        # Older versions could hash the requested quality while saving a lower one.
        # Validate the stored media quality before reusing those historical rows.
        if expected_quality is None:
            cur = self._conn.execute("SELECT file_id FROM cache WHERE hash = ?", (key,))
        else:
            cur = self._conn.execute(
                "SELECT file_id FROM cache WHERE hash = ? AND quality = ?", (key, expected_quality))
        row = cur.fetchone()
        return row[0] if row else None

    def put(self, key: str, file_id: str, title: str = "",
            quality: int = 0, size_bytes: int = 0) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO cache (hash, file_id, title, quality, size_bytes) "
            "VALUES (?, ?, ?, ?, ?)",
            (key, file_id, title, quality, size_bytes),
        )
        self._conn.commit()

    def get_parts(self, session: str) -> dict[int, str]:
        return dict(self._conn.execute("SELECT part, file_id FROM upload_parts WHERE session=?", (session,)))

    def put_part(self, session: str, part: int, file_id: str):
        self._conn.execute("INSERT OR REPLACE INTO upload_parts(session,part,file_id) VALUES(?,?,?)",
                           (session, part, file_id))
        self._conn.commit()

    def stats(self) -> tuple[int, int]:
        cur = self._conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(size_bytes), 0) FROM cache"
        )
        count, total = cur.fetchone()
        return int(count), int(total or 0)

    def clear(self) -> int:
        cur = self._conn.execute("SELECT COUNT(*) FROM cache")
        n = cur.fetchone()[0]
        self._conn.execute("DELETE FROM cache")
        self._conn.execute("DELETE FROM upload_parts")
        self._conn.commit()
        return int(n)

    def close(self) -> None:
        self._conn.close()
