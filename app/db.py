"""SQLite-Datenbank für Jobs, Pair-Ergebnisse, Scheduler und Login-Schutz."""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional

_DB_PATH = Path(os.getenv("RCLONE_SYNC_DB", "/opt/rclone-sync/data/rclone-sync.db"))
_singleton_lock = threading.Lock()
_SCHEMA_VERSION = 13
_MAX_JOB_SUMMARY_BYTES = 256 * 1024
_MAX_PAIR_RESULT_BYTES = 32 * 1024
# restoretest liest vom Ziel und schreibt nur in ein Temp-Verzeichnis, teilt
# sich aber bewusst den Backup-Scope: Ein Drill während eines laufenden Syncs
# würde einen inkonsistenten Zwischenstand prüfen und Bandbreite streitig machen.
_BACKUP_SCOPE_KINDS = ("backup", "check", "quicksync", "restoretest", "recovery")
_JOB_SCOPE_KINDS = {
    "backup": _BACKUP_SCOPE_KINDS,
    "check": _BACKUP_SCOPE_KINDS,
    "quicksync": _BACKUP_SCOPE_KINDS,
    "restoretest": _BACKUP_SCOPE_KINDS,
    "recovery": _BACKUP_SCOPE_KINDS,
    "pbs": ("pbs",),
}


class JobAlreadyRunningError(RuntimeError):
    """Der exklusive Job-Scope ist bereits durch eine DB-Reservation belegt."""


