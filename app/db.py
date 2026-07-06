"""SQLite-DB für Job-Tracking. Job = ein rclone-Lauf."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, List, Optional


_DB_PATH = Path(os.getenv("RCLONE_SYNC_DB", "/opt/rclone-sync/data/rclone-sync.db"))
_lock = threading.Lock()


_DDL = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,                      -- backup | quicksync
    status TEXT NOT NULL DEFAULT 'running',  -- running | ok | error | skipped
    started_at REAL NOT NULL,
    ended_at REAL,
    summary_json TEXT,
    log_file TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_kind_started ON jobs(kind, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_status_started ON jobs(status, started_at DESC);
"""


class Database:
    def __init__(self, path: Path = _DB_PATH):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        c0 = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
        try:
            c0.execute("PRAGMA journal_mode=WAL")
            c0.execute("PRAGMA synchronous=NORMAL")
            c0.execute("PRAGMA busy_timeout=30000")
            c0.executescript(_DDL)
            c0.commit()
        finally:
            c0.close()

    @contextmanager
    def conn(self):
        c = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False)
        c.row_factory = sqlite3.Row
        try:
            c.execute("PRAGMA busy_timeout=30000")
            yield c
            c.commit()
        finally:
            c.close()

    def job_start(self, kind: str, log_file: Optional[str] = None) -> int:
        with self.conn() as c:
            cur = c.execute(
                "INSERT INTO jobs (kind, status, started_at, log_file) VALUES (?, 'running', ?, ?)",
                (kind, time.time(), log_file),
            )
            return int(cur.lastrowid)

    def job_finish(self, job_id: int, status: str, summary: Optional[Dict] = None) -> None:
        with self.conn() as c:
            c.execute(
                "UPDATE jobs SET status=?, ended_at=?, summary_json=? WHERE id=?",
                (status, time.time(), json.dumps(summary) if summary else None, job_id),
            )

    def job_set_log_file(self, job_id: int, path: str) -> None:
        with self.conn() as c:
            c.execute("UPDATE jobs SET log_file=? WHERE id=?", (path, job_id))

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> Dict:
        d = dict(row)
        if d.get("summary_json"):
            try:
                d["summary"] = json.loads(d["summary_json"])
            except Exception:
                d["summary"] = None
        d.pop("summary_json", None)
        return d

    def job_get(self, job_id: int) -> Optional[Dict]:
        with self.conn() as c:
            r = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            return self._row_to_dict(r) if r else None

    def job_list(self, kind: Optional[str] = None, limit: int = 50) -> List[Dict]:
        limit = max(1, min(int(limit or 50), 500))
        with self.conn() as c:
            if kind:
                rows = c.execute(
                    "SELECT * FROM jobs WHERE kind=? ORDER BY started_at DESC LIMIT ?",
                    (kind, limit),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM jobs ORDER BY started_at DESC LIMIT ?", (limit,),
                ).fetchall()
            return [self._row_to_dict(r) for r in rows]

    def job_running(self, kind: str) -> Optional[Dict]:
        with self.conn() as c:
            r = c.execute(
                "SELECT * FROM jobs WHERE kind=? AND status='running' "
                "ORDER BY started_at DESC LIMIT 1",
                (kind,),
            ).fetchone()
            return self._row_to_dict(r) if r else None

    def jobs_delete_failed(self) -> int:
        with self.conn() as c:
            cur = c.execute("DELETE FROM jobs WHERE status='error'")
            return cur.rowcount or 0


_db: Optional[Database] = None


def get_db() -> Database:
    global _db
    if _db is None:
        with _lock:
            if _db is None:
                _db = Database()
    return _db
