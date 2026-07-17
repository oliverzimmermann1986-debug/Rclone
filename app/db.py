"""SQLite-Datenbank für Jobs, Pair-Ergebnisse, Scheduler und Login-Schutz."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

_DB_PATH = Path(os.getenv("RCLONE_SYNC_DB", "/opt/rclone-sync/data/rclone-sync.db"))
_singleton_lock = threading.Lock()

_DDL = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    started_at REAL NOT NULL,
    ended_at REAL,
    summary_json TEXT,
    log_file TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_kind_started ON jobs(kind, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_status_started ON jobs(status, started_at DESC);

CREATE TABLE IF NOT EXISTS pair_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    pair_name TEXT NOT NULL,
    ok INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    started_at REAL NOT NULL,
    ended_at REAL,
    result_json TEXT,
    UNIQUE(job_id, pair_name)
);
CREATE INDEX IF NOT EXISTS idx_pair_runs_name_ended ON pair_runs(pair_name, ended_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_pair_runs_name_ok_ended ON pair_runs(pair_name, ok, ended_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS auth_failures (
    client_key TEXT PRIMARY KEY,
    window_started_at REAL NOT NULL,
    failure_count INTEGER NOT NULL DEFAULT 0,
    blocked_until REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_auth_failures_updated ON auth_failures(updated_at);

CREATE TABLE IF NOT EXISTS runtime_settings (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT 'system',
    created_at REAL NOT NULL,
    details_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_events_created ON audit_events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_events_type_created ON audit_events(event_type, created_at DESC);
"""


