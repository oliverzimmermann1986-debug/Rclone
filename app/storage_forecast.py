"""Bounded daily storage observations and evidence-based capacity forecasts."""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from statistics import median
from typing import Any, Mapping

from .job_definitions import data_paths_by_id
from .jobs.rclone_sync import _is_remote
from .rclone_args import rclone_subprocess_env
from .utils import bounded_int

_DAY = 86400
_MAX_TARGETS = 512
_MAX_PER_TICK = 8
_PROBE_SECONDS = 8.0
_MAX_JSON_BYTES = 64 * 1024
_SAMPLE_LOCK = threading.Lock()


def _snapshot(cfg: Any) -> dict[str, Any]:
    return dict(cfg) if isinstance(cfg, Mapping) else cfg.snapshot()


def settings(cfg: Any) -> dict[str, Any]:
    raw = (_snapshot(cfg).get("backup") or {}).get("storage_forecast") or {}
    capacities = raw.get("capacities") or {}
    if not isinstance(capacities, Mapping):
        capacities = {}
    return {
        "enabled": raw.get("enabled", True) is True,
        "sample_interval_hours": bounded_int(
            raw.get("sample_interval_hours", 24), default=24, minimum=1, maximum=168
        ),
        "warning_days": bounded_int(
            raw.get("warning_days", 30), default=30, minimum=1, maximum=365
        ),
        "history_days": bounded_int(
            raw.get("history_days", 180), default=180, minimum=7, maximum=365
        ),
        "capacities": {
            str(key): int(value)
            for key, value in capacities.items()
            if isinstance(value, int)
            and not isinstance(value, bool)
            and 0 < value <= 2**63 - 1
        },
    }


def targets(cfg: Any) -> list[dict[str, Any]]:
    capacities = settings(cfg)["capacities"]
    result = []
    for pair_id, pair in data_paths_by_id(cfg).items():
        for side in ("local", "remote"):
            endpoint = str(pair.get(side) or "").strip()
            if not endpoint:
                continue
            identity = f"{pair_id}:{side}"
            capacity = capacities.get(identity)
            kind = "remote" if _is_remote(endpoint) else "local"
            binding = hashlib.sha256(
                json.dumps([kind, endpoint, capacity], separators=(",", ":")).encode()
            ).hexdigest()
            result.append(
                {
                    "identity": identity,
                    "label": f"{pair.get('name') or pair_id} · {'Lokal' if side == 'local' else 'Remote'}",
                    "kind": kind,
                    "endpoint": endpoint,
                    "binding": binding,
                    "capacity": capacity,
                }
            )
    return result[:_MAX_TARGETS]


def ensure_tables(db: Any) -> None:
    with db.conn() as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS storage_forecast_samples ("
            "identity TEXT NOT NULL, binding TEXT NOT NULL, sample_day INTEGER NOT NULL, "
            "observed_at REAL NOT NULL, total_bytes INTEGER, used_bytes INTEGER, "
            "free_bytes INTEGER, state TEXT NOT NULL, reason TEXT NOT NULL, "
            "PRIMARY KEY(identity,binding,sample_day))"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS storage_forecast_samples_age "
            "ON storage_forecast_samples(observed_at)"
        )


def _number(value: Any) -> int | None:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not 0 <= value <= 2**63 - 1
        or not math.isfinite(value)
    ):
        return None
    return int(value)


def _bounded_json_command(command: list[str], deadline: float) -> dict[str, Any]:
    """Read at most 64 KiB; terminate a stalled or verbose read-only rclone probe."""
    if deadline <= time.monotonic():
        raise TimeoutError("Speichermessung hat ihr Zeitlimit erreicht")
    proc = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
        env=rclone_subprocess_env(),
    )
    output: list[bytes] = []
    failures: list[Exception] = []

    def read() -> None:
        try:
            output.append(proc.stdout.read(_MAX_JSON_BYTES + 1))
        except Exception as exc:
            failures.append(exc)

    reader = threading.Thread(target=read, name="storage-quota-probe", daemon=True)
    reader.start()
    try:
        reader.join(max(0.0, deadline - time.monotonic()))
        if reader.is_alive():
            raise TimeoutError("Speichermessung hat ihr Zeitlimit erreicht")
        if failures or not output or len(output[0]) > _MAX_JSON_BYTES:
            raise ValueError("Speicherantwort ist ungültig oder zu groß")
        proc.wait(timeout=max(0.01, deadline - time.monotonic()))
        if proc.returncode:
            raise ValueError("Der Anbieter unterstützt diese Speicherauskunft nicht")
        value = json.loads(output[0])
        if not isinstance(value, dict):
            raise ValueError("Speicherantwort ist kein Objekt")
        return value
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=0.5)
        reader.join(timeout=0.5)
        if not reader.is_alive():
            proc.stdout.close()


