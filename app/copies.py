"""Kopien-Matrix: Wie viele Sicherungen hat ein Quellpfad, und wie alt sind sie?

Die Pair-Liste beantwortet "läuft jeder Job?". Sie beantwortet nicht "ist jeder
Datenbestand mehrfach gesichert?". Dafür muss man nach Quelle gruppieren statt
nach Pair — genau das leistet dieses Modul und damit die 3-2-1-Frage.
"""

from __future__ import annotations

import time
from typing import Any, Mapping, Optional
from urllib.parse import quote

from .overdue import is_scheduled

# Eine einzige Kopie ist kein Backup, sondern eine Verschiebung mit Zeitversatz.
MIN_RECOMMENDED_COPIES = 2


def _normalize(path: str) -> str:
    return str(path or "").strip().rstrip("/")


def _is_remote(path: str) -> bool:
    value = _normalize(path)
    return bool(value) and not value.startswith("/") and ":" in value.split("/", 1)[0]


def target_scope(path: str) -> str:
    """Grobe Ablageeinheit eines Ziels.

    Bei Remotes der Remote-Name (``wasabi:``), lokal der Mount- bzw.
    Wurzelpfad. Zwei Kopien im selben Scope sind für 3-2-1 nur eine: fällt der
    Anbieter oder die Platte aus, sind beide weg.
    """
    value = _normalize(path)
    if not value:
        return ""
    if _is_remote(value):
        return value.split(":", 1)[0] + ":"
    parts = [part for part in value.split("/") if part]
    return "/" + "/".join(parts[:2]) if parts else "/"


def _direction_endpoints(pair: Mapping[str, Any]) -> tuple[str, str]:
    """(Quelle, Sicherungskopie) — gleiche Regel wie Check und Restore-Drill."""
    direction = str(pair.get("direction") or "bisync").lower().strip()
    if direction == "push":
        return _normalize(pair.get("local")), _normalize(pair.get("remote"))
    return _normalize(pair.get("remote")), _normalize(pair.get("local"))


def build_matrix(cfg, db, *, now: Optional[float] = None) -> dict[str, Any]:
    """Gruppiert aktive Pairs nach Quelle und bewertet die Abdeckung."""
    from .jobs.scheduler import rclone_history_key

    now_value = float(time.time() if now is None else now)
    backup = cfg.get("backup", default={}) or {}
    default_schedule = str(backup.get("default_schedule") or "").strip()

    pairs = [
        pair
        for pair in (backup.get("pairs") or [])
        if isinstance(pair, Mapping)
        and pair.get("enabled", True)
        and str(pair.get("name") or "")
    ]

    identities = {rclone_history_key(pair): str(pair.get("name")) for pair in pairs}
    histories = db.pair_last_history(identities) if identities else {}

    sources: dict[str, dict[str, Any]] = {}
    for pair in pairs:
        name = str(pair.get("name"))
        source, target = _direction_endpoints(pair)
        if not source or not target:
            continue
        history = histories.get(rclone_history_key(pair)) or {}
        success = history.get("last_success") or {}
        last_success = float(success["ended_at"]) if success.get("ended_at") else None
        latest = history.get("last_result") or {}

        entry = sources.setdefault(
            source,
            {"source": source, "copies": [], "scopes": set()},
        )
        scope = target_scope(target)
        entry["scopes"].add(scope)
        entry["copies"].append(
            {
                "pair": name,
                "target": target,
                "scope": scope,
                "remote": _is_remote(target),
                "direction": str(pair.get("direction") or "bisync"),
                "mode": str(pair.get("mode") or ""),
                "scheduled": is_scheduled(pair, default_schedule),
                "last_success": last_success,
                "age_hours": round((now_value - last_success) / 3600.0, 1)
                if last_success is not None
                else None,
                "last_status": latest.get("status") if latest else None,
                "versioned": bool(
                    pair.get("backup_dir")
                    or pair.get("backup_dir1")
                    or pair.get("backup_dir2")
                ),
            }
        )

    rows: list[dict[str, Any]] = []
    for entry in sources.values():
        copies = sorted(entry["copies"], key=lambda item: item["pair"].casefold())
        scopes = sorted(scope for scope in entry["scopes"] if scope)
        ages = [item["age_hours"] for item in copies if item["age_hours"] is not None]
        offsite = [item for item in copies if item["remote"]]
        findings: list[str] = []
        if len(copies) < MIN_RECOMMENDED_COPIES:
            findings.append("nur eine Kopie")
        if len(scopes) < 2 and len(copies) >= MIN_RECOMMENDED_COPIES:
            findings.append("alle Kopien im selben Speicherort")
        if not offsite:
            findings.append("keine Kopie außer Haus")
        if not any(item["versioned"] for item in copies):
            findings.append("keine Versionsablage")
        never = [item for item in copies if item["last_success"] is None]
        if never:
            findings.append(f"{len(never)} Kopie(n) ohne je erfolgreichen Lauf")

        rows.append(
            {
                "source": entry["source"],
                "id": quote(entry["source"], safe=""),
                "copy_count": len(copies),
                "scope_count": len(scopes),
                "scopes": scopes,
                "offsite_count": len(offsite),
                "newest_age_hours": min(ages) if ages else None,
                "oldest_age_hours": max(ages) if ages else None,
                "copies": copies,
                "findings": findings,
                "level": "error"
                if len(copies) < MIN_RECOMMENDED_COPIES or never
                else ("warn" if findings else "ok"),
            }
        )

    rows.sort(key=lambda row: (row["copy_count"], row["source"].casefold()))
    return {
        "generated_at": now_value,
        "sources": rows,
        "totals": {
            "sources": len(rows),
            "single_copy": sum(1 for row in rows if row["copy_count"] < 2),
            "without_offsite": sum(1 for row in rows if row["offsite_count"] == 0),
            "without_versioning": sum(
                1
                for row in rows
                if not any(item["versioned"] for item in row["copies"])
            ),
        },
    }