_DDL = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    started_at REAL NOT NULL,
    ended_at REAL,
    summary_json TEXT,
    log_file TEXT,
    definition_id TEXT,
    definition_name TEXT,
    config_revision TEXT,
    scheduled_slot TEXT,
    dry_run INTEGER NOT NULL DEFAULT 0,
    trigger TEXT NOT NULL DEFAULT 'manual'
);
CREATE INDEX IF NOT EXISTS idx_jobs_kind_started ON jobs(kind, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_status_started ON jobs(status, started_at DESC);

CREATE TABLE IF NOT EXISTS pair_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    pair_name TEXT NOT NULL,
    history_key TEXT NOT NULL DEFAULT '',
    ok INTEGER NOT NULL DEFAULT 0,
    dry_run INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    started_at REAL NOT NULL,
    ended_at REAL,
    scheduled_slot TEXT,
    result_json TEXT,
    UNIQUE(job_id, pair_name)
);
CREATE INDEX IF NOT EXISTS idx_pair_runs_name_ended ON pair_runs(pair_name, ended_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_pair_runs_name_ok_ended ON pair_runs(pair_name, ok, ended_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS pair_history_state (
    history_key TEXT PRIMARY KEY,
    pair_name TEXT NOT NULL,
    ever_succeeded INTEGER NOT NULL DEFAULT 0,
    terminal_seen INTEGER NOT NULL DEFAULT 0,
    first_seen_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    last_success_at REAL
);
CREATE INDEX IF NOT EXISTS idx_pair_history_state_name ON pair_history_state(pair_name);

CREATE TABLE IF NOT EXISTS job_definition_schedule_state (
    definition_id TEXT PRIMARY KEY,
    definition_name TEXT NOT NULL DEFAULT '',
    last_attempt_job_id INTEGER,
    last_attempt_started_at REAL,
    last_attempt_at REAL,
    last_attempt_status TEXT,
    last_attempt_trigger TEXT,
    last_attempt_scheduled_slot TEXT,
    last_success_job_id INTEGER,
    last_success_started_at REAL,
    last_success_at REAL,
    last_success_trigger TEXT,
    last_success_scheduled_slot TEXT,
    updated_at REAL NOT NULL
);

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

CREATE TABLE IF NOT EXISTS push_devices (
    token TEXT PRIMARY KEY,
    environment TEXT NOT NULL,
    app_version TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_push_devices_updated ON push_devices(updated_at DESC);

CREATE TABLE IF NOT EXISTS push_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key TEXT NOT NULL,
    token TEXT NOT NULL,
    environment TEXT NOT NULL,
    event TEXT NOT NULL,
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    lease_until REAL NOT NULL DEFAULT 0,
    claim_owner TEXT NOT NULL DEFAULT '',
    last_error TEXT,
    apns_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    sent_at REAL,
    UNIQUE(dedupe_key, token)
);
CREATE INDEX IF NOT EXISTS idx_push_outbox_due
ON push_outbox(status, next_attempt_at, lease_until, id);
CREATE INDEX IF NOT EXISTS idx_push_outbox_created
ON push_outbox(created_at DESC);

CREATE TABLE IF NOT EXISTS job_batches (
    id TEXT PRIMARY KEY,
    state TEXT NOT NULL DEFAULT 'running',
    dry_run INTEGER NOT NULL DEFAULT 0,
    config_revision TEXT NOT NULL DEFAULT '',
    snapshot_json TEXT NOT NULL,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    completed_at REAL
);
CREATE INDEX IF NOT EXISTS idx_job_batches_state_created
ON job_batches(state, created_at);

CREATE TABLE IF NOT EXISTS job_batch_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL REFERENCES job_batches(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    definition_id TEXT,
    definition_name TEXT,
    spec_json TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued',
    job_id INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
    error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(batch_id, position)
);
CREATE INDEX IF NOT EXISTS idx_job_batch_items_state
ON job_batch_items(batch_id, state, position);

CREATE TABLE IF NOT EXISTS job_terminal_intents (
    job_id INTEGER PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    applied_at REAL
);
CREATE INDEX IF NOT EXISTS idx_job_terminal_intents_pending
ON job_terminal_intents(applied_at, updated_at);

CREATE TABLE IF NOT EXISTS webauthn_credentials (
    credential_id TEXT PRIMARY KEY,
    method TEXT NOT NULL CHECK(method IN ('passkey', 'security_key')),
    public_key BLOB NOT NULL,
    sign_count INTEGER NOT NULL DEFAULT 0,
    transports_json TEXT NOT NULL DEFAULT '[]',
    device_type TEXT NOT NULL DEFAULT '',
    backed_up INTEGER NOT NULL DEFAULT 0,
    label TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    last_used_at REAL
);
CREATE INDEX IF NOT EXISTS idx_webauthn_credentials_method
ON webauthn_credentials(method, created_at);

CREATE TABLE IF NOT EXISTS webauthn_challenges (
    id TEXT PRIMARY KEY,
    challenge BLOB NOT NULL,
    purpose TEXT NOT NULL CHECK(purpose IN ('register', 'authenticate')),
    method TEXT NOT NULL CHECK(method IN ('passkey', 'security_key')),
    label TEXT NOT NULL DEFAULT '',
    native INTEGER NOT NULL DEFAULT 0,
    app_binding TEXT NOT NULL DEFAULT '',
    expires_at REAL NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_webauthn_challenges_expiry
ON webauthn_challenges(expires_at);

CREATE TABLE IF NOT EXISTS native_auth_exchanges (
    token_hash TEXT PRIMARY KEY,
    verifier_hash TEXT NOT NULL,
    username TEXT NOT NULL,
    expires_at REAL NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_native_auth_exchanges_expiry
ON native_auth_exchanges(expires_at);
"""


def _json_preview(
    value: Any,
    *,
    string_limit: int,
    list_limit: int,
    dict_limit: int,
    depth: int,
) -> Any:
    if isinstance(value, str):
        if len(value) <= string_limit:
            return value
        return value[:string_limit] + "…"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if depth <= 0:
        return str(value)[:string_limit]
    if isinstance(value, dict):
        result: Dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= dict_limit:
                break
            result[str(key)[:128]] = _json_preview(
                item,
                string_limit=string_limit,
                list_limit=list_limit,
                dict_limit=dict_limit,
                depth=depth - 1,
            )
        return result
    if isinstance(value, (list, tuple)):
        return [
            _json_preview(
                item,
                string_limit=string_limit,
                list_limit=list_limit,
                dict_limit=dict_limit,
                depth=depth - 1,
            )
            for item in value[:list_limit]
        ]
    return str(value)[:string_limit]


def _json_dumps_bounded(value: Any, max_bytes: int) -> str:
    """Serialisiert Diagnose-Payloads mit einem harten UTF-8-Bytebudget."""
    raw = json.dumps(value, ensure_ascii=False, default=str)
    raw_size = len(raw.encode("utf-8"))
    if raw_size <= max_bytes:
        return raw

    stages = (
        (4096, 100, 100, 5),
        (1024, 25, 75, 4),
        (256, 5, 40, 3),
        (64, 1, 20, 2),
    )
    for string_limit, list_limit, dict_limit, depth in stages:
        preview = _json_preview(
            value,
            string_limit=string_limit,
            list_limit=list_limit,
            dict_limit=dict_limit,
            depth=depth,
        )
        if isinstance(preview, dict):
            preview["truncated"] = True
            preview["original_bytes"] = raw_size
        else:
            preview = {
                "truncated": True,
                "original_bytes": raw_size,
                "preview": preview,
            }
        candidate = json.dumps(preview, ensure_ascii=False, default=str)
        if len(candidate.encode("utf-8")) <= max_bytes:
            return candidate

    return json.dumps(
        {"truncated": True, "original_bytes": raw_size},
        ensure_ascii=False,
    )


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
            # Serialize schema decisions across concurrently starting processes.
            # Every migration, including its data backfill and version marker,
            # remains in this transaction so a crash can only leave the old
            # user_version behind and the idempotent migration will resume.
            connection.execute("BEGIN IMMEDIATE")
            self._migrate_schema(connection)
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass

    @staticmethod
    def _default_history_key(job_kind: str, pair_name: str) -> str:
        """Getypter Fallback für alte Call-Sites ohne stabile Config-ID."""
        name = str(pair_name or "").strip()
        if str(job_kind or "").strip().lower() == "pbs" or name.startswith("pbs:"):
            return f"pbs:name:{name.removeprefix('pbs:').casefold()}"
        return f"rclone:name:{name.casefold()}"

    @classmethod
    def _migrate_schema(cls, connection: sqlite3.Connection) -> None:
        """Führt idempotente Schema-Upgrades in fester Reihenfolge aus."""
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version > _SCHEMA_VERSION:
            raise RuntimeError(
                f"DB-Schema {version} ist neuer als unterstützt ({_SCHEMA_VERSION})"
            )

        if version < 1:
            cls._migrate_pair_history_columns(connection)
            connection.execute("PRAGMA user_version=1")
            version = 1
        if version < 2:
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_started ON jobs(started_at DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_pair_runs_history_ended "
                "ON pair_runs(history_key, ended_at DESC, id DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_pair_runs_history_success_ended "
                "ON pair_runs(history_key, dry_run, ok, ended_at DESC, id DESC)"
            )
            connection.execute("PRAGMA user_version=2")
            version = 2
        if version < 3:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS pair_history_state ("
                "history_key TEXT PRIMARY KEY, pair_name TEXT NOT NULL, "
                "ever_succeeded INTEGER NOT NULL DEFAULT 0, "
                "terminal_seen INTEGER NOT NULL DEFAULT 0, "
                "first_seen_at REAL NOT NULL, last_seen_at REAL NOT NULL, "
                "last_success_at REAL)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_pair_history_state_name "
                "ON pair_history_state(pair_name)"
            )
            connection.execute("PRAGMA user_version=3")
            version = 3
        if version < 4:
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
            }
            for name, sql_type in (
                ("definition_id", "TEXT"),
                ("definition_name", "TEXT"),
                ("config_revision", "TEXT"),
                ("scheduled_slot", "TEXT"),
            ):
                if name not in columns:
                    connection.execute(f"ALTER TABLE jobs ADD COLUMN {name} {sql_type}")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_definition_started "
                "ON jobs(definition_id, started_at DESC, id DESC)"
            )
            connection.execute("PRAGMA user_version=4")
            version = 4
        if version < 5:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS push_devices ("
                "token TEXT PRIMARY KEY, environment TEXT NOT NULL, "
                "app_version TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, "
                "updated_at REAL NOT NULL)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_push_devices_updated "
                "ON push_devices(updated_at DESC)"
            )
            connection.execute("PRAGMA user_version=5")
            version = 5
        if version < 6:
            columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(push_devices)"
                ).fetchall()
            }
            if "expires_at" not in columns:
                connection.execute(
                    "ALTER TABLE push_devices ADD COLUMN expires_at REAL"
                )
            connection.execute(
                "UPDATE push_devices SET expires_at=COALESCE(expires_at, updated_at + ?) ",
                (30 * 86400,),
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS push_outbox ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, dedupe_key TEXT NOT NULL, "
                "token TEXT NOT NULL, environment TEXT NOT NULL, event TEXT NOT NULL, "
                "title TEXT NOT NULL, message TEXT NOT NULL, "
                "payload_json TEXT NOT NULL DEFAULT '{}', "
                "status TEXT NOT NULL DEFAULT 'pending', "
                "attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at REAL NOT NULL, "
                "expires_at REAL NOT NULL, lease_until REAL NOT NULL DEFAULT 0, "
                "last_error TEXT, apns_id TEXT NOT NULL, created_at REAL NOT NULL, "
                "updated_at REAL NOT NULL, sent_at REAL, "
                "UNIQUE(dedupe_key, token))"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_push_outbox_due "
                "ON push_outbox(status, next_attempt_at, lease_until, id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_push_outbox_created "
                "ON push_outbox(created_at DESC)"
            )
            connection.execute("PRAGMA user_version=6")
            version = 6
        if version < 7:
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
            }
            if "dry_run" not in columns:
                connection.execute(
                    "ALTER TABLE jobs ADD COLUMN dry_run INTEGER NOT NULL DEFAULT 0"
                )
            if "trigger" not in columns:
                connection.execute(
                    "ALTER TABLE jobs ADD COLUMN trigger TEXT NOT NULL DEFAULT 'manual'"
                )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS job_definition_schedule_state ("
                "definition_id TEXT PRIMARY KEY, "
                "definition_name TEXT NOT NULL DEFAULT '', "
                "last_attempt_job_id INTEGER, last_attempt_started_at REAL, "
                "last_attempt_at REAL, last_attempt_status TEXT, "
                "last_attempt_trigger TEXT, last_attempt_scheduled_slot TEXT, "
                "last_success_job_id INTEGER, last_success_started_at REAL, "
                "last_success_at REAL, last_success_trigger TEXT, "
                "last_success_scheduled_slot TEXT, updated_at REAL NOT NULL)"
            )
            rows = connection.execute(
                "SELECT id, summary_json, scheduled_slot FROM jobs ORDER BY id"
            ).fetchall()
            for row in rows:
                try:
                    summary = json.loads(row["summary_json"] or "{}")
                except (json.JSONDecodeError, TypeError):
                    summary = {}
                if not isinstance(summary, dict):
                    summary = {}
                pairs = summary.get("pairs") or []
                inferred_dry_run = (
                    summary.get("dry_run") is True
                    or bool(pairs)
                    and all(
                        isinstance(pair, dict) and pair.get("dry_run") is True
                        for pair in pairs
                    )
                )
                inferred_trigger = str(summary.get("trigger") or "").strip()
                if not inferred_trigger:
                    inferred_trigger = (
                        "scheduler"
                        if str(row["scheduled_slot"] or "").strip()
                        else "manual"
                    )
                connection.execute(
                    "UPDATE jobs SET dry_run=?, trigger=? WHERE id=?",
                    (1 if inferred_dry_run else 0, inferred_trigger, int(row["id"])),
                )
            connection.execute("PRAGMA user_version=7")
            version = 7
        if version < 8:
            columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(push_outbox)"
                ).fetchall()
            }
            if "claim_owner" not in columns:
                connection.execute(
                    "ALTER TABLE push_outbox ADD COLUMN claim_owner "
                    "TEXT NOT NULL DEFAULT ''"
                )
            # Schema 7 hatte keine Claim-Identität. Solche übernommenen Leases
            # können keinem Dispatcher sicher zugeordnet werden und werden daher
            # sofort wieder fällig statt von einem beliebigen Prozess bestätigt.
            connection.execute(
                "UPDATE push_outbox SET status='pending', lease_until=0, "
                "claim_owner='', updated_at=? WHERE status='sending'",
                (time.time(),),
            )
            connection.execute("PRAGMA user_version=8")
            version = 8
        if version < 9:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS job_batches ("
                "id TEXT PRIMARY KEY, state TEXT NOT NULL DEFAULT 'running', "
                "dry_run INTEGER NOT NULL DEFAULT 0, "
                "config_revision TEXT NOT NULL DEFAULT '', "
                "snapshot_json TEXT NOT NULL, "
                "cancel_requested INTEGER NOT NULL DEFAULT 0, "
                "created_at REAL NOT NULL, updated_at REAL NOT NULL, "
                "completed_at REAL)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_job_batches_state_created "
                "ON job_batches(state, created_at)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS job_batch_items ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "batch_id TEXT NOT NULL REFERENCES job_batches(id) ON DELETE CASCADE, "
                "position INTEGER NOT NULL, definition_id TEXT, "
                "definition_name TEXT, spec_json TEXT NOT NULL, "
                "state TEXT NOT NULL DEFAULT 'queued', "
                "job_id INTEGER REFERENCES jobs(id) ON DELETE SET NULL, "
                "error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL, "
                "UNIQUE(batch_id, position))"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_job_batch_items_state "
                "ON job_batch_items(batch_id, state, position)"
            )
            connection.execute("PRAGMA user_version=9")
            version = 9
        if version < 10:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS job_terminal_intents ("
                "job_id INTEGER PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE, "
                "status TEXT NOT NULL, summary_json TEXT NOT NULL, "
                "created_at REAL NOT NULL, updated_at REAL NOT NULL, applied_at REAL)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_job_terminal_intents_pending "
                "ON job_terminal_intents(applied_at, updated_at)"
            )
            connection.execute("PRAGMA user_version=10")
            version = 10
        if version < 11:
            # These scans used to run unconditionally from __init__. Keeping
            # them behind the durable marker removes scheduler hot-path scans.
            # All three routines are idempotent, so an interrupted migration can
            # safely restart while user_version is still 10.
            cls._backfill_pair_runs(connection)
            cls._backfill_pair_history_state(connection)
            cls._backfill_job_definition_schedule_state(connection)
            connection.execute("PRAGMA user_version=11")
            version = 11
        if version < 12:
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_pair_runs_history_activity "
                "ON pair_runs(history_key, "
                "COALESCE(ended_at, started_at) DESC, id DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_pair_runs_history_success_activity "
                "ON pair_runs(history_key, ok, dry_run, "
                "COALESCE(ended_at, started_at) DESC, id DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_pair_runs_name_activity "
                "ON pair_runs(pair_name, "
                "COALESCE(ended_at, started_at) DESC, id DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_pair_runs_name_success_activity "
                "ON pair_runs(pair_name, ok, dry_run, "
                "COALESCE(ended_at, started_at) DESC, id DESC)"
            )
            connection.execute("PRAGMA user_version=12")
            version = 12
        if version < 13:
            for statement in (
                "CREATE TABLE IF NOT EXISTS webauthn_credentials ("
                "credential_id TEXT PRIMARY KEY, "
                "method TEXT NOT NULL CHECK(method IN ('passkey', 'security_key')), "
                "public_key BLOB NOT NULL, sign_count INTEGER NOT NULL DEFAULT 0, "
                "transports_json TEXT NOT NULL DEFAULT '[]', "
                "device_type TEXT NOT NULL DEFAULT '', backed_up INTEGER NOT NULL DEFAULT 0, "
                "label TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, last_used_at REAL)",
                "CREATE INDEX IF NOT EXISTS idx_webauthn_credentials_method "
                "ON webauthn_credentials(method, created_at)",
                "CREATE TABLE IF NOT EXISTS webauthn_challenges ("
                "id TEXT PRIMARY KEY, challenge BLOB NOT NULL, "
                "purpose TEXT NOT NULL CHECK(purpose IN ('register', 'authenticate')), "
                "method TEXT NOT NULL CHECK(method IN ('passkey', 'security_key')), "
                "label TEXT NOT NULL DEFAULT '', native INTEGER NOT NULL DEFAULT 0, "
                "app_binding TEXT NOT NULL DEFAULT '', "
                "expires_at REAL NOT NULL, created_at REAL NOT NULL)",
                "CREATE INDEX IF NOT EXISTS idx_webauthn_challenges_expiry "
                "ON webauthn_challenges(expires_at)",
                "CREATE TABLE IF NOT EXISTS native_auth_exchanges ("
                "token_hash TEXT PRIMARY KEY, verifier_hash TEXT NOT NULL, "
                "username TEXT NOT NULL, "
                "expires_at REAL NOT NULL, created_at REAL NOT NULL)",
                "CREATE INDEX IF NOT EXISTS idx_native_auth_exchanges_expiry "
                "ON native_auth_exchanges(expires_at)",
            ):
                connection.execute(statement)
            connection.execute("PRAGMA user_version=13")
            version = 13

    @staticmethod
    def _record_job_definition_schedule_state(
        connection: sqlite3.Connection,
        *,
        definition_id: str,
        definition_name: str,
        job_id: int,
        started_at: float,
        ended_at: Optional[float],
        status: str,
        trigger: str,
        scheduled_slot: Optional[str],
        dry_run: bool,
    ) -> None:
        """Persistiert Scheduler-Fakten unabhängig von löschbarer Anzeigehistorie."""

        stable_id = str(definition_id or "").strip()
        if not stable_id or dry_run:
            return
        name = str(definition_name or "").strip()
        started = float(started_at)
        effective_at = float(ended_at if ended_at is not None else started_at)
        normalized_trigger = str(trigger or "manual").strip() or "manual"
        slot = str(scheduled_slot or "").strip() or None
        scheduler_attempt = normalized_trigger == "scheduler"
        succeeded = status == "ok"
        if not scheduler_attempt and not succeeded:
            return
        existing = connection.execute(
            "SELECT * FROM job_definition_schedule_state WHERE definition_id=?",
            (stable_id,),
        ).fetchone()
        if existing is None:
            connection.execute(
                "INSERT INTO job_definition_schedule_state ("
                "definition_id, definition_name, last_attempt_job_id, "
                "last_attempt_started_at, last_attempt_at, last_attempt_status, "
                "last_attempt_trigger, last_attempt_scheduled_slot, "
                "last_success_job_id, last_success_started_at, last_success_at, "
                "last_success_trigger, last_success_scheduled_slot, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    stable_id,
                    name,
                    int(job_id) if scheduler_attempt else None,
                    started if scheduler_attempt else None,
                    effective_at if scheduler_attempt else None,
                    status if scheduler_attempt else None,
                    normalized_trigger if scheduler_attempt else None,
                    slot if scheduler_attempt else None,
                    int(job_id) if succeeded else None,
                    started if succeeded else None,
                    effective_at if succeeded else None,
                    normalized_trigger if succeeded else None,
                    slot if succeeded else None,
                    effective_at,
                ),
            )
            return

        current_attempt = (
            float(existing["last_attempt_started_at"] or 0),
            int(existing["last_attempt_job_id"] or 0),
        )
        candidate = (started, int(job_id))
        assignments = ["definition_name=?", "updated_at=MAX(updated_at, ?)"]
        values: list[Any] = [
            name or str(existing["definition_name"] or ""),
            effective_at,
        ]
        if scheduler_attempt and candidate >= current_attempt:
            assignments.extend(
                [
                    "last_attempt_job_id=?",
                    "last_attempt_started_at=?",
                    "last_attempt_at=?",
                    "last_attempt_status=?",
                    "last_attempt_trigger=?",
                    "last_attempt_scheduled_slot=?",
                ]
            )
            values.extend(
                [int(job_id), started, effective_at, status, normalized_trigger, slot]
            )
        if status == "ok":
            current_success = (
                float(existing["last_success_started_at"] or 0),
                int(existing["last_success_job_id"] or 0),
            )
            if candidate >= current_success:
                assignments.extend(
                    [
                        "last_success_job_id=?",
                        "last_success_started_at=?",
                        "last_success_at=?",
                        "last_success_trigger=?",
                        "last_success_scheduled_slot=?",
                    ]
                )
                values.extend(
                    [int(job_id), started, effective_at, normalized_trigger, slot]
                )
        values.append(stable_id)
        connection.execute(
            f"UPDATE job_definition_schedule_state SET {', '.join(assignments)} "
            "WHERE definition_id=?",
            tuple(values),
        )

    @classmethod
    def _backfill_job_definition_schedule_state(
        cls, connection: sqlite3.Connection
    ) -> None:
        """Backfill ist idempotent und überschreibt nie neueren, erhaltenen Zustand."""

        rows = connection.execute(
            "SELECT id, definition_id, definition_name, started_at, ended_at, status, "
            "trigger, scheduled_slot, dry_run FROM jobs "
            "WHERE definition_id IS NOT NULL AND definition_id<>'' "
            "ORDER BY started_at, id"
        ).fetchall()
        for row in rows:
            cls._record_job_definition_schedule_state(
                connection,
                definition_id=str(row["definition_id"] or ""),
                definition_name=str(row["definition_name"] or ""),
                job_id=int(row["id"]),
                started_at=float(row["started_at"]),
                ended_at=(
                    float(row["ended_at"]) if row["ended_at"] is not None else None
                ),
                status=str(row["status"]),
                trigger=str(row["trigger"] or "manual"),
                scheduled_slot=str(row["scheduled_slot"] or "") or None,
                dry_run=bool(row["dry_run"]),
            )

    @classmethod
    def _migrate_pair_history_columns(cls, connection: sqlite3.Connection) -> None:
        """Migration 1: getypte Historie, Dry-Run- und Cron-Slot-Metadaten."""
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(pair_runs)").fetchall()
        }
        if "history_key" not in columns:
            connection.execute(
                "ALTER TABLE pair_runs ADD COLUMN history_key TEXT NOT NULL DEFAULT ''"
            )
        if "dry_run" not in columns:
            connection.execute(
                "ALTER TABLE pair_runs ADD COLUMN dry_run INTEGER NOT NULL DEFAULT 0"
            )
        if "scheduled_slot" not in columns:
            connection.execute("ALTER TABLE pair_runs ADD COLUMN scheduled_slot TEXT")

        rows = connection.execute(
            "SELECT pr.id, pr.pair_name, pr.history_key, pr.result_json, j.kind "
            "FROM pair_runs pr JOIN jobs j ON j.id=pr.job_id"
        ).fetchall()
        for row in rows:
            try:
                result = json.loads(row["result_json"] or "{}")
            except (json.JSONDecodeError, TypeError):
                result = {}
            if not isinstance(result, dict):
                result = {}
            history_key = str(
                row["history_key"]
                or result.get("history_key")
                or cls._default_history_key(str(row["kind"]), str(row["pair_name"]))
            )
            dry_run = 1 if result.get("dry_run") is True else 0
            scheduled_slot = (
                str(
                    result.get("scheduled_slot") or result.get("slot_key") or ""
                ).strip()
                or None
            )
            connection.execute(
                "UPDATE pair_runs SET history_key=?, dry_run=?, "
                "scheduled_slot=COALESCE(scheduled_slot, ?) WHERE id=?",
                (history_key, dry_run, scheduled_slot, int(row["id"])),
            )

    @classmethod
    def _backfill_pair_runs(cls, connection: sqlite3.Connection) -> None:
        """Ergänzt fehlende Pair-Versuche aus der historischen JSON-Historie.

        Eine Datenbank kann nach einem abgebrochenen Upgrade bereits einzelne
        ``pair_runs`` enthalten. Ein globaler Tabellen-Count ist deshalb kein
        belastbarer Migrationsmarker: Wir vergleichen stattdessen jeden Job mit
        den in seiner Zusammenfassung enthaltenen Versuchen. Bestehende Zeilen
        bleiben durch den Unique-Key ``(job_id, pair_name)`` idempotent.
        """
        # Cursor-Iteration statt fetchall(): große historische Datenbanken werden
        # beim Upgrade nicht vollständig in den Arbeitsspeicher geladen. SQLite
        # liefert die Zeilen intern schrittweise; regelmäßige Savepoints begrenzen
        # zusätzlich den Umfang einer fehlgeschlagenen Teilmigration.
        cursor = connection.execute(
            "SELECT id, kind, status, started_at, ended_at, summary_json FROM jobs "
            "WHERE summary_json IS NOT NULL ORDER BY id"
        )
        batch_size = 500
        processed = 0
        connection.execute("SAVEPOINT pair_runs_backfill")
        try:
            for row in cursor:
                try:
                    summary = json.loads(row["summary_json"] or "{}")
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(summary, dict):
                    continue
                expected_names = {
                    str(pair.get("name") or "").strip()
                    for pair in (summary.get("pairs") or [])
                    if isinstance(pair, dict) and str(pair.get("name") or "").strip()
                }
                if str(row["status"]) in {"error", "stale", "cancelled"}:
                    due = summary.get("due") or []
                    if isinstance(due, list):
                        expected_names.update(
                            str(name or "").strip()
                            for name in due
                            if str(name or "").strip()
                        )
                if not expected_names:
                    continue
                existing_names = {
                    str(existing["pair_name"])
                    for existing in connection.execute(
                        "SELECT pair_name FROM pair_runs WHERE job_id=?",
                        (int(row["id"]),),
                    ).fetchall()
                }
                if expected_names <= existing_names:
                    continue
                cls._store_pair_results(
                    connection,
                    int(row["id"]),
                    str(row["status"]),
                    float(row["started_at"]),
                    float(row["ended_at"] or row["started_at"]),
                    summary,
                    job_kind=str(row["kind"]),
                )
                processed += 1
                if processed % batch_size == 0:
                    connection.execute("RELEASE SAVEPOINT pair_runs_backfill")
                    connection.execute("SAVEPOINT pair_runs_backfill")
            connection.execute("RELEASE SAVEPOINT pair_runs_backfill")
        except Exception:
            connection.execute("ROLLBACK TO SAVEPOINT pair_runs_backfill")
            connection.execute("RELEASE SAVEPOINT pair_runs_backfill")
            raise

    @staticmethod
    def _record_pair_history_state(
        connection: sqlite3.Connection,
        *,
        history_key: str,
        pair_name: str,
        seen_at: float,
        succeeded: bool = False,
        terminal: bool = False,
        success_at: float | None = None,
    ) -> None:
        """Speichert Baseline-Evidenz unabhängig von der löschbaren Job-Historie."""

        key = str(history_key or "").strip()
        name = str(pair_name or "").strip()
        if not key or not name:
            return
        timestamp = float(seen_at)
        succeeded_value = 1 if succeeded else 0
        terminal_value = 1 if terminal else 0
        succeeded_at = float(success_at or timestamp) if succeeded else None
        connection.execute(
            "INSERT INTO pair_history_state "
            "(history_key, pair_name, ever_succeeded, terminal_seen, "
            "first_seen_at, last_seen_at, last_success_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(history_key) DO UPDATE SET "
            "pair_name=CASE WHEN excluded.last_seen_at >= pair_history_state.last_seen_at "
            "THEN excluded.pair_name ELSE pair_history_state.pair_name END, "
            "ever_succeeded=MAX(pair_history_state.ever_succeeded, excluded.ever_succeeded), "
            "terminal_seen=MAX(pair_history_state.terminal_seen, excluded.terminal_seen), "
            "first_seen_at=MIN(pair_history_state.first_seen_at, excluded.first_seen_at), "
            "last_seen_at=MAX(pair_history_state.last_seen_at, excluded.last_seen_at), "
            "last_success_at=CASE "
            "WHEN excluded.last_success_at IS NULL THEN pair_history_state.last_success_at "
            "WHEN pair_history_state.last_success_at IS NULL THEN excluded.last_success_at "
            "ELSE MAX(pair_history_state.last_success_at, excluded.last_success_at) END",
            (
                key,
                name,
                succeeded_value,
                terminal_value,
                timestamp,
                timestamp,
                succeeded_at,
            ),
        )

    @classmethod
    def _backfill_pair_history_state(cls, connection: sqlite3.Connection) -> None:
        """Backfillt den dauerhaften Marker idempotent aus allen Pair-Versuchen."""

        rows = connection.execute(
            "SELECT history_key, pair_name, MIN(started_at) AS first_seen, "
            "MAX(COALESCE(ended_at, started_at)) AS last_seen, "
            "MAX(CASE WHEN ok=1 AND dry_run=0 THEN 1 ELSE 0 END) AS succeeded, "
            "MAX(CASE WHEN status<>'running' OR ended_at IS NOT NULL THEN 1 ELSE 0 END) "
            "AS terminal_seen, "
            "MAX(CASE WHEN ok=1 AND dry_run=0 "
            "THEN COALESCE(ended_at, started_at) ELSE NULL END) AS success_at "
            "FROM pair_runs WHERE history_key<>'' GROUP BY history_key, pair_name"
        ).fetchall()
        for row in rows:
            cls._record_pair_history_state(
                connection,
                history_key=str(row["history_key"]),
                pair_name=str(row["pair_name"]),
                seen_at=float(row["last_seen"] or row["first_seen"] or time.time()),
                succeeded=bool(row["succeeded"]),
                terminal=bool(row["terminal_seen"]),
                success_at=(
                    float(row["success_at"]) if row["success_at"] is not None else None
                ),
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
            # synchronous ist verbindungsgebunden (im Gegensatz zum persistenten
            # journal_mode) und muss daher für jede Verbindung gesetzt werden.
            connection.execute("PRAGMA synchronous=NORMAL")
            if initialize:
                connection.execute("PRAGMA journal_mode=WAL")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def job_start(
        self,
        kind: str,
        log_file: Optional[str] = None,
        *,
        attempts: Optional[Iterable[Mapping[str, Any]]] = None,
        exclusive_scope: bool = False,
        definition_id: Optional[str] = None,
        definition_name: Optional[str] = None,
        config_revision: Optional[str] = None,
        scheduled_slot: Optional[str] = None,
        dry_run: Optional[bool] = None,
        trigger: Optional[str] = None,
    ) -> int:
        if not kind or len(kind) > 64:
            raise ValueError("Ungültiger Job-Typ")
        started_at = time.time()
        prepared_attempts = [
            attempt for attempt in (attempts or ()) if isinstance(attempt, Mapping)
        ]
        inferred_dry_run = (
            bool(dry_run)
            if dry_run is not None
            else bool(prepared_attempts)
            and all(attempt.get("dry_run") is True for attempt in prepared_attempts)
        )
        attempt_triggers = {
            str(attempt.get("trigger") or "").strip()
            for attempt in prepared_attempts
            if str(attempt.get("trigger") or "").strip()
        }
        inferred_trigger = str(trigger or "").strip()
        if not inferred_trigger and len(attempt_triggers) == 1:
            inferred_trigger = next(iter(attempt_triggers))
        if not inferred_trigger:
            inferred_trigger = "scheduler" if scheduled_slot else "manual"
        definition_slot = str(scheduled_slot or "").strip() or None
        with self.conn() as connection:
            if exclusive_scope:
                connection.execute("BEGIN IMMEDIATE")
                scope_kinds = _JOB_SCOPE_KINDS.get(kind, (kind,))
                marks = ",".join("?" for _ in scope_kinds)
                running = connection.execute(
                    f"SELECT id, kind FROM jobs WHERE status='running' "
                    f"AND kind IN ({marks}) ORDER BY started_at DESC LIMIT 1",
                    scope_kinds,
                ).fetchone()
                if running:
                    raise JobAlreadyRunningError(
                        f"Job-Scope bereits belegt durch "
                        f"{running['kind']} #{running['id']}"
                    )
            cursor = connection.execute(
                "INSERT INTO jobs (kind, status, started_at, log_file, "
                "definition_id, definition_name, config_revision, scheduled_slot, "
                "dry_run, trigger) VALUES (?, 'running', ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    kind,
                    started_at,
                    log_file,
                    str(definition_id or "").strip() or None,
                    str(definition_name or "").strip() or None,
                    str(config_revision or "").strip() or None,
                    definition_slot,
                    1 if inferred_dry_run else 0,
                    inferred_trigger,
                ),
            )
            job_id = int(cursor.lastrowid)
            for attempt in prepared_attempts:
                pair_name = str(attempt.get("name") or "").strip()
                if not pair_name:
                    continue
                history_key = str(
                    attempt.get("history_key")
                    or self._default_history_key(kind, pair_name)
                ).strip()
                scheduled_slot = (
                    str(attempt.get("scheduled_slot") or "").strip() or None
                )
                result = {
                    "name": pair_name,
                    "ok": False,
                    "pending": True,
                    "trigger": str(attempt.get("trigger") or "manual"),
                    "history_key": history_key,
                }
                if scheduled_slot:
                    result["scheduled_slot"] = scheduled_slot
                if attempt.get("dry_run") is True:
                    result["dry_run"] = True
                connection.execute(
                    "INSERT OR IGNORE INTO pair_runs "
                    "(job_id, pair_name, history_key, ok, dry_run, status, "
                    "started_at, ended_at, scheduled_slot, result_json) "
                    "VALUES (?, ?, ?, 0, ?, 'running', ?, NULL, ?, ?)",
                    (
                        job_id,
                        pair_name,
                        history_key,
                        1 if attempt.get("dry_run") is True else 0,
                        started_at,
                        scheduled_slot,
                        _json_dumps_bounded(result, _MAX_PAIR_RESULT_BYTES),
                    ),
                )
                self._record_pair_history_state(
                    connection,
                    history_key=history_key,
                    pair_name=pair_name,
                    seen_at=started_at,
                )
            if inferred_trigger == "scheduler":
                self._record_job_definition_schedule_state(
                    connection,
                    definition_id=str(definition_id or ""),
                    definition_name=str(definition_name or ""),
                    job_id=job_id,
                    started_at=started_at,
                    ended_at=None,
                    status="running",
                    trigger=inferred_trigger,
                    scheduled_slot=definition_slot,
                    dry_run=inferred_dry_run,
                )
            return job_id

    def job_finish(
        self, job_id: int, status: str, summary: Optional[Dict[str, Any]] = None
    ) -> bool:
        if status not in {"running", "ok", "error", "skipped", "cancelled", "stale"}:
            raise ValueError(f"Ungültiger Job-Status: {status}")
        ended_at = time.time()
        payload = (
            _json_dumps_bounded(summary, _MAX_JOB_SUMMARY_BYTES)
            if summary is not None
            else None
        )
        with self.conn() as connection:
            row = connection.execute(
                "SELECT kind, status, started_at, definition_id, definition_name, "
                "scheduled_slot, dry_run, trigger FROM jobs WHERE id=?",
                (job_id,),
            ).fetchone()
            if not row:
                raise ValueError(f"Job nicht gefunden: {job_id}")
            if str(row["status"]) != "running":
                return False
            started_at = float(row["started_at"])
            summary_data = summary if isinstance(summary, dict) else {}
            effective_dry_run = (
                bool(row["dry_run"]) or summary_data.get("dry_run") is True
            )
            effective_trigger = (
                str(summary_data.get("trigger") or row["trigger"] or "manual").strip()
                or "manual"
            )
            effective_slot = (
                str(
                    summary_data.get("scheduled_slot") or row["scheduled_slot"] or ""
                ).strip()
                or None
            )
            cursor = connection.execute(
                "UPDATE jobs SET status=?, ended_at=?, summary_json=?, dry_run=?, "
                "trigger=?, scheduled_slot=COALESCE(scheduled_slot, ?) "
                "WHERE id=? AND status='running'",
                (
                    status,
                    ended_at,
                    payload,
                    1 if effective_dry_run else 0,
                    effective_trigger,
                    effective_slot,
                    job_id,
                ),
            )
            if int(cursor.rowcount or 0) != 1:
                return False
            self._store_pair_results(
                connection,
                job_id,
                status,
                started_at,
                ended_at,
                summary or {},
                job_kind=str(row["kind"]),
            )
            self._record_job_definition_schedule_state(
                connection,
                definition_id=str(row["definition_id"] or ""),
                definition_name=str(row["definition_name"] or ""),
                job_id=job_id,
                started_at=started_at,
                ended_at=ended_at,
                status=status,
                trigger=effective_trigger,
                scheduled_slot=effective_slot,
                dry_run=effective_dry_run,
            )
            return True

    def job_terminal_stage(
        self, job_id: int, status: str, summary: Optional[Dict[str, Any]] = None
    ) -> None:
        """Schreibt das externe Ergebnis idempotent vor den terminalen CAS."""
        if status not in {"ok", "error", "skipped", "cancelled", "stale"}:
            raise ValueError(f"Ungueltiger terminaler Job-Status: {status}")
        payload = _json_dumps_bounded(summary or {}, _MAX_JOB_SUMMARY_BYTES)
        now = time.time()
        with self.conn() as connection:
            connection.execute("BEGIN IMMEDIATE")
            job = connection.execute(
                "SELECT id FROM jobs WHERE id=?", (int(job_id),)
            ).fetchone()
            if not job:
                raise ValueError(f"Job nicht gefunden: {job_id}")
            connection.execute(
                "INSERT OR IGNORE INTO job_terminal_intents "
                "(job_id, status, summary_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (int(job_id), status, payload, now, now),
            )
            existing = connection.execute(
                "SELECT status, summary_json FROM job_terminal_intents WHERE job_id=?",
                (int(job_id),),
            ).fetchone()
            if not existing or (
                str(existing["status"]) != status
                or str(existing["summary_json"]) != payload
            ):
                raise RuntimeError(
                    f"Abweichender terminaler Abschluss fuer Job #{job_id}"
                )
            connection.execute(
                "UPDATE job_terminal_intents SET updated_at=? WHERE job_id=?",
                (now, int(job_id)),
            )

    def job_terminal_apply(self, job_id: int) -> bool:
        with self.conn() as connection:
            row = connection.execute(
                "SELECT status, summary_json, applied_at FROM job_terminal_intents "
                "WHERE job_id=?",
                (int(job_id),),
            ).fetchone()
        if not row:
            raise ValueError(f"Kein terminaler Abschluss fuer Job #{job_id}")
        status = str(row["status"])
        try:
            summary = json.loads(row["summary_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            summary = {}
        transitioned = self.job_finish(int(job_id), status, summary)
        actual = self.job_get(int(job_id)) or {}
        actual_status = str(actual.get("status") or "")
        if not transitioned and actual_status != status:
            raise RuntimeError(
                f"Terminaler CAS-Konflikt fuer Job #{job_id}: {actual_status} != {status}"
            )
        now = time.time()
        with self.conn() as connection:
            connection.execute(
                "UPDATE job_terminal_intents SET applied_at=COALESCE(applied_at, ?), "
                "updated_at=? WHERE job_id=?",
                (now, now, int(job_id)),
            )
        return transitioned or actual_status == status

    def job_finish_external(
        self,
        job_id: int,
        status: str,
        summary: Optional[Dict[str, Any]] = None,
        *,
        attempts: int = 3,
    ) -> bool:
        """Retryt nur Persistenz/CAS, niemals die bereits beendete externe Aktion."""
        last_error: Exception | None = None
        for retry in range(max(1, int(attempts))):
            try:
                self.job_terminal_stage(job_id, status, summary)
                return self.job_terminal_apply(job_id)
            except sqlite3.OperationalError as exc:
                last_error = exc
                if retry + 1 < max(1, int(attempts)):
                    time.sleep(0.05 * (retry + 1))
        assert last_error is not None
        raise last_error

    def job_terminal_recover_pending(self) -> dict[str, Any]:
        """Vollendet vor Lifecycle-Recovery bereits extern beendete Jobs."""
        with self.conn() as connection:
            rows = connection.execute(
                "SELECT job_id FROM job_terminal_intents WHERE applied_at IS NULL "
                "ORDER BY created_at"
            ).fetchall()
        recovered = 0
        recovered_job_ids: list[int] = []
        failed = 0
        for row in rows:
            try:
                if self.job_terminal_apply(int(row["job_id"])):
                    recovered += 1
                    recovered_job_ids.append(int(row["job_id"]))
            except Exception:
                failed += 1
        return {
            "recovered": recovered,
            "failed": failed,
            "recovered_job_ids": recovered_job_ids,
        }

    def job_terminal_pending(self, job_id: int) -> bool:
        with self.conn() as connection:
            row = connection.execute(
                "SELECT 1 FROM job_terminal_intents "
                "WHERE job_id=? AND applied_at IS NULL",
                (int(job_id),),
            ).fetchone()
        return row is not None

    def job_batch_create(
        self,
        *,
        specs: list[dict[str, Any]],
        snapshot: dict[str, Any],
        config_revision: str,
        dry_run: bool,
        first_job_id: int,
    ) -> str:
        """Persistiert einen akzeptierten Definitions-Batch vor der HTTP-Antwort."""
        if not specs:
            raise ValueError("Ein Job-Batch benoetigt mindestens ein Element")
        batch_id = uuid.uuid4().hex
        now = time.time()
        snapshot_json = _json_dumps_bounded(snapshot, 2 * 1024 * 1024)
        with self.conn() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO job_batches "
                "(id, state, dry_run, config_revision, snapshot_json, "
                "cancel_requested, created_at, updated_at) "
                "VALUES (?, 'running', ?, ?, ?, 0, ?, ?)",
                (
                    batch_id,
                    1 if dry_run else 0,
                    str(config_revision or ""),
                    snapshot_json,
                    now,
                    now,
                ),
            )
            for position, spec in enumerate(specs):
                definition = spec.get("definition") or {}
                connection.execute(
                    "INSERT INTO job_batch_items "
                    "(batch_id, position, definition_id, definition_name, "
                    "spec_json, state, job_id, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        batch_id,
                        position,
                        str(definition.get("id") or "") or None,
                        str(definition.get("name") or "") or None,
                        _json_dumps_bounded(spec, _MAX_JOB_SUMMARY_BYTES),
                        "running" if position == 0 else "queued",
                        int(first_job_id) if position == 0 else None,
                        now,
                        now,
                    ),
                )
        return batch_id

    def job_batch_get(self, batch_id: str) -> Optional[Dict[str, Any]]:
        with self.conn() as connection:
            batch = connection.execute(
                "SELECT * FROM job_batches WHERE id=?", (batch_id,)
            ).fetchone()
            if not batch:
                return None
            items = connection.execute(
                "SELECT * FROM job_batch_items WHERE batch_id=? ORDER BY position",
                (batch_id,),
            ).fetchall()
        data = dict(batch)
        try:
            data["snapshot"] = json.loads(data.pop("snapshot_json"))
        except (json.JSONDecodeError, TypeError):
            data["snapshot"] = {}
        data["dry_run"] = bool(data.get("dry_run"))
        data["cancel_requested"] = bool(data.get("cancel_requested"))
        decoded_items = []
        for row in items:
            item = dict(row)
            try:
                item["spec"] = json.loads(item.pop("spec_json"))
            except (json.JSONDecodeError, TypeError):
                item["spec"] = {}
            decoded_items.append(item)
        data["items"] = decoded_items
        return data

    def job_batches_pending(self) -> List[Dict[str, Any]]:
        with self.conn() as connection:
            rows = connection.execute(
                "SELECT id FROM job_batches WHERE state='running' ORDER BY created_at"
            ).fetchall()
        return [batch for row in rows if (batch := self.job_batch_get(str(row["id"])))]

    def job_batch_item_start(self, batch_id: str, position: int, job_id: int) -> bool:
        now = time.time()
        with self.conn() as connection:
            cursor = connection.execute(
                "UPDATE job_batch_items SET state='running', job_id=?, updated_at=? "
                "WHERE batch_id=? AND position=? AND state='queued'",
                (int(job_id), now, batch_id, int(position)),
            )
            return int(cursor.rowcount or 0) == 1

    def job_batch_item_finish(
        self, batch_id: str, position: int, state: str, *, error: str | None = None
    ) -> bool:
        if state not in {"done", "failed", "cancelled"}:
            raise ValueError("Ungueltiger Batch-Item-Status")
        now = time.time()
        with self.conn() as connection:
            cursor = connection.execute(
                "UPDATE job_batch_items SET state=?, error=?, updated_at=? "
                "WHERE batch_id=? AND position=? AND state IN ('queued', 'running')",
                (state, error, now, batch_id, int(position)),
            )
            remaining = connection.execute(
                "SELECT COUNT(*) FROM job_batch_items WHERE batch_id=? "
                "AND state IN ('queued', 'running')",
                (batch_id,),
            ).fetchone()[0]
            if int(remaining or 0) == 0:
                cancelled = connection.execute(
                    "SELECT cancel_requested FROM job_batches WHERE id=?", (batch_id,)
                ).fetchone()
                final_state = "cancelled" if cancelled and cancelled[0] else "done"
                connection.execute(
                    "UPDATE job_batches SET state=?, updated_at=?, completed_at=? "
                    "WHERE id=? AND state='running'",
                    (final_state, now, now, batch_id),
                )
            return int(cursor.rowcount or 0) == 1

    def job_batch_request_cancel(self) -> int:
        now = time.time()
        with self.conn() as connection:
            cursor = connection.execute(
                "UPDATE job_batches SET cancel_requested=1, updated_at=? "
                "WHERE state='running' AND cancel_requested=0",
                (now,),
            )
            return int(cursor.rowcount or 0)

    def job_batch_recover(self, batch_id: str) -> Optional[Dict[str, Any]]:
        """Ordnet beim Neustart verwaiste aktive Items ein, ohne sie neu zu starten."""
        now = time.time()
        with self.conn() as connection:
            rows = connection.execute(
                "SELECT position, job_id FROM job_batch_items "
                "WHERE batch_id=? AND state='running'",
                (batch_id,),
            ).fetchall()
            for row in rows:
                job = connection.execute(
                    "SELECT status FROM jobs WHERE id=?", (row["job_id"],)
                ).fetchone()
                if job and str(job["status"]) == "running":
                    continue
                status = str(job["status"]) if job else "stale"
                item_state = "done" if status == "ok" else "failed"
                connection.execute(
                    "UPDATE job_batch_items SET state=?, error=?, updated_at=? "
                    "WHERE batch_id=? AND position=? AND state='running'",
                    (
                        item_state,
                        None
                        if item_state == "done"
                        else f"Unterbrochener Lauf: {status}",
                        now,
                        batch_id,
                        int(row["position"]),
                    ),
                )
            remaining = connection.execute(
                "SELECT COUNT(*) FROM job_batch_items WHERE batch_id=? "
                "AND state IN ('queued', 'running')",
                (batch_id,),
            ).fetchone()[0]
            if int(remaining or 0) == 0:
                connection.execute(
                    "UPDATE job_batches SET state='done', updated_at=?, completed_at=? "
                    "WHERE id=? AND state='running'",
                    (now, now, batch_id),
                )
        return self.job_batch_get(batch_id)

    @staticmethod
    def _store_pair_results(
        connection: sqlite3.Connection,
        job_id: int,
        job_status: str,
        started_at: float,
        ended_at: float,
        summary: Dict[str, Any],
        *,
        job_kind: str = "backup",
    ) -> None:
        history_keys = summary.get("history_keys") or {}
        if not isinstance(history_keys, dict):
            history_keys = {}
        scheduled_slots = summary.get("scheduler_slots") or {}
        if not isinstance(scheduled_slots, dict):
            scheduled_slots = {}
        summary_dry_run = summary.get("dry_run") is True
        summary_trigger = str(summary.get("trigger") or "").strip()

        pairs = summary.get("pairs") or []
        if not isinstance(pairs, list):
            pairs = []
        for raw_pair in pairs:
            if not isinstance(raw_pair, dict):
                continue
            pair = dict(raw_pair)
            name = str(pair.get("name") or "").strip()
            if not name:
                continue
            history_key = str(
                pair.get("history_key")
                or history_keys.get(name)
                or Database._default_history_key(job_kind, name)
            ).strip()
            scheduled_slot = (
                str(
                    pair.get("scheduled_slot") or scheduled_slots.get(name) or ""
                ).strip()
                or None
            )
            dry_run = pair.get("dry_run") is True or summary_dry_run
            if summary_trigger and not pair.get("trigger"):
                pair["trigger"] = summary_trigger
            pair["history_key"] = history_key
            if scheduled_slot:
                pair["scheduled_slot"] = scheduled_slot
            if dry_run:
                pair["dry_run"] = True
            pair_status = (
                "ok"
                if pair.get("ok") is True
                else (
                    "cancelled"
                    if pair.get("cancelled") or job_status == "cancelled"
                    else ("skipped" if pair.get("skipped") else "error")
                )
            )
            connection.execute(
                "INSERT INTO pair_runs "
                "(job_id, pair_name, history_key, ok, dry_run, status, started_at, "
                "ended_at, scheduled_slot, result_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(job_id, pair_name) DO UPDATE SET "
                "history_key=excluded.history_key, ok=excluded.ok, dry_run=excluded.dry_run, "
                "status=excluded.status, ended_at=excluded.ended_at, "
                "scheduled_slot=excluded.scheduled_slot, result_json=excluded.result_json",
                (
                    job_id,
                    name,
                    history_key,
                    1 if pair.get("ok") is True else 0,
                    1 if dry_run else 0,
                    pair_status,
                    started_at,
                    ended_at,
                    scheduled_slot,
                    _json_dumps_bounded(pair, _MAX_PAIR_RESULT_BYTES),
                ),
            )
            Database._record_pair_history_state(
                connection,
                history_key=history_key,
                pair_name=name,
                seen_at=ended_at,
                succeeded=pair.get("ok") is True and not dry_run,
                terminal=job_status != "running",
                success_at=ended_at,
            )

        due = summary.get("due") or []
        if job_status in {"error", "stale", "cancelled"} and isinstance(due, list):
            for name_value in due:
                name = str(name_value or "").strip()
                if not name:
                    continue
                history_key = str(
                    history_keys.get(name)
                    or Database._default_history_key(job_kind, name)
                ).strip()
                scheduled_slot = str(scheduled_slots.get(name) or "").strip() or None
                result = {
                    "name": name,
                    "ok": False,
                    "error": summary.get("error"),
                    "history_key": history_key,
                }
                if summary_trigger:
                    result["trigger"] = summary_trigger
                if scheduled_slot:
                    result["scheduled_slot"] = scheduled_slot
                if summary_dry_run:
                    result["dry_run"] = True
                connection.execute(
                    "INSERT INTO pair_runs "
                    "(job_id, pair_name, history_key, ok, dry_run, status, started_at, "
                    "ended_at, scheduled_slot, result_json) "
                    "VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(job_id, pair_name) DO UPDATE SET "
                    "history_key=excluded.history_key, ok=0, dry_run=excluded.dry_run, "
                    "status=excluded.status, ended_at=excluded.ended_at, "
                    "scheduled_slot=excluded.scheduled_slot, result_json=excluded.result_json",
                    (
                        job_id,
                        name,
                        history_key,
                        1 if summary_dry_run else 0,
                        job_status,
                        started_at,
                        ended_at,
                        scheduled_slot,
                        _json_dumps_bounded(result, _MAX_PAIR_RESULT_BYTES),
                    ),
                )
                Database._record_pair_history_state(
                    connection,
                    history_key=history_key,
                    pair_name=name,
                    seen_at=ended_at,
                    terminal=True,
                )

        if job_status != "running":
            unfinished_status = (
                job_status
                if job_status in {"cancelled", "stale", "skipped"}
                else "error"
            )
            Database._terminalize_running_attempts(
                connection,
                (job_id,),
                status=unfinished_status,
                ended_at=ended_at,
                error=str(summary.get("error") or "") or None,
            )

    @staticmethod
    def _terminalize_running_attempts(
        connection: sqlite3.Connection,
        job_ids: Iterable[int],
        *,
        status: str,
        ended_at: float,
        error: str | None = None,
    ) -> None:
        """Hält Tabellenstatus und eingebettetes Attempt-JSON konsistent."""

        for raw_job_id in job_ids:
            job_id = int(raw_job_id)
            unfinished_rows = connection.execute(
                "SELECT id, pair_name, history_key, result_json FROM pair_runs "
                "WHERE job_id=? AND status='running'",
                (job_id,),
            ).fetchall()
            for unfinished in unfinished_rows:
                try:
                    result = json.loads(unfinished["result_json"] or "{}")
                except (json.JSONDecodeError, TypeError):
                    result = {}
                if not isinstance(result, dict):
                    result = {}
                result.pop("pending", None)
                result["ok"] = False
                result["status"] = status
                if status == "cancelled":
                    result["cancelled"] = True
                if error and not result.get("error"):
                    result["error"] = str(error)
                connection.execute(
                    "UPDATE pair_runs SET status=?, ok=0, ended_at=?, result_json=? "
                    "WHERE id=? AND status='running'",
                    (
                        status,
                        ended_at,
                        _json_dumps_bounded(result, _MAX_PAIR_RESULT_BYTES),
                        int(unfinished["id"]),
                    ),
                )
                Database._record_pair_history_state(
                    connection,
                    history_key=str(unfinished["history_key"] or ""),
                    pair_name=str(unfinished["pair_name"] or ""),
                    seen_at=ended_at,
                    terminal=True,
                )

    def job_set_log_file(self, job_id: int, path: str) -> None:
        with self.conn() as connection:
            connection.execute("UPDATE jobs SET log_file=? WHERE id=?", (path, job_id))

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
        data = dict(row)
        if "dry_run" in data:
            data["dry_run"] = bool(data["dry_run"])
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
            "dry_run": bool(data.get("dry_run")),
            "status": data.get("status"),
            "started_at": data.get("started_at"),
            "ended_at": data.get("ended_at") or data.get("started_at"),
            "pair": pair,
            "job_id": data.get("job_id"),
            "history_key": data.get("history_key"),
            "scheduled_slot": data.get("scheduled_slot"),
        }

    def job_get(self, job_id: int) -> Optional[Dict[str, Any]]:
        with self.conn() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            return self._row_to_dict(row) if row else None

    @staticmethod
    def _job_filter(
        *,
        kind: Optional[str] = None,
        status: Optional[str] = None,
        query: Optional[str] = None,
    ) -> tuple[str, list[Any]]:
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
                "OR LOWER(COALESCE(log_file, '')) LIKE ? ESCAPE '\\' "
                "OR LOWER(COALESCE(definition_id, '')) LIKE ? ESCAPE '\\' "
                "OR LOWER(COALESCE(definition_name, '')) LIKE ? ESCAPE '\\')"
            )
            params.extend(
                [
                    needle,
                    f"%{escaped}%",
                    f"%{escaped}%",
                    f"%{escaped}%",
                    f"%{escaped}%",
                ]
            )
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        return where, params

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
        where, params = self._job_filter(kind=kind, status=status, query=query)
        params.extend([limit, offset])
        with self.conn() as connection:
            rows = connection.execute(
                f"SELECT * FROM jobs{where} "
                "ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?",
                tuple(params),
            ).fetchall()
            return [self._row_to_dict(row) for row in rows]

    def job_iter(
        self,
        *,
        kind: Optional[str] = None,
        status: Optional[str] = None,
        query: Optional[str] = None,
        limit: int = 5000,
        batch_size: int = 250,
    ) -> Iterator[Dict[str, Any]]:
        """Streamt Jobs aus genau einem SQLite-Statement-Snapshot.

        Der offene Cursor hält die Sicht auf die Ergebnismenge stabil, während
        ``fetchmany`` den Python-Speicher unabhängig von der Exportgröße begrenzt.
        """

        bounded_limit = max(1, min(int(limit or 5000), 10000))
        bounded_batch = max(1, min(int(batch_size or 250), 1000))
        where, params = self._job_filter(kind=kind, status=status, query=query)
        params.append(bounded_limit)
        with self.conn() as connection:
            cursor = connection.execute(
                f"SELECT * FROM jobs{where} ORDER BY started_at DESC, id DESC LIMIT ?",
                tuple(params),
            )
            while rows := cursor.fetchmany(bounded_batch):
                for row in rows:
                    yield self._row_to_dict(row)

    def job_count(
        self,
        *,
        kind: Optional[str] = None,
        status: Optional[str] = None,
        query: Optional[str] = None,
    ) -> int:
        where, params = self._job_filter(kind=kind, status=status, query=query)
        with self.conn() as connection:
            return int(
                connection.execute(
                    f"SELECT COUNT(*) FROM jobs{where}", tuple(params)
                ).fetchone()[0]
            )

    def job_search(
        self,
        *,
        kind: Optional[str] = None,
        status: Optional[str] = None,
        query: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[List[Dict[str, Any]], int]:
        """Liest Treffer und Gesamtzahl aus derselben SQLite-Read-Transaktion."""

        bounded_limit = max(1, min(int(limit or 50), 200))
        bounded_offset = max(0, min(int(offset or 0), 1_000_000))
        where, params = self._job_filter(kind=kind, status=status, query=query)
        with self.conn() as connection:
            connection.execute("BEGIN")
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM jobs{where}", tuple(params)
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"SELECT * FROM jobs{where} "
                "ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?",
                (*params, bounded_limit, bounded_offset),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows], total

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

    def job_definition_history(
        self, definitions: Mapping[str, str]
    ) -> Dict[str, Dict[str, Optional[Dict[str, Any]]]]:
        """Lädt die löschbare Anzeigehistorie pro stabiler Jobdefinitions-ID."""

        ids = [str(value).strip() for value in definitions if str(value).strip()]
        result: Dict[str, Dict[str, Optional[Dict[str, Any]]]] = {
            definition_id: {"last_success": None, "last_result": None}
            for definition_id in ids
        }
        if not ids:
            return result
        marks = ",".join("?" for _ in ids)
        with self.conn() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs WHERE definition_id IN ("
                + marks
                + ") ORDER BY started_at DESC, id DESC",
                tuple(ids),
            ).fetchall()
        for row in rows:
            item = self._row_to_dict(row)
            definition_id = str(item.get("definition_id") or "")
            bucket = result.get(definition_id)
            if bucket is None:
                continue
            summary = item.get("summary")
            if isinstance(summary, dict):
                item = {**item, **summary, "summary": summary}
            if bucket["last_result"] is None:
                bucket["last_result"] = item
            if (
                item.get("status") == "ok"
                and not bool(item.get("dry_run"))
                and bucket["last_success"] is None
            ):
                bucket["last_success"] = item
        return result

    def job_definition_schedule_state(
        self, definitions: Mapping[str, str]
    ) -> Dict[str, Dict[str, Optional[Dict[str, Any]]]]:
        """Lädt den dauerhaften Schedulerzustand, unabhängig von ``jobs``-Retention."""

        ids = [str(value).strip() for value in definitions if str(value).strip()]
        result: Dict[str, Dict[str, Optional[Dict[str, Any]]]] = {
            definition_id: {"last_success": None, "last_result": None}
            for definition_id in ids
        }
        if not ids:
            return result
        marks = ",".join("?" for _ in ids)
        with self.conn() as connection:
            rows = connection.execute(
                "SELECT * FROM job_definition_schedule_state WHERE definition_id IN ("
                + marks
                + ")",
                tuple(ids),
            ).fetchall()
        for row in rows:
            definition_id = str(row["definition_id"] or "")
            bucket = result.get(definition_id)
            if bucket is None:
                continue
            if row["last_attempt_job_id"] is not None:
                status = str(row["last_attempt_status"] or "error")
                bucket["last_result"] = {
                    "id": int(row["last_attempt_job_id"]),
                    "job_id": int(row["last_attempt_job_id"]),
                    "definition_id": definition_id,
                    "definition_name": str(row["definition_name"] or ""),
                    "ok": status == "ok",
                    "status": status,
                    "started_at": row["last_attempt_started_at"],
                    "ended_at": row["last_attempt_at"],
                    "trigger": str(row["last_attempt_trigger"] or "manual"),
                    "scheduled_slot": row["last_attempt_scheduled_slot"],
                }
            if row["last_success_job_id"] is not None:
                bucket["last_success"] = {
                    "id": int(row["last_success_job_id"]),
                    "job_id": int(row["last_success_job_id"]),
                    "definition_id": definition_id,
                    "definition_name": str(row["definition_name"] or ""),
                    "ok": True,
                    "status": "ok",
                    "started_at": row["last_success_started_at"],
                    "ended_at": row["last_success_at"],
                    "trigger": str(row["last_success_trigger"] or "manual"),
                    "scheduled_slot": row["last_success_scheduled_slot"],
                }
        return result

    def job_running(self, kind: str) -> Optional[Dict[str, Any]]:
        with self.conn() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE kind=? AND status='running' ORDER BY started_at DESC LIMIT 1",
                (kind,),
            ).fetchone()
            return self._row_to_dict(row) if row else None

    def job_mark_stale(
        self, job_id: int, reason: str = "Laufender Prozess nicht mehr aktiv"
    ) -> bool:
        """Markiert genau einen noch laufenden Job atomar als verwaist."""
        now = time.time()
        summary = _json_dumps_bounded(
            {"error": reason, "recovered_at": now}, _MAX_JOB_SUMMARY_BYTES
        )
        with self.conn() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT id, definition_id, definition_name, started_at, "
                "scheduled_slot, dry_run, trigger FROM jobs "
                "WHERE id=? AND status='running'",
                (int(job_id),),
            ).fetchone()
            cursor = connection.execute(
                "UPDATE jobs SET status='stale', ended_at=?, summary_json=? "
                "WHERE id=? AND status='running'",
                (now, summary, int(job_id)),
            )
            transitioned = int(cursor.rowcount or 0) == 1
            if transitioned:
                self._terminalize_running_attempts(
                    connection,
                    (int(job_id),),
                    status="stale",
                    ended_at=now,
                    error=reason,
                )
                if row is not None:
                    self._record_job_definition_schedule_state(
                        connection,
                        definition_id=str(row["definition_id"] or ""),
                        definition_name=str(row["definition_name"] or ""),
                        job_id=int(row["id"]),
                        started_at=float(row["started_at"]),
                        ended_at=now,
                        status="stale",
                        trigger=str(row["trigger"] or "manual"),
                        scheduled_slot=str(row["scheduled_slot"] or "") or None,
                        dry_run=bool(row["dry_run"]),
                    )
            return transitioned

    def jobs_mark_all_running_stale(
        self,
        *,
        kinds: Optional[Iterable[str]] = None,
        reason: str = "Laufender Prozess nicht mehr aktiv",
    ) -> int:
        now = time.time()
        summary = _json_dumps_bounded(
            {"error": reason, "recovered_at": now}, _MAX_JOB_SUMMARY_BYTES
        )
        normalized_kinds = tuple(
            dict.fromkeys(
                str(kind).strip() for kind in (kinds or ()) if str(kind).strip()
            )
        )
        kind_clause = ""
        kind_params: tuple[Any, ...] = ()
        if normalized_kinds:
            marks = ",".join("?" for _ in normalized_kinds)
            kind_clause = f" AND kind IN ({marks})"
            kind_params = normalized_kinds
        with self.conn() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT id, definition_id, definition_name, started_at, "
                "scheduled_slot, dry_run, trigger FROM jobs "
                f"WHERE status='running'{kind_clause}",
                kind_params,
            ).fetchall()
            cursor = connection.execute(
                "UPDATE jobs SET status='stale', ended_at=?, summary_json=? "
                f"WHERE status='running'{kind_clause}",
                (now, summary, *kind_params),
            )
            self._terminalize_running_attempts(
                connection,
                (int(row["id"]) for row in rows),
                status="stale",
                ended_at=now,
                error=reason,
            )
            for row in rows:
                self._record_job_definition_schedule_state(
                    connection,
                    definition_id=str(row["definition_id"] or ""),
                    definition_name=str(row["definition_name"] or ""),
                    job_id=int(row["id"]),
                    started_at=float(row["started_at"]),
                    ended_at=now,
                    status="stale",
                    trigger=str(row["trigger"] or "manual"),
                    scheduled_slot=str(row["scheduled_slot"] or "") or None,
                    dry_run=bool(row["dry_run"]),
                )
            return int(cursor.rowcount or 0)

    def jobs_mark_stale(
        self,
        max_age_sec: int,
        kind: Optional[str] = None,
        *,
        reason: str = "Prozess beendet oder Timeout überschritten",
    ) -> int:
        cutoff = time.time() - max(60, int(max_age_sec))
        now = time.time()
        summary = json.dumps({"error": reason, "recovered_at": now}, ensure_ascii=False)
        with self.conn() as connection:
            connection.execute("BEGIN IMMEDIATE")
            kind_clause = " AND kind=?" if kind else ""
            params: tuple[Any, ...] = (cutoff, str(kind)) if kind else (cutoff,)
            stale_rows = connection.execute(
                "SELECT id, definition_id, definition_name, started_at, "
                "scheduled_slot, dry_run, trigger FROM jobs "
                f"WHERE status='running' AND started_at < ?{kind_clause}",
                params,
            ).fetchall()
            update_params: tuple[Any, ...] = (
                (now, summary, cutoff, str(kind)) if kind else (now, summary, cutoff)
            )
            cursor = connection.execute(
                "UPDATE jobs SET status='stale', ended_at=?, summary_json=? "
                f"WHERE status='running' AND started_at < ?{kind_clause}",
                update_params,
            )
            self._terminalize_running_attempts(
                connection,
                (int(row["id"]) for row in stale_rows),
                status="stale",
                ended_at=now,
                error=reason,
            )
            for row in stale_rows:
                self._record_job_definition_schedule_state(
                    connection,
                    definition_id=str(row["definition_id"] or ""),
                    definition_name=str(row["definition_name"] or ""),
                    job_id=int(row["id"]),
                    started_at=float(row["started_at"]),
                    ended_at=now,
                    status="stale",
                    trigger=str(row["trigger"] or "manual"),
                    scheduled_slot=str(row["scheduled_slot"] or "") or None,
                    dry_run=bool(row["dry_run"]),
                )
            return int(cursor.rowcount or 0)

    def pair_last_result(
        self,
        pair_name: str,
        *,
        history_key: Optional[str] = None,
        limit: int = 1000,
    ) -> Optional[Dict[str, Any]]:
        with self.conn() as connection:
            if history_key:
                row = connection.execute(
                    "SELECT * FROM pair_runs WHERE history_key=? "
                    "ORDER BY COALESCE(ended_at, started_at) DESC, id DESC LIMIT 1",
                    (history_key,),
                ).fetchone()
                if not row:
                    legacy_key = self._default_history_key("", pair_name)
                    row = connection.execute(
                        "SELECT * FROM pair_runs "
                        "WHERE pair_name=? AND history_key IN ('', ?) "
                        "ORDER BY COALESCE(ended_at, started_at) DESC, id DESC LIMIT 1",
                        (pair_name, legacy_key),
                    ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM pair_runs WHERE pair_name=? "
                    "ORDER BY COALESCE(ended_at, started_at) DESC, id DESC LIMIT 1",
                    (pair_name,),
                ).fetchone()
            if row:
                return self._pair_row_to_result(row)
        return self._legacy_pair_last_result(pair_name, success_only=False, limit=limit)

    def pair_last_success(
        self,
        pair_name: str,
        *,
        history_key: Optional[str] = None,
        limit: int = 1000,
    ) -> Optional[Dict[str, Any]]:
        with self.conn() as connection:
            if history_key:
                row = connection.execute(
                    "SELECT * FROM pair_runs WHERE history_key=? AND ok=1 AND dry_run=0 "
                    "ORDER BY COALESCE(ended_at, started_at) DESC, id DESC LIMIT 1",
                    (history_key,),
                ).fetchone()
                if not row:
                    terminal_stable_attempt_exists = connection.execute(
                        "SELECT 1 FROM pair_runs WHERE history_key=? "
                        "AND (status<>'running' OR ended_at IS NOT NULL) LIMIT 1",
                        (history_key,),
                    ).fetchone()
                    if terminal_stable_attempt_exists:
                        return None
                    legacy_key = self._default_history_key("", pair_name)
                    row = connection.execute(
                        "SELECT * FROM pair_runs "
                        "WHERE pair_name=? AND history_key IN ('', ?) "
                        "AND ok=1 AND dry_run=0 "
                        "ORDER BY COALESCE(ended_at, started_at) DESC, id DESC LIMIT 1",
                        (pair_name, legacy_key),
                    ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM pair_runs WHERE pair_name=? AND ok=1 AND dry_run=0 "
                    "ORDER BY COALESCE(ended_at, started_at) DESC, id DESC LIMIT 1",
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

    def pair_baseline_state(
        self,
        pair_name: str,
        *,
        history_key: Optional[str] = None,
    ) -> str:
        """Bewertet, ob ein automatischer initialer Baseline-Resync sicher ist.

        ``new`` bedeutet, dass ausschließlich ein aktueller laufender Versuch
        bekannt ist. ``succeeded`` ist dauerhafte Erfolgsevidenz. Bereits
        abgeschlossene Versuche ohne nachweisbaren Erfolg sind ``ambiguous``
        und müssen aus Sicherheitsgründen manuell geprüft werden.
        """

        name = str(pair_name or "").strip()
        key = str(history_key or self._default_history_key("", name)).strip()
        if not name or not key:
            return "ambiguous"
        legacy_key = self._default_history_key("", name)

        with self.conn() as connection:
            stable = connection.execute(
                "SELECT * FROM pair_history_state WHERE history_key=?",
                (key,),
            ).fetchone()
            if stable and bool(stable["ever_succeeded"]):
                return "succeeded"

            success = connection.execute(
                "SELECT pair_name, COALESCE(ended_at, started_at) AS succeeded_at "
                "FROM pair_runs WHERE history_key=? AND ok=1 AND dry_run=0 "
                "ORDER BY COALESCE(ended_at, started_at) DESC, id DESC LIMIT 1",
                (key,),
            ).fetchone()
            if success:
                self._record_pair_history_state(
                    connection,
                    history_key=key,
                    pair_name=str(success["pair_name"] or name),
                    seen_at=float(success["succeeded_at"]),
                    succeeded=True,
                    terminal=True,
                    success_at=float(success["succeeded_at"]),
                )
                return "succeeded"

            # Beim Umstieg von name-basierten Keys auf stabile Config-IDs darf
            # der bereits vor dem Lookup reservierte laufende Versuch die alte
            # Erfolgsevidenz nicht verdecken. Die Evidenz wird auf den stabilen
            # Key übernommen und überlebt danach auch Job-Pruning und Umbenennen.
            if key != legacy_key:
                legacy = connection.execute(
                    "SELECT * FROM pair_history_state "
                    "WHERE history_key=? AND pair_name=?",
                    (legacy_key, name),
                ).fetchone()
                if legacy and bool(legacy["ever_succeeded"]):
                    success_at = float(
                        legacy["last_success_at"] or legacy["last_seen_at"]
                    )
                    self._record_pair_history_state(
                        connection,
                        history_key=key,
                        pair_name=name,
                        seen_at=float(legacy["last_seen_at"]),
                        succeeded=True,
                        terminal=bool(legacy["terminal_seen"]),
                        success_at=success_at,
                    )
                    return "succeeded"

                legacy_success = connection.execute(
                    "SELECT COALESCE(ended_at, started_at) AS succeeded_at "
                    "FROM pair_runs WHERE pair_name=? AND history_key IN ('', ?) "
                    "AND ok=1 AND dry_run=0 "
                    "ORDER BY COALESCE(ended_at, started_at) DESC, id DESC LIMIT 1",
                    (name, legacy_key),
                ).fetchone()
                if legacy_success:
                    success_at = float(legacy_success["succeeded_at"])
                    self._record_pair_history_state(
                        connection,
                        history_key=key,
                        pair_name=name,
                        seen_at=success_at,
                        succeeded=True,
                        terminal=True,
                        success_at=success_at,
                    )
                    return "succeeded"

            terminal_seen = bool(stable and stable["terminal_seen"])
            if not terminal_seen:
                terminal_seen = (
                    connection.execute(
                        "SELECT 1 FROM pair_runs WHERE history_key=? "
                        "AND (status<>'running' OR ended_at IS NOT NULL) LIMIT 1",
                        (key,),
                    ).fetchone()
                    is not None
                )
            if not terminal_seen and key != legacy_key:
                legacy = connection.execute(
                    "SELECT terminal_seen FROM pair_history_state "
                    "WHERE history_key=? AND pair_name=?",
                    (legacy_key, name),
                ).fetchone()
                terminal_seen = bool(legacy and legacy["terminal_seen"])
            return "ambiguous" if terminal_seen else "new"

    def pair_last_history(
        self, identities: Mapping[str, str]
    ) -> Dict[str, Dict[str, Optional[Dict[str, Any]]]]:
        """Lädt letzten Versuch und echten Erfolg für viele Identitäten auf einmal.

        ``identities`` bildet den stabilen, getypten History-Key auf den aktuellen
        Anzeigenamen ab. Alte name-basierte Zeilen werden nur verwendet, solange
        noch keine Zeile für den stabilen Key existiert.
        """
        requested = {
            str(history_key).strip(): str(pair_name).strip()
            for history_key, pair_name in identities.items()
            if str(history_key).strip() and str(pair_name).strip()
        }
        if not requested:
            return {}

        selected_ids: Dict[str, tuple[Optional[int], Optional[int]]] = {}
        rows_by_id: Dict[int, sqlite3.Row] = {}
        items = list(requested.items())
        with self.conn() as connection:
            # Four correlated Top-1 probes per identity keep work proportional
            # to the number of configured targets rather than all historical
            # attempts. Expression indexes cover the activity ordering.
            for offset in range(0, len(items), 300):
                chunk = items[offset : offset + 300]
                values = ",".join("(?, ?, ?)" for _ in chunk)
                params: list[str] = []
                for history_key, pair_name in chunk:
                    params.extend(
                        [
                            history_key,
                            pair_name,
                            self._default_history_key("", pair_name),
                        ]
                    )
                id_rows = connection.execute(
                    "WITH requested(history_key, pair_name, legacy_key) AS ("
                    f"VALUES {values}), stable AS MATERIALIZED ("
                    "SELECT history_key, pair_name, legacy_key, "
                    "(SELECT id FROM pair_runs WHERE history_key=requested.history_key "
                    "ORDER BY COALESCE(ended_at, started_at) DESC, id DESC LIMIT 1) "
                    "AS stable_last_id, "
                    "(SELECT id FROM pair_runs WHERE history_key=requested.history_key "
                    "AND ok=1 AND dry_run=0 "
                    "ORDER BY COALESCE(ended_at, started_at) DESC, id DESC LIMIT 1) "
                    "AS stable_success_id FROM requested) "
                    "SELECT history_key, stable_last_id, stable_success_id, "
                    "CASE WHEN stable_last_id IS NULL THEN "
                    "(SELECT id FROM pair_runs WHERE pair_name=stable.pair_name "
                    "AND history_key IN ('', stable.legacy_key) "
                    "ORDER BY COALESCE(ended_at, started_at) DESC, id DESC LIMIT 1) "
                    "END AS legacy_last_id, "
                    "CASE WHEN stable_last_id IS NULL THEN "
                    "(SELECT id FROM pair_runs WHERE pair_name=stable.pair_name "
                    "AND history_key IN ('', stable.legacy_key) "
                    "AND ok=1 AND dry_run=0 "
                    "ORDER BY COALESCE(ended_at, started_at) DESC, id DESC LIMIT 1) "
                    "END AS legacy_success_id FROM stable",
                    tuple(params),
                ).fetchall()
                row_ids: set[int] = set()
                for row in id_rows:
                    stable_last = row["stable_last_id"]
                    if stable_last is not None:
                        last_id = int(stable_last)
                        success_id = (
                            int(row["stable_success_id"])
                            if row["stable_success_id"] is not None
                            else None
                        )
                    else:
                        last_id = (
                            int(row["legacy_last_id"])
                            if row["legacy_last_id"] is not None
                            else None
                        )
                        success_id = (
                            int(row["legacy_success_id"])
                            if row["legacy_success_id"] is not None
                            else None
                        )
                    selected_ids[str(row["history_key"])] = (last_id, success_id)
                    if last_id is not None:
                        row_ids.add(last_id)
                    if success_id is not None:
                        row_ids.add(success_id)
                if not row_ids:
                    continue
                marks = ",".join("?" for _ in row_ids)
                for row in connection.execute(
                    f"SELECT * FROM pair_runs WHERE id IN ({marks})", tuple(row_ids)
                ).fetchall():
                    rows_by_id[int(row["id"])] = row

        result: Dict[str, Dict[str, Optional[Dict[str, Any]]]] = {}
        for history_key in requested:
            last_id, success_id = selected_ids.get(history_key, (None, None))
            last_row = rows_by_id.get(last_id) if last_id is not None else None
            success_row = rows_by_id.get(success_id) if success_id is not None else None
            last_success = (
                self._pair_row_to_result(success_row) if success_row else None
            )
            result[history_key] = {
                "last_result": self._pair_row_to_result(last_row) if last_row else None,
                "last_success": last_success,
            }
        return result

    def pair_last_results(self) -> Dict[str, Dict[str, Any]]:
        """Letztes Ergebnis je Pair über eine indexierte Abfrage."""
        with self.conn() as connection:
            rows = connection.execute(
                "SELECT pr.* FROM pair_runs pr "
                "JOIN (SELECT pair_name, MAX(COALESCE(ended_at, started_at)) AS max_ended "
                "FROM pair_runs GROUP BY pair_name) latest "
                "ON latest.pair_name=pr.pair_name "
                "AND latest.max_ended=COALESCE(pr.ended_at, pr.started_at) "
                "ORDER BY COALESCE(pr.ended_at, pr.started_at) DESC, pr.id DESC"
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
                "JOIN (SELECT pair_name, MAX(COALESCE(ended_at, started_at)) AS max_ended "
                "FROM pair_runs WHERE ok=1 AND dry_run=0 GROUP BY pair_name) latest "
                "ON latest.pair_name=pr.pair_name "
                "AND latest.max_ended=COALESCE(pr.ended_at, pr.started_at) "
                "WHERE pr.ok=1 AND pr.dry_run=0 "
                "ORDER BY COALESCE(pr.ended_at, pr.started_at) DESC, pr.id DESC"
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
                    not success_only
                    or (
                        pair.get("ok") is True
                        and pair.get("dry_run") is not True
                        and summary.get("dry_run") is not True
                    )
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
                "DELETE FROM jobs WHERE status<>'running' AND started_at < ? AND id NOT IN "
                "(SELECT id FROM jobs ORDER BY started_at DESC LIMIT ?)",
                (cutoff, keep_latest),
            )
            return int(cursor.rowcount or 0)

    def push_device_upsert(
        self,
        token: str,
        environment: str,
        *,
        app_version: str = "",
        lease_seconds: int = 7 * 86400,
        now: Optional[float] = None,
    ) -> None:
        now_value = float(time.time() if now is None else now)
        expires_at = now_value + max(3600, min(int(lease_seconds), 90 * 86400))
        with self.conn() as connection:
            connection.execute(
                "INSERT INTO push_devices(token, environment, app_version, created_at, "
                "updated_at, expires_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(token) DO UPDATE SET "
                "environment=excluded.environment, app_version=excluded.app_version, "
                "updated_at=excluded.updated_at, expires_at=excluded.expires_at",
                (
                    token,
                    environment,
                    app_version,
                    now_value,
                    now_value,
                    expires_at,
                ),
            )

    def push_device_delete(
        self, token: str, *, claim_owner: Optional[str] = None
    ) -> bool:
        with self.conn() as connection:
            if claim_owner is None:
                connection.execute("DELETE FROM push_outbox WHERE token=?", (token,))
            else:
                connection.execute(
                    "DELETE FROM push_outbox WHERE token=? "
                    "AND (status<>'sending' OR claim_owner=?)",
                    (token, str(claim_owner)),
                )
            cursor = connection.execute(
                "DELETE FROM push_devices WHERE token=?", (token,)
            )
            return bool(cursor.rowcount)

    def push_device_exists(self, token: str, *, now: Optional[float] = None) -> bool:
        now_value = float(time.time() if now is None else now)
        with self.conn() as connection:
            row = connection.execute(
                "SELECT 1 FROM push_devices WHERE token=? AND expires_at>?",
                (token, now_value),
            ).fetchone()
        return row is not None

    def push_devices_revoke_all(self) -> int:
        """Atomically remove every device and all queued/in-flight delivery rows."""

        with self.conn() as connection:
            count = int(
                connection.execute("SELECT COUNT(*) FROM push_devices").fetchone()[0]
            )
            connection.execute("DELETE FROM push_outbox")
            connection.execute("DELETE FROM push_devices")
        return count

    def push_devices(
        self, *, limit: int = 32, now: Optional[float] = None
    ) -> List[Dict[str, Any]]:
        now_value = float(time.time() if now is None else now)
        with self.conn() as connection:
            rows = connection.execute(
                "SELECT token, environment, app_version, created_at, updated_at "
                "FROM push_devices WHERE expires_at>? "
                "ORDER BY updated_at DESC LIMIT ?",
                (now_value, max(1, min(int(limit), 128))),
            ).fetchall()
        return [dict(row) for row in rows]

    def push_device_prune_expired(self, *, now: Optional[float] = None) -> int:
        now_value = float(time.time() if now is None else now)
        with self.conn() as connection:
            expired_tokens = [
                str(row["token"])
                for row in connection.execute(
                    "SELECT token FROM push_devices WHERE expires_at<=?",
                    (now_value,),
                ).fetchall()
            ]
            if expired_tokens:
                placeholders = ",".join("?" for _ in expired_tokens)
                connection.execute(
                    f"DELETE FROM push_outbox WHERE token IN ({placeholders}) "
                    "AND (status<>'sending' OR lease_until<=?)",
                    (*expired_tokens, now_value),
                )
            cursor = connection.execute(
                "DELETE FROM push_devices WHERE expires_at<=?", (now_value,)
            )
            return int(cursor.rowcount or 0)

    def push_outbox_enqueue(
        self,
        *,
        event: str,
        title: str,
        message: str,
        payload: Mapping[str, Any],
        dedupe_key: str,
        retention_seconds: int,
        device_limit: int = 128,
        now: Optional[float] = None,
    ) -> int:
        now_value = float(time.time() if now is None else now)
        expires_at = now_value + max(60, min(int(retention_seconds), 7 * 86400))
        payload_json = _json_dumps_bounded(dict(payload), 8 * 1024)
        inserted = 0
        with self.conn() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT token, environment FROM push_devices WHERE expires_at>? "
                "ORDER BY updated_at DESC LIMIT ?",
                (now_value, max(1, min(int(device_limit), 128))),
            ).fetchall()
            for row in rows:
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO push_outbox "
                    "(dedupe_key, token, environment, event, title, message, "
                    "payload_json, status, attempts, next_attempt_at, expires_at, "
                    "lease_until, apns_id, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, 0, ?, ?, ?)",
                    (
                        str(dedupe_key)[:256],
                        str(row["token"]),
                        str(row["environment"]),
                        str(event)[:64],
                        str(title)[:120],
                        str(message)[:900],
                        payload_json,
                        now_value,
                        expires_at,
                        str(uuid.uuid4()),
                        now_value,
                        now_value,
                    ),
                )
                inserted += int(cursor.rowcount or 0)
        return inserted

    def push_outbox_claim_due(
        self,
        *,
        claim_owner: str,
        limit: int = 32,
        lease_seconds: int = 60,
        now: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        now_value = float(time.time() if now is None else now)
        owner = str(claim_owner or "").strip()
        if not owner:
            raise ValueError("claim_owner darf nicht leer sein")
        with self.conn() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE push_outbox SET status='pending', lease_until=0, "
                "claim_owner='', "
                "updated_at=? WHERE status='sending' AND lease_until<=? "
                "AND expires_at>?",
                (now_value, now_value, now_value),
            )
            connection.execute(
                "UPDATE push_outbox SET status='failed', lease_until=0, "
                "claim_owner='', "
                "last_error=COALESCE(last_error, 'Benachrichtigung abgelaufen'), "
                "updated_at=? WHERE status IN ('pending', 'sending') AND expires_at<=?",
                (now_value, now_value),
            )
            connection.execute(
                "UPDATE push_outbox SET status='failed', lease_until=0, "
                "claim_owner='', "
                "last_error=COALESCE(last_error, 'Push-Gerät nicht mehr registriert'), "
                "updated_at=? WHERE status='pending' AND NOT EXISTS ("
                "SELECT 1 FROM push_devices WHERE push_devices.token=push_outbox.token"
                ")",
                (now_value,),
            )
            rows = connection.execute(
                "SELECT id FROM push_outbox WHERE status='pending' "
                "AND next_attempt_at<=? AND expires_at>? "
                "ORDER BY next_attempt_at, id LIMIT ?",
                (now_value, now_value, max(1, min(int(limit), 128))),
            ).fetchall()
            ids = [int(row["id"]) for row in rows]
            if not ids:
                return []
            placeholders = ",".join("?" for _ in ids)
            lease_until = now_value + max(10, min(int(lease_seconds), 300))
            connection.execute(
                f"UPDATE push_outbox SET status='sending', attempts=attempts+1, "
                f"lease_until=?, claim_owner=?, updated_at=? "
                f"WHERE status='pending' AND id IN ({placeholders})",
                (lease_until, owner, now_value, *ids),
            )
            claimed = connection.execute(
                f"SELECT * FROM push_outbox WHERE id IN ({placeholders}) "
                "AND status='sending' AND claim_owner=? "
                "ORDER BY next_attempt_at, id",
                (*ids, owner),
            ).fetchall()
        result: List[Dict[str, Any]] = []
        for row in claimed:
            item = dict(row)
            try:
                item["payload"] = json.loads(item.pop("payload_json") or "{}")
            except (json.JSONDecodeError, TypeError):
                item["payload"] = {}
            result.append(item)
        return result

    def push_outbox_renew_claims(
        self,
        outbox_ids: Iterable[int],
        *,
        claim_owner: str,
        lease_seconds: int = 60,
        now: Optional[float] = None,
    ) -> List[int]:
        """Verlängert ausschließlich noch aktive Claims des aufrufenden Dispatchers."""

        ids = list(dict.fromkeys(int(value) for value in outbox_ids))
        if not ids:
            return []
        owner = str(claim_owner or "").strip()
        if not owner:
            raise ValueError("claim_owner darf nicht leer sein")
        now_value = float(time.time() if now is None else now)
        lease_until = now_value + max(10, min(int(lease_seconds), 300))
        placeholders = ",".join("?" for _ in ids)
        with self.conn() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                f"UPDATE push_outbox SET lease_until=?, updated_at=? "
                f"WHERE status='sending' AND claim_owner=? "
                f"AND id IN ({placeholders})",
                (lease_until, now_value, owner, *ids),
            )
            rows = connection.execute(
                f"SELECT id FROM push_outbox WHERE status='sending' "
                f"AND claim_owner=? AND id IN ({placeholders}) ORDER BY id",
                (owner, *ids),
            ).fetchall()
        return [int(row["id"]) for row in rows]

    def push_outbox_finish(
        self,
        outbox_id: int,
        *,
        claim_owner: str,
        sent: bool,
        retry: bool = False,
        retry_delay_seconds: int = 60,
        error: str = "",
        max_attempts: int = 8,
        now: Optional[float] = None,
    ) -> str:
        now_value = float(time.time() if now is None else now)
        owner = str(claim_owner or "").strip()
        if not owner:
            raise ValueError("claim_owner darf nicht leer sein")
        with self.conn() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT attempts, expires_at, token FROM push_outbox "
                "WHERE id=? AND status='sending' AND claim_owner=?",
                (int(outbox_id), owner),
            ).fetchone()
            if not row:
                exists = connection.execute(
                    "SELECT 1 FROM push_outbox WHERE id=?", (int(outbox_id),)
                ).fetchone()
                return "not_owned" if exists else "missing"
            device_exists = connection.execute(
                "SELECT 1 FROM push_devices WHERE token=?", (str(row["token"]),)
            ).fetchone()
            elif_retry = (
                retry
                and device_exists is not None
                and int(row["attempts"] or 0) < max(1, int(max_attempts))
                and float(row["expires_at"] or 0) > now_value
            )
            if sent:
                status = "sent"
                next_attempt_at = now_value
                sent_at: float | None = now_value
            elif elif_retry:
                status = "pending"
                next_attempt_at = now_value + max(
                    1, min(int(retry_delay_seconds), 86400)
                )
                sent_at = None
            else:
                status = "failed"
                next_attempt_at = now_value
                sent_at = None
            connection.execute(
                "UPDATE push_outbox SET status=?, next_attempt_at=?, lease_until=0, "
                "claim_owner='', last_error=?, updated_at=?, sent_at=? "
                "WHERE id=? AND status='sending' AND claim_owner=?",
                (
                    status,
                    next_attempt_at,
                    str(error)[:1000] or None,
                    now_value,
                    sent_at,
                    int(outbox_id),
                    owner,
                ),
            )
            return status

    def push_outbox_status(self) -> Dict[str, Any]:
        with self.conn() as connection:
            counts = {
                str(row["status"]): int(row["count"])
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS count FROM push_outbox GROUP BY status"
                ).fetchall()
            }
            latest_error = connection.execute(
                "SELECT last_error, updated_at FROM push_outbox "
                "WHERE last_error IS NOT NULL ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
        return {
            "pending": counts.get("pending", 0) + counts.get("sending", 0),
            "sent": counts.get("sent", 0),
            "failed": counts.get("failed", 0),
            "last_error": str(latest_error["last_error"]) if latest_error else None,
            "last_error_at": float(latest_error["updated_at"])
            if latest_error
            else None,
        }

    def push_outbox_prune(
        self, *, older_than_days: int = 30, now: Optional[float] = None
    ) -> int:
        now_value = float(time.time() if now is None else now)
        cutoff = now_value - max(1, int(older_than_days)) * 86400
        with self.conn() as connection:
            cursor = connection.execute(
                "DELETE FROM push_outbox WHERE status IN ('sent', 'failed') "
                "AND updated_at<?",
                (cutoff,),
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

    def auth_retry_after_many(
        self, client_keys: Iterable[str], *, now: Optional[float] = None
    ) -> int:
        """Liefert die längste aktive Sperre mehrerer anonymer Auth-Sichten.

        Reauthentifizierungen werden sowohl pro Sitzung als auch pro Client-IP
        begrenzt. Beide Schlüssel werden absichtlich einzeln gespeichert, damit
        weder ein Sitzungs- noch ein IP-Wechsel den jeweils anderen Schutz
        aufhebt.
        """
        keys = tuple(dict.fromkeys(str(key) for key in client_keys if str(key)))
        if not keys:
            return 0
        now_value = float(time.time() if now is None else now)
        blocked_until = 0.0
        with self.conn() as connection:
            for key in keys:
                row = connection.execute(
                    "SELECT blocked_until FROM auth_failures WHERE client_key=?",
                    (key,),
                ).fetchone()
                if row:
                    blocked_until = max(blocked_until, float(row["blocked_until"] or 0))
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

    def auth_record_failure_many(
        self,
        client_keys: Iterable[str],
        *,
        window_sec: int,
        max_failures: int,
        lock_sec: int,
        now: Optional[float] = None,
    ) -> int:
        """Erfasst einen Fehlversuch atomar für mehrere anonyme Auth-Sichten."""
        keys = tuple(dict.fromkeys(str(key) for key in client_keys if str(key)))
        if not keys:
            return 0
        now_value = float(time.time() if now is None else now)
        longest_block = 0.0
        with self.conn() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for key in keys:
                row = connection.execute(
                    "SELECT * FROM auth_failures WHERE client_key=?", (key,)
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
                    "INSERT INTO auth_failures(client_key, window_started_at, "
                    "failure_count, blocked_until, updated_at) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(client_key) DO UPDATE SET "
                    "window_started_at=excluded.window_started_at, "
                    "failure_count=excluded.failure_count, "
                    "blocked_until=excluded.blocked_until, "
                    "updated_at=excluded.updated_at",
                    (key, window_started, count, blocked_until, now_value),
                )
                longest_block = max(longest_block, blocked_until)
        if longest_block > now_value:
            return max(1, int(longest_block - now_value))
        return 0

    def auth_clear(self, client_key: str) -> None:
        with self.conn() as connection:
            connection.execute(
                "DELETE FROM auth_failures WHERE client_key=?", (client_key,)
            )

    def auth_clear_many(self, client_keys: Iterable[str]) -> None:
        """Löscht ausschließlich die angegebenen Auth-Zähler atomar."""
        keys = tuple(dict.fromkeys(str(key) for key in client_keys if str(key)))
        if not keys:
            return
        with self.conn() as connection:
            connection.executemany(
                "DELETE FROM auth_failures WHERE client_key=?",
                ((key,) for key in keys),
            )

    def auth_prune(self, older_than_days: int = 7) -> int:
        cutoff = time.time() - max(1, int(older_than_days)) * 86400
        with self.conn() as connection:
            cursor = connection.execute(
                "DELETE FROM auth_failures WHERE updated_at < ?", (cutoff,)
            )
            return int(cursor.rowcount or 0)

    def webauthn_credentials(
        self, method: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        params: tuple[Any, ...] = ()
        where = ""
        if method is not None:
            if method not in {"passkey", "security_key"}:
                raise ValueError("Ungültige WebAuthn-Methode")
            where = " WHERE method=?"
            params = (method,)
        with self.conn() as connection:
            rows = connection.execute(
                "SELECT credential_id, method, public_key, sign_count, transports_json, "
                "device_type, backed_up, label, created_at, last_used_at "
                f"FROM webauthn_credentials{where} ORDER BY created_at, credential_id",
                params,
            ).fetchall()
        result: List[Dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                item["transports"] = json.loads(item.pop("transports_json") or "[]")
            except (json.JSONDecodeError, TypeError):
                item["transports"] = []
            item["backed_up"] = bool(item["backed_up"])
            result.append(item)
        return result

    def webauthn_credential_get(self, credential_id: str) -> Optional[Dict[str, Any]]:
        with self.conn() as connection:
            row = connection.execute(
                "SELECT credential_id, method, public_key, sign_count, transports_json, "
                "device_type, backed_up, label, created_at, last_used_at "
                "FROM webauthn_credentials WHERE credential_id=?",
                (credential_id,),
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        try:
            item["transports"] = json.loads(item.pop("transports_json") or "[]")
        except (json.JSONDecodeError, TypeError):
            item["transports"] = []
        item["backed_up"] = bool(item["backed_up"])
        return item

    def webauthn_credential_add(
        self,
        *,
        credential_id: str,
        method: str,
        public_key: bytes,
        sign_count: int,
        transports: Iterable[str],
        device_type: str,
        backed_up: bool,
        label: str,
    ) -> None:
        if method not in {"passkey", "security_key"}:
            raise ValueError("Ungültige WebAuthn-Methode")
        transport_values = list(dict.fromkeys(str(value) for value in transports))
        with self.conn() as connection:
            connection.execute(
                "INSERT INTO webauthn_credentials(credential_id, method, public_key, "
                "sign_count, transports_json, device_type, backed_up, label, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    credential_id,
                    method,
                    sqlite3.Binary(public_key),
                    max(0, int(sign_count)),
                    json.dumps(transport_values),
                    str(device_type or "")[:40],
                    1 if backed_up else 0,
                    str(label or "")[:80],
                    time.time(),
                ),
            )

    def webauthn_credential_used(
        self, credential_id: str, *, sign_count: int, device_type: str, backed_up: bool
    ) -> None:
        with self.conn() as connection:
            connection.execute(
                "UPDATE webauthn_credentials SET sign_count=?, device_type=?, "
                "backed_up=?, last_used_at=? WHERE credential_id=?",
                (
                    max(0, int(sign_count)),
                    str(device_type or "")[:40],
                    1 if backed_up else 0,
                    time.time(),
                    credential_id,
                ),
            )

    def webauthn_credential_delete(self, credential_id: str) -> bool:
        with self.conn() as connection:
            cursor = connection.execute(
                "DELETE FROM webauthn_credentials WHERE credential_id=?",
                (credential_id,),
            )
            return bool(cursor.rowcount)

    def webauthn_challenge_create(
        self,
        *,
        challenge_id: str,
        challenge: bytes,
        purpose: str,
        method: str,
        label: str = "",
        native: bool = False,
        app_binding: str = "",
        ttl_seconds: int = 300,
    ) -> None:
        now = time.time()
        with self.conn() as connection:
            connection.execute(
                "DELETE FROM webauthn_challenges WHERE expires_at < ?", (now,)
            )
            connection.execute(
                "DELETE FROM webauthn_challenges WHERE id IN ("
                "SELECT id FROM webauthn_challenges ORDER BY created_at DESC LIMIT -1 OFFSET 1023)"
            )
            connection.execute(
                "INSERT INTO webauthn_challenges(id, challenge, purpose, method, label, "
                "native, app_binding, expires_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    challenge_id,
                    sqlite3.Binary(challenge),
                    purpose,
                    method,
                    str(label or "")[:80],
                    1 if native else 0,
                    str(app_binding or "")[:128],
                    now + max(30, min(int(ttl_seconds), 600)),
                    now,
                ),
            )

    def webauthn_challenge_consume(
        self, challenge_id: str, *, purpose: str
    ) -> Optional[Dict[str, Any]]:
        now = time.time()
        with self.conn() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM webauthn_challenges WHERE id=?", (challenge_id,)
            ).fetchone()
            connection.execute(
                "DELETE FROM webauthn_challenges WHERE id=?", (challenge_id,)
            )
        if not row or str(row["purpose"]) != purpose or float(row["expires_at"]) < now:
            return None
        item = dict(row)
        item["native"] = bool(item["native"])
        return item

    def native_auth_exchange_create(
        self,
        token_hash: str,
        verifier_hash: str,
        username: str,
        *,
        ttl_seconds: int = 60,
    ) -> None:
        now = time.time()
        with self.conn() as connection:
            connection.execute(
                "DELETE FROM native_auth_exchanges WHERE expires_at < ?", (now,)
            )
            connection.execute(
                "DELETE FROM native_auth_exchanges WHERE token_hash IN ("
                "SELECT token_hash FROM native_auth_exchanges "
                "ORDER BY created_at DESC LIMIT -1 OFFSET 1023)"
            )
            connection.execute(
                "INSERT INTO native_auth_exchanges(token_hash, verifier_hash, username, "
                "expires_at, created_at) VALUES (?, ?, ?, ?, ?)",
                (
                    token_hash,
                    verifier_hash,
                    username,
                    now + max(15, min(int(ttl_seconds), 120)),
                    now,
                ),
            )

    def native_auth_exchange_consume(
        self, token_hash: str, verifier_hash: str
    ) -> Optional[str]:
        now = time.time()
        with self.conn() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT username, verifier_hash, expires_at FROM native_auth_exchanges "
                "WHERE token_hash=?",
                (token_hash,),
            ).fetchone()
            valid = bool(
                row
                and float(row["expires_at"]) >= now
                and secrets.compare_digest(str(row["verifier_hash"]), verifier_hash)
            )
            if valid or (row and float(row["expires_at"]) < now):
                connection.execute(
                    "DELETE FROM native_auth_exchanges WHERE token_hash=?",
                    (token_hash,),
                )
        if not row or not valid:
            return None
        return str(row["username"])

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


def database_path() -> Path:
    """Liefert den aktiven DB-Pfad, ohne den Singleton zu initialisieren."""

    return _db.path if _db is not None else _DB_PATH


def check_database_readonly(path: Path | None = None) -> bool:
    """Prüft die bestehende SQLite-Datei strikt read-only und ohne Anlage."""

    candidate = Path(path or database_path())
    try:
        if candidate.is_symlink():
            return False
        before = candidate.stat()
        if not stat.S_ISREG(before.st_mode) or before.st_size <= 0:
            return False
        resolved = candidate.resolve(strict=True)
        if not resolved.parent.is_dir():
            return False
        uri = f"{resolved.as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            connection.execute("PRAGMA query_only=ON")
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version <= 0 or version > _SCHEMA_VERSION:
                return False
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "jobs" not in tables:
                return False
            quick_check = str(connection.execute("PRAGMA quick_check(1)").fetchone()[0])
            if quick_check != "ok":
                return False
        finally:
            connection.close()
        after = candidate.stat()
        return (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return False


def get_db() -> Database:
    global _db
    if _db is None:
        with _singleton_lock:
            if _db is None:
                _db = Database()
    return _db