def _probe_target(target: dict[str, Any]) -> dict[str, Any]:
    deadline = time.monotonic() + _PROBE_SECONDS
    if target["kind"] == "local":
        try:
            # Isolate statvfs as well: an unresponsive mounted filesystem must
            # not hold the independent heartbeat process beyond this deadline.
            disk = _bounded_json_command(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    "import json,shutil,sys; d=shutil.disk_usage(sys.argv[1]); "
                    "print(json.dumps(dict(total_bytes=d.total,used_bytes=d.used,free_bytes=d.free)))",
                    target["endpoint"],
                ],
                deadline,
            )
            return {
                "total_bytes": _number(disk.get("total_bytes")),
                "used_bytes": _number(disk.get("used_bytes")),
                "free_bytes": _number(disk.get("free_bytes")),
                "state": "ok",
                "reason": "Belegung des Dateisystems",
            }
        except (OSError, ValueError, TimeoutError, subprocess.TimeoutExpired):
            return {"state": "error", "reason": "Speicherpfad ist nicht erreichbar"}
    quota = {}
    if not target["capacity"]:
        try:
            quota = _bounded_json_command(
                ["rclone", "about", "--json", "--", target["endpoint"]], deadline
            )
        except (OSError, ValueError, TimeoutError, subprocess.TimeoutExpired):
            pass
    total = target["capacity"] or _number(quota.get("total"))
    used, free = _number(quota.get("used")), _number(quota.get("free"))
    if target["capacity"]:
        try:
            size = _bounded_json_command(
                ["rclone", "size", "--json", "--", target["endpoint"]], deadline
            )
            used = _number(size.get("bytes"))
        except (OSError, ValueError, TimeoutError, subprocess.TimeoutExpired):
            pass
    if total is None or total <= 0:
        return {
            "used_bytes": used,
            "free_bytes": free,
            "state": "unknown",
            "reason": "Anbieter liefert keine Kapazität; bei Bedarf eigene Kapazität hinterlegen",
        }
    if target["capacity"]:
        free = max(0, total - used) if used is not None else None
    elif used is None and free is not None:
        used = max(0, total - free)
    elif free is None and used is not None:
        free = max(0, total - used)
    if used is None or free is None:
        return {
            "total_bytes": total,
            "state": "unknown",
            "reason": "Kapazität bekannt, Belegung konnte nicht gemessen werden",
        }
    return {
        "total_bytes": total,
        "used_bytes": used,
        "free_bytes": free,
        "state": "ok",
        "reason": "Eigene Zielkapazität und gemessener Zielbestand"
        if target["capacity"]
        else "Anbieter-Kontingent",
    }


def _history(
    db: Any, target: dict[str, Any], since: float, until: float
) -> list[dict[str, Any]]:
    with db.conn() as connection:
        rows = connection.execute(
            "SELECT observed_at,total_bytes,used_bytes,free_bytes,state,reason "
            "FROM storage_forecast_samples WHERE identity=? AND binding=? "
            "AND observed_at>=? AND observed_at<=? ORDER BY sample_day DESC LIMIT 365",
            (target["identity"], target["binding"], since, until),
        ).fetchall()
    return [dict(row) for row in reversed(rows)]