class Database:
    def __init__(self, path: Path = _DB_PATH):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        with self.conn(initialize=True) as connection:
            connection.executescript(_DDL)
            self._backfill_pair_runs(connection)
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass

    @classmethod
    def _backfill_pair_runs(cls, connection: sqlite3.Connection) -> None:
        """Migriert bestehende JSON-Historie einmalig in die indexierte Pair-Tabelle."""
        count = int(connection.execute("SELECT COUNT(*) FROM pair_runs").fetchone()[0])
        if count:
            return
        rows = connection.execute(
            "SELECT id, status, started_at, ended_at, summary_json FROM jobs "
            "WHERE summary_json IS NOT NULL ORDER BY id"
        ).fetchall()
        for row in rows:
            try:
                summary = json.loads(row["summary_json"] or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(summary, dict):
                continue
            cls._store_pair_results(
                connection,
                int(row["id"]),
                str(row["status"]),
                float(row["started_at"]),
                float(row["ended_at"] or row["started_at"]),
                summary,
            )

    @contextmanager
    def conn(self, *, initialize: bool = False) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            str(self.path), timeout=30, check_same_thread=False
        )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA busy_timeout=30000")
            connection.execute("PRAGMA foreign_keys=ON")
            if initialize:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=NORMAL")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def job_start(self, kind: str, log_file: Optional[str] = None) -> int:
        if not kind or len(kind) > 64:
            raise ValueError("Ungültiger Job-Typ")
        with self.conn() as connection:
            cursor = connection.execute(
                "INSERT INTO jobs (kind, status, started_at, log_file) VALUES (?, 'running', ?, ?)",
                (kind, time.time(), log_file),
            )
            return int(cursor.lastrowid)

    def job_finish(
        self, job_id: int, status: str, summary: Optional[Dict[str, Any]] = None
    ) -> None:
        if status not in {"running", "ok", "error", "skipped", "cancelled", "stale"}:
            raise ValueError(f"Ungültiger Job-Status: {status}")
        ended_at = time.time()
        payload = (
            json.dumps(summary, ensure_ascii=False, default=str)
            if summary is not None
            else None
        )
        with self.conn() as connection:
            row = connection.execute(
                "SELECT started_at FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            if not row:
                raise ValueError(f"Job nicht gefunden: {job_id}")
            started_at = float(row["started_at"])
            connection.execute(
                "UPDATE jobs SET status=?, ended_at=?, summary_json=? WHERE id=?",
                (status, ended_at, payload, job_id),
            )
            self._store_pair_results(
                connection, job_id, status, started_at, ended_at, summary or {}
            )

    @staticmethod
    def _store_pair_results(
        connection: sqlite3.Connection,
        job_id: int,
        job_status: str,
        started_at: float,
        ended_at: float,
        summary: Dict[str, Any],
    ) -> None:
        pairs = summary.get("pairs") or []
        if not isinstance(pairs, list):
            pairs = []
        for pair in pairs:
            if not isinstance(pair, dict):
                continue
            name = str(pair.get("name") or "").strip()
            if not name:
                continue
            pair_status = (
                "ok"
                if pair.get("ok") is True
                else (
                    "cancelled"
                    if pair.get("cancelled")
                    else ("skipped" if pair.get("skipped") else "error")
                )
            )
            connection.execute(
                "INSERT INTO pair_runs (job_id, pair_name, ok, status, started_at, ended_at, result_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(job_id, pair_name) DO UPDATE SET "
                "ok=excluded.ok, status=excluded.status, ended_at=excluded.ended_at, result_json=excluded.result_json",
                (
                    job_id,
                    name,
                    1 if pair.get("ok") is True else 0,
                    pair_status,
                    started_at,
                    ended_at,
                    json.dumps(pair, ensure_ascii=False, default=str),
                ),
            )

        due = summary.get("due") or []
        if job_status in {"error", "stale", "cancelled"} and isinstance(due, list):
            for name_value in due:
                name = str(name_value or "").strip()
                if not name:
                    continue
                result = {"name": name, "ok": False, "error": summary.get("error")}
                connection.execute(
                    "INSERT OR IGNORE INTO pair_runs "
                    "(job_id, pair_name, ok, status, started_at, ended_at, result_json) VALUES (?, ?, 0, ?, ?, ?, ?)",
                    (
                        job_id,
                        name,
                        job_status,
                        started_at,
                        ended_at,
                        json.dumps(result, ensure_ascii=False, default=str),
                    ),
                )

    def job_set_log_file(self, job_id: int, path: str) -> None:
        with self.conn() as connection:
            connection.execute("UPDATE jobs SET log_file=? WHERE id=?", (path, job_id))

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
        data = dict(row)
        if data.get("summary_json"):
            try:
                data["summary"] = json.loads(data["summary_json"])
            except (json.JSONDecodeError, TypeError):
                data["summary"] = None
        data.pop("summary_json", None)
        return data

    @staticmethod
    def _pair_row_to_result(row: sqlite3.Row) -> Dict[str, Any]:
        data = dict(row)
        try:
            pair = json.loads(data.get("result_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            pair = {}
        return {
            "ok": bool(data.get("ok")),
            "status": data.get("status"),
            "started_at": data.get("started_at"),
            "ended_at": data.get("ended_at") or data.get("started_at"),
            "pair": pair,
            "job_id": data.get("job_id"),
        }

    def job_get(self, job_id: int) -> Optional[Dict[str, Any]]:
        with self.conn() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            return self._row_to_dict(row) if row else None

    def job_list(
        self,
        kind: Optional[str] = None,
        limit: int = 50,
        *,
        status: Optional[str] = None,
        offset: int = 0,
        query: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit or 50), 2000))
        offset = max(0, min(int(offset or 0), 1_000_000))
        clauses: list[str] = []
        params: list[Any] = []
        if kind:
            clauses.append("kind=?")
            params.append(kind)
        if status:
            clauses.append("status=?")
            params.append(status)
        needle = str(query or "").strip().casefold()
        if needle:
            escaped = (
                needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            clauses.append(
                "(CAST(id AS TEXT)=? OR LOWER(COALESCE(summary_json, '')) LIKE ? ESCAPE '\\' "
                "OR LOWER(COALESCE(log_file, '')) LIKE ? ESCAPE '\\')"
            )
            params.extend([needle, f"%{escaped}%", f"%{escaped}%"])
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.extend([limit, offset])
        with self.conn() as connection:
            rows = connection.execute(
                f"SELECT * FROM jobs{where} ORDER BY started_at DESC LIMIT ? OFFSET ?",
                tuple(params),
            ).fetchall()
            return [self._row_to_dict(row) for row in rows]

    def job_count(
        self,
        *,
        kind: Optional[str] = None,
        status: Optional[str] = None,
        query: Optional[str] = None,
    ) -> int:
        clauses: list[str] = []
        params: list[Any] = []
        if kind:
            clauses.append("kind=?")
            params.append(kind)
        if status:
            clauses.append("status=?")
            params.append(status)
        needle = str(query or "").strip().casefold()
        if needle:
            escaped = (
                needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            clauses.append(
                "(CAST(id AS TEXT)=? OR LOWER(COALESCE(summary_json, '')) LIKE ? ESCAPE '\\' "
                "OR LOWER(COALESCE(log_file, '')) LIKE ? ESCAPE '\\')"
            )
            params.extend([needle, f"%{escaped}%", f"%{escaped}%"])
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.conn() as connection:
            return int(
                connection.execute(
                    f"SELECT COUNT(*) FROM jobs{where}", tuple(params)
                ).fetchone()[0]
            )

    def job_statistics(self, *, since: Optional[float] = None) -> Dict[str, Any]:
        clauses = ["1=1"]
        params: list[Any] = []
        if since is not None:
            clauses.append("started_at>=?")
            params.append(float(since))
        where = " AND ".join(clauses)
        with self.conn() as connection:
            rows = connection.execute(
                f"SELECT status, COUNT(*) AS count FROM jobs WHERE {where} GROUP BY status",
                tuple(params),
            ).fetchall()
            kinds = connection.execute(
                f"SELECT kind, COUNT(*) AS count FROM jobs WHERE {where} GROUP BY kind",
                tuple(params),
            ).fetchall()
        return {
            "total": sum(int(row["count"]) for row in rows),
            "by_status": {str(row["status"]): int(row["count"]) for row in rows},
            "by_kind": {str(row["kind"]): int(row["count"]) for row in kinds},
        }

    def job_running(self, kind: str) -> Optional[Dict[str, Any]]:
        with self.conn() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE kind=? AND status='running' ORDER BY started_at DESC LIMIT 1",
                (kind,),
            ).fetchone()
            return self._row_to_dict(row) if row else None

    def jobs_mark_all_running_stale(
        self, *, reason: str = "Laufender Prozess nicht mehr aktiv"
    ) -> int:
        now = time.time()
        summary = json.dumps({"error": reason, "recovered_at": now}, ensure_ascii=False)
        with self.conn() as connection:
            rows = connection.execute(
                "SELECT id FROM jobs WHERE status='running'"
            ).fetchall()
            cursor = connection.execute(
                "UPDATE jobs SET status='stale', ended_at=?, summary_json=? WHERE status='running'",
                (now, summary),
            )
            for row in rows:
                connection.execute(
                    "UPDATE pair_runs SET status='stale', ended_at=? WHERE job_id=?",
                    (now, int(row["id"])),
                )
            return int(cursor.rowcount or 0)

    def jobs_mark_stale(
        self,
        max_age_sec: int,
        *,
        reason: str = "Prozess beendet oder Timeout überschritten",
    ) -> int:
        cutoff = time.time() - max(60, int(max_age_sec))
        now = time.time()
        summary = json.dumps({"error": reason, "recovered_at": now}, ensure_ascii=False)
        with self.conn() as connection:
            stale_rows = connection.execute(
                "SELECT id, started_at FROM jobs WHERE status='running' AND started_at < ?",
                (cutoff,),
            ).fetchall()
            cursor = connection.execute(
                "UPDATE jobs SET status='stale', ended_at=?, summary_json=? WHERE status='running' AND started_at < ?",
                (now, summary, cutoff),
            )
            for row in stale_rows:
                connection.execute(
                    "UPDATE pair_runs SET status='stale', ended_at=? WHERE job_id=? AND status='running'",
                    (now, int(row["id"])),
                )
            return int(cursor.rowcount or 0)

    def pair_last_result(
        self, pair_name: str, *, limit: int = 1000
    ) -> Optional[Dict[str, Any]]:
        with self.conn() as connection:
            row = connection.execute(
                "SELECT * FROM pair_runs WHERE pair_name=? ORDER BY ended_at DESC, id DESC LIMIT 1",
                (pair_name,),
            ).fetchone()
            if row:
                return self._pair_row_to_result(row)
        return self._legacy_pair_last_result(pair_name, success_only=False, limit=limit)

    def pair_last_success(
        self, pair_name: str, *, limit: int = 1000
    ) -> Optional[Dict[str, Any]]:
        with self.conn() as connection:
            row = connection.execute(
                "SELECT * FROM pair_runs WHERE pair_name=? AND ok=1 ORDER BY ended_at DESC, id DESC LIMIT 1",
                (pair_name,),
            ).fetchone()
            if row:
                result = self._pair_row_to_result(row)
                return {
                    "started_at": result["started_at"],
                    "ended_at": result["ended_at"],
                    "pair": result["pair"],
                    "job_id": result["job_id"],
                }
        legacy = self._legacy_pair_last_result(
            pair_name, success_only=True, limit=limit
        )
        if not legacy:
            return None
        return {
            "started_at": legacy["started_at"],
            "ended_at": legacy["ended_at"],
            "pair": legacy["pair"],
            "job_id": legacy["job_id"],
        }

    def pair_last_results(self) -> Dict[str, Dict[str, Any]]:
        """Letztes Ergebnis je Pair über eine indexierte Abfrage."""
        with self.conn() as connection:
            rows = connection.execute(
                "SELECT pr.* FROM pair_runs pr "
                "JOIN (SELECT pair_name, MAX(ended_at) AS max_ended FROM pair_runs GROUP BY pair_name) latest "
                "ON latest.pair_name=pr.pair_name AND latest.max_ended=pr.ended_at "
                "ORDER BY pr.id DESC"
            ).fetchall()
        result: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            name = str(row["pair_name"])
            if name not in result:
                result[name] = self._pair_row_to_result(row)
        return result

    def pair_last_successes(self) -> Dict[str, Dict[str, Any]]:
        """Letzter Erfolg je Pair über eine indexierte Abfrage."""
        with self.conn() as connection:
            rows = connection.execute(
                "SELECT pr.* FROM pair_runs pr "
                "JOIN (SELECT pair_name, MAX(ended_at) AS max_ended FROM pair_runs WHERE ok=1 GROUP BY pair_name) latest "
                "ON latest.pair_name=pr.pair_name AND latest.max_ended=pr.ended_at "
                "WHERE pr.ok=1 ORDER BY pr.id DESC"
            ).fetchall()
        result: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            name = str(row["pair_name"])
            if name not in result:
                result[name] = self._pair_row_to_result(row)
        return result

    def _legacy_pair_last_result(
        self, pair_name: str, *, success_only: bool, limit: int
    ) -> Optional[Dict[str, Any]]:
        for job in self.job_list(kind="backup", limit=limit):
            summary = job.get("summary") or {}
            for pair in summary.get("pairs") or []:
                if pair.get("name") == pair_name and (
                    not success_only or pair.get("ok") is True
                ):
                    return {
                        "ok": pair.get("ok") is True,
                        "status": job.get("status"),
                        "started_at": job.get("started_at"),
                        "ended_at": job.get("ended_at") or job.get("started_at"),
                        "pair": pair,
                        "job_id": job.get("id"),
                    }
            due = summary.get("due") or []
            if (
                not success_only
                and job.get("status") in {"error", "stale", "cancelled"}
                and pair_name in due
            ):
                return {
                    "ok": False,
                    "status": job.get("status"),
                    "started_at": job.get("started_at"),
                    "ended_at": job.get("ended_at") or job.get("started_at"),
                    "pair": {
                        "name": pair_name,
                        "ok": False,
                        "error": summary.get("error"),
                    },
                    "job_id": job.get("id"),
                }
        return None

    def jobs_delete_failed(self) -> int:
        with self.conn() as connection:
            cursor = connection.execute(
                "DELETE FROM jobs WHERE status IN ('error', 'stale', 'cancelled')"
            )
            return int(cursor.rowcount or 0)

    def jobs_prune(self, older_than_days: int, keep_latest: int = 100) -> int:
        cutoff = time.time() - max(1, int(older_than_days)) * 86400
        keep_latest = max(0, int(keep_latest))
        with self.conn() as connection:
            cursor = connection.execute(
                "DELETE FROM jobs WHERE started_at < ? AND id NOT IN "
                "(SELECT id FROM jobs ORDER BY started_at DESC LIMIT ?)",
                (cutoff, keep_latest),
            )
            return int(cursor.rowcount or 0)

    def auth_retry_after(self, client_key: str, *, now: Optional[float] = None) -> int:
        now_value = float(time.time() if now is None else now)
        with self.conn() as connection:
            row = connection.execute(
                "SELECT blocked_until FROM auth_failures WHERE client_key=?",
                (client_key,),
            ).fetchone()
            if not row:
                return 0
            blocked_until = float(row["blocked_until"] or 0)
            if blocked_until > now_value:
                return max(1, int(blocked_until - now_value))
            return 0

    def auth_record_failure(
        self,
        client_key: str,
        *,
        window_sec: int,
        max_failures: int,
        lock_sec: int,
        now: Optional[float] = None,
    ) -> int:
        now_value = float(time.time() if now is None else now)
        with self.conn() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM auth_failures WHERE client_key=?", (client_key,)
            ).fetchone()
            if not row or now_value - float(row["window_started_at"]) > window_sec:
                window_started = now_value
                count = 1
                blocked_until = 0.0
            else:
                window_started = float(row["window_started_at"])
                count = int(row["failure_count"] or 0) + 1
                blocked_until = float(row["blocked_until"] or 0)
            if count >= max_failures:
                blocked_until = max(blocked_until, now_value + lock_sec)
                count = 0
                window_started = now_value
            connection.execute(
                "INSERT INTO auth_failures(client_key, window_started_at, failure_count, blocked_until, updated_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(client_key) DO UPDATE SET "
                "window_started_at=excluded.window_started_at, failure_count=excluded.failure_count, "
                "blocked_until=excluded.blocked_until, updated_at=excluded.updated_at",
                (client_key, window_started, count, blocked_until, now_value),
            )
            return max(0, int(blocked_until - now_value))

    def auth_clear(self, client_key: str) -> None:
        with self.conn() as connection:
            connection.execute(
                "DELETE FROM auth_failures WHERE client_key=?", (client_key,)
            )

    def auth_prune(self, older_than_days: int = 7) -> int:
        cutoff = time.time() - max(1, int(older_than_days)) * 86400
        with self.conn() as connection:
            cursor = connection.execute(
                "DELETE FROM auth_failures WHERE updated_at < ?", (cutoff,)
            )
            return int(cursor.rowcount or 0)

    def runtime_get(self, key: str, default: Any = None) -> Any:
        if not key or len(key) > 128:
            raise ValueError("Ungültiger Runtime-Key")
        with self.conn() as connection:
            row = connection.execute(
                "SELECT value_json FROM runtime_settings WHERE key=?", (key,)
            ).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value_json"])
        except (json.JSONDecodeError, TypeError):
            return default

    def runtime_set(self, key: str, value: Any) -> None:
        if not key or len(key) > 128:
            raise ValueError("Ungültiger Runtime-Key")
        payload = json.dumps(value, ensure_ascii=False, default=str)
        if len(payload.encode("utf-8")) > 64 * 1024:
            raise ValueError("Runtime-Wert ist zu groß")
        now = time.time()
        with self.conn() as connection:
            connection.execute(
                "INSERT INTO runtime_settings(key, value_json, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
                (key, payload, now),
            )

    def runtime_delete(self, key: str) -> None:
        with self.conn() as connection:
            connection.execute("DELETE FROM runtime_settings WHERE key=?", (key,))

    def audit_add(
        self,
        event_type: str,
        *,
        actor: str = "system",
        details: Optional[Dict[str, Any]] = None,
    ) -> int:
        event = str(event_type or "").strip()
        if not event or len(event) > 80:
            raise ValueError("Ungültiger Audit-Ereignistyp")
        actor_value = str(actor or "system").strip()[:128] or "system"
        payload = json.dumps(details or {}, ensure_ascii=False, default=str)
        if len(payload.encode("utf-8")) > 128 * 1024:
            payload = json.dumps(
                {"note": "Details wegen Größe verworfen"}, ensure_ascii=False
            )
        with self.conn() as connection:
            cursor = connection.execute(
                "INSERT INTO audit_events(event_type, actor, created_at, details_json) VALUES (?, ?, ?, ?)",
                (event, actor_value, time.time(), payload),
            )
            return int(cursor.lastrowid)

    def audit_list(
        self, *, limit: int = 100, event_type: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit or 100), 1000))
        params: list[Any] = []
        where = ""
        if event_type:
            where = " WHERE event_type=?"
            params.append(str(event_type))
        params.append(limit)
        with self.conn() as connection:
            rows = connection.execute(
                f"SELECT * FROM audit_events{where} ORDER BY created_at DESC, id DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        result: List[Dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                item["details"] = json.loads(item.get("details_json") or "{}")
            except (json.JSONDecodeError, TypeError):
                item["details"] = {}
            item.pop("details_json", None)
            result.append(item)
        return result

    def audit_prune(self, older_than_days: int = 365, keep_latest: int = 1000) -> int:
        days = max(1, min(int(older_than_days), 3650))
        keep = max(100, min(int(keep_latest), 100000))
        cutoff = time.time() - days * 86400
        with self.conn() as connection:
            cursor = connection.execute(
                "DELETE FROM audit_events WHERE created_at < ? AND id NOT IN "
                "(SELECT id FROM audit_events ORDER BY created_at DESC LIMIT ?)",
                (cutoff, keep),
            )
            return int(cursor.rowcount or 0)

    def integrity_check(self) -> Dict[str, Any]:
        with self.conn() as connection:
            result = str(connection.execute("PRAGMA quick_check").fetchone()[0])
            fk_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
        return {
            "ok": result == "ok" and not fk_errors,
            "quick_check": result,
            "foreign_key_errors": len(fk_errors),
        }

    def stats(self) -> Dict[str, Any]:
        with self.conn() as connection:
            jobs = int(connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
            pairs = int(
                connection.execute("SELECT COUNT(*) FROM pair_runs").fetchone()[0]
            )
            auth = int(
                connection.execute("SELECT COUNT(*) FROM auth_failures").fetchone()[0]
            )
            running = int(
                connection.execute(
                    "SELECT COUNT(*) FROM jobs WHERE status='running'"
                ).fetchone()[0]
            )
            audit = int(
                connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
            )
            runtime = int(
                connection.execute("SELECT COUNT(*) FROM runtime_settings").fetchone()[
                    0
                ]
            )
        try:
            size = self.path.stat().st_size
        except OSError:
            size = 0
        return {
            "jobs": jobs,
            "pair_runs": pairs,
            "auth_failures": auth,
            "running": running,
            "audit_events": audit,
            "runtime_settings": runtime,
            "bytes": size,
        }

    def checkpoint(self) -> None:
        with self.conn() as connection:
            connection.execute("PRAGMA wal_checkpoint(PASSIVE)")


_db: Optional[Database] = None


def get_db() -> Database:
    global _db
    if _db is None:
        with _singleton_lock:
            if _db is None:
                _db = Database()
    return _db