def estimate(
    samples: list[dict[str, Any]], *, now: float, options: Mapping[str, Any]
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "total_bytes": None,
        "used_bytes": None,
        "free_bytes": None,
        "observed_at": None,
        "samples": 0,
        "growth_bytes_per_day": None,
        "days_to_full": None,
        "estimated_full_at": None,
        "state": "insufficient_samples",
        "confidence": "none",
        "reason": "Noch keine Messungen vorhanden",
    }
    if not samples:
        return result
    latest = samples[-1]
    result.update(
        {
            k: latest.get(k)
            for k in ("total_bytes", "used_bytes", "free_bytes", "observed_at")
        }
    )
    if now - latest["observed_at"] > max(
        2 * options["sample_interval_hours"] * 3600, 2 * _DAY
    ):
        result.update(state="stale", reason="Letzte Speichermessung ist veraltet")
        return result
    if latest.get("state") != "ok":
        result.update(
            state=latest.get("state", "unknown"),
            reason=latest.get("reason", "Belegung unbekannt"),
        )
        return result
    if (
        any(
            _number(latest.get(key)) is None
            for key in ("total_bytes", "used_bytes", "free_bytes")
        )
        or latest["total_bytes"] <= 0
    ):
        result.update(
            state="unknown",
            reason="Speichermessung enthält keine vollständige Kapazitätsangabe",
        )
        return result
    usable = [
        row
        for row in samples
        if row.get("state") == "ok"
        and _number(row.get("used_bytes")) is not None
        and row.get("total_bytes") == latest.get("total_bytes")
    ]
    result["samples"] = len(usable)
    if latest["free_bytes"] <= 0:
        result.update(
            state="full",
            confidence="high",
            days_to_full=0,
            estimated_full_at=latest["observed_at"],
            reason="Kein freier Speicherplatz gemessen",
        )
        return result
    span = (
        (usable[-1]["observed_at"] - usable[0]["observed_at"]) / _DAY if usable else 0
    )
    if len(usable) < 3 or span < 2:
        result["reason"] = "Mindestens drei Tagesmessungen über zwei Tage erforderlich"
        return result
    # Theil-Sen resists isolated spikes, backup deletions and measurement noise.
    fitting = (
        usable
        if len(usable) <= 60
        else [usable[round(index * (len(usable) - 1) / 59)] for index in range(60)]
    )
    slopes = [
        (second["used_bytes"] - first["used_bytes"])
        / ((second["observed_at"] - first["observed_at"]) / _DAY)
        for i, first in enumerate(fitting)
        for second in fitting[i + 1 :]
        if second["observed_at"] > first["observed_at"]
    ]
    slope = median(slopes)
    if slope <= 0:
        result.update(
            state="not_growing",
            confidence="medium",
            growth_bytes_per_day=0,
            reason="Im beobachteten Zeitraum kein anhaltendes Wachstum",
        )
        return result
    residuals = [
        row["used_bytes"]
        - slope * (row["observed_at"] - usable[0]["observed_at"]) / _DAY
        for row in usable
    ]
    center = median(residuals)
    noise = median(abs(value - center) for value in residuals)
    if slope <= max(1, 2 * noise / span):
        result.update(
            state="uncertain",
            confidence="low",
            reason="Schwankungen erlauben noch keine belastbare Wachstumsprognose",
        )
        return result
    deviation = median(abs(value - slope) for value in slopes)
    conservative = slope + min(deviation, slope)
    days = latest["free_bytes"] / conservative
    date = latest["observed_at"] + days * _DAY
    result.update(
        state="warning" if days <= options["warning_days"] else "ok",
        confidence="high"
        if len(usable) >= 14 and span >= 14 and noise < slope
        else "medium"
        if len(usable) >= 7
        else "low",
        growth_bytes_per_day=round(conservative, 2),
        days_to_full=round(days, 2),
        estimated_full_at=date if math.isfinite(date) and date < 253402300799 else None,
        reason="Konservative Schätzung aus gemessener Belegung; Änderungen am Datenbestand können sie verschieben",
    )
    return result


def forecasts(cfg: Any, db: Any, now: float | None = None) -> dict[str, Any]:
    now = float(time.time() if now is None else now)
    options = settings(cfg)
    ensure_tables(db)
    items = []
    for target in targets(cfg):
        rows = _history(db, target, now - options["history_days"] * _DAY, now)
        items.append(
            {
                "identity": target["identity"],
                "label": target["label"],
                "kind": target["kind"],
                "capacity_bytes": target["capacity"],
                "measurement_scope": "filesystem"
                if target["kind"] == "local"
                else "configured_target"
                if target["capacity"]
                else "remote_account",
                **estimate(rows, now=now, options=options),
            }
        )
    return {
        "enabled": options["enabled"],
        "observed_at": now,
        "items": items,
        "settings": {
            k: options[k]
            for k in ("sample_interval_hours", "warning_days", "history_days")
        },
    }


def history(
    cfg: Any, db: Any, identity: str, now: float | None = None
) -> dict[str, Any]:
    now = float(time.time() if now is None else now)
    target = next(
        (target for target in targets(cfg) if target["identity"] == identity), None
    )
    if target is None:
        raise LookupError("Speicherziel nicht gefunden")
    ensure_tables(db)
    rows = _history(db, target, now - settings(cfg)["history_days"] * _DAY, now)
    return {"identity": identity, "items": rows, "total": len(rows)}


def sample_targets(
    cfg: Any,
    db: Any,
    *,
    identity: str | None = None,
    now: float | None = None,
    force: bool = False,
    include_forecasts: bool = True,
) -> dict[str, Any]:
    now = float(time.time() if now is None else now)
    options = settings(cfg)

    def response() -> dict[str, Any]:
        return (
            forecasts(cfg, db, now)
            if include_forecasts
            else {"enabled": options["enabled"], "observed_at": now}
        )

    ensure_tables(db)
    configured = targets(cfg)
    if identity and identity not in {target["identity"] for target in configured}:
        raise LookupError("Speicherziel nicht gefunden")
    if not options["enabled"]:
        return {
            "ok": True,
            "sampled": 0,
            "skipped": len(configured),
            "errors": 0,
            **response(),
        }
    if not _SAMPLE_LOCK.acquire(blocking=False):
        return {
            "ok": True,
            "sampled": 0,
            "skipped": len(configured),
            "errors": 0,
            "reason": "Messung läuft bereits",
            **response(),
        }
    try:
        due = []
        for target in configured:
            if identity and target["identity"] != identity:
                continue
            rows = _history(db, target, now - options["history_days"] * _DAY, now)
            last = rows[-1] if rows else {}
            age = now - last.get("observed_at", 0)
            interval = (
                min(options["sample_interval_hours"] * 3600, 3600)
                if last.get("state") == "error"
                else options["sample_interval_hours"] * 3600
            )
            if force or not last or age >= interval:
                due.append((last.get("observed_at", 0), target))
        due.sort(key=lambda item: (item[0], item[1]["identity"]))
        selected = [target for _age, target in due[:_MAX_PER_TICK]]
        with ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="storage-forecast"
        ) as workers:
            measurements = list(workers.map(_probe_target, selected))
        day = int(now // _DAY)
        with db.conn() as connection:
            for target, measurement in zip(selected, measurements):
                connection.execute(
                    "INSERT INTO storage_forecast_samples "
                    "(identity,binding,sample_day,observed_at,total_bytes,used_bytes,free_bytes,state,reason) "
                    "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(identity,binding,sample_day) DO UPDATE SET "
                    "observed_at=excluded.observed_at,total_bytes=excluded.total_bytes,used_bytes=excluded.used_bytes,"
                    "free_bytes=excluded.free_bytes,state=excluded.state,reason=excluded.reason",
                    (
                        target["identity"],
                        target["binding"],
                        day,
                        now,
                        *(
                            _number(measurement.get(k))
                            for k in ("total_bytes", "used_bytes", "free_bytes")
                        ),
                        measurement.get("state", "unknown"),
                        str(measurement.get("reason") or "")[:300],
                    ),
                )
            connection.execute(
                "DELETE FROM storage_forecast_samples WHERE observed_at<?",
                (now - options["history_days"] * _DAY,),
            )
            connection.execute(
                "DELETE FROM storage_forecast_samples WHERE rowid NOT IN (SELECT rowid FROM storage_forecast_samples ORDER BY observed_at DESC LIMIT ?)",
                (_MAX_TARGETS * 365,),
            )
        return {
            "ok": True,
            "sampled": len(selected),
            "skipped": len(due) - len(selected),
            "errors": sum(item.get("state") == "error" for item in measurements),
            **response(),
        }
    finally:
        _SAMPLE_LOCK.release()


def sample_if_due(cfg: Any, db: Any, now: float | None = None) -> dict[str, Any]:
    """One independent operations tick: at most eight probes, four at a time."""
    return sample_targets(cfg, db, now=now, include_forecasts=False)
