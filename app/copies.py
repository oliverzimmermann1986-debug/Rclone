"""Kopien-Matrix: Wie viele Sicherungen hat ein Quellpfad, und wie alt sind sie?

Die Pair-Liste beantwortet "läuft jeder Job?". Sie beantwortet nicht "ist jeder
Datenbestand mehrfach gesichert?". Dafür muss man nach Quelle gruppieren statt
nach Pair — genau das leistet dieses Modul und damit die 3-2-1-Frage.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib.parse import quote

from .job_definitions import scheduled_data_path_ids, stable_data_path_id

# Eine einzige Kopie ist kein Backup, sondern eine Verschiebung mit Zeitversatz.
MIN_RECOMMENDED_COPIES = 2


def _normalize(path: str) -> str:
    return str(path or "").strip().rstrip("/")


def _is_remote(path: str) -> bool:
    value = _normalize(path)
    if len(value) >= 3 and value[1] == ":" and value[2] in {"/", "\\"}:
        # Ein Windows-Laufwerk ist kein einbuchstabiger rclone-Remote.
        return False
    return bool(value) and not value.startswith("/") and ":" in value.split("/", 1)[0]


def _path_stat(path: Path):
    """Separater Wrapper, damit Dateisystem-Domains deterministisch testbar sind."""
    return os.stat(path)


def _explicit_failure_domain(pair: Mapping[str, Any]) -> str:
    """Liest den kanonischen Namen und den kompatiblen ``location_id``-Alias."""
    return str(pair.get("failure_domain") or pair.get("location_id") or "").strip()


def _local_failure_domain(path: str) -> dict[str, Any]:
    """Bestimmt das reale Dateisystem eines lokalen Ziels konservativ.

    Existiert das Ziel noch nicht, wird bis zum nächsten existierenden Elternpfad
    aufgestiegen. Das verhindert, dass zwei lediglich *geplante* Mountpunkte auf
    demselben Root-Dateisystem als zwei unabhängige Datenträger gelten. Kann gar
    kein Pfad aufgelöst werden, landen alle solchen Ziele absichtlich in einer
    gemeinsamen unsicheren Domain.
    """
    value = _normalize(path)
    candidate = Path(value).expanduser()
    requested = candidate
    inaccessible = False

    while True:
        try:
            device = int(_path_stat(candidate).st_dev)
            exact = candidate == requested
            warning = None
            if not exact:
                reason = "nicht vorhanden"
                if inaccessible:
                    reason = "nicht vollständig zugreifbar"
                warning = (
                    f"Lokales Ziel {value} ist {reason}; Ausfallbereich wurde "
                    f"vom nächsten vorhandenen Elternpfad {candidate} abgeleitet."
                )
            return {
                "id": f"local-device:{device}",
                "label": f"Lokales Dateisystem {device}",
                "kind": "local",
                "confidence": "high" if exact else "low",
                "source": "st_dev" if exact else "nearest_existing_parent",
                "resolved_path": str(candidate),
                "warning": warning,
            }
        except (FileNotFoundError, NotADirectoryError):
            pass
        except (OSError, ValueError):
            # Auch bei Berechtigungs-/Pfadfehlern kann ein Elternpfad noch eine
            # konservative, reale Gruppierung liefern.
            inaccessible = True

        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent

    return {
        "id": "local-device:unresolved",
        "label": "Lokaler Speicherort (nicht auflösbar)",
        "kind": "local",
        "confidence": "low",
        "source": "unresolved",
        "resolved_path": None,
        "warning": (
            f"Lokales Ziel {value} konnte keinem Dateisystem zugeordnet werden; "
            "es wird vorsichtshalber mit allen nicht auflösbaren Zielen gruppiert."
        ),
    }


def _target_failure_domain(
    path: str, pair: Optional[Mapping[str, Any]] = None
) -> dict[str, Any]:
    """Liefert ID, Herkunft und Vertrauensniveau des Ausfallbereichs."""
    value = _normalize(path)
    if not value:
        return {
            "id": "",
            "label": "",
            "kind": "unknown",
            "confidence": "low",
            "source": "missing",
            "resolved_path": None,
            "warning": "Zielpfad fehlt; Ausfallbereich ist unbekannt.",
        }

    if not _is_remote(value):
        return _local_failure_domain(value)

    explicit = _explicit_failure_domain(pair or {})
    if explicit:
        return {
            "id": f"explicit:{explicit.casefold()}",
            "label": explicit,
            "kind": "remote",
            "confidence": "high",
            "source": "explicit",
            "resolved_path": None,
            "warning": None,
        }

    remote_name = value.split(":", 1)[0] + ":"
    return {
        "id": f"remote-name:{remote_name.casefold()}",
        "label": remote_name,
        "kind": "remote",
        "confidence": "medium",
        "source": "remote_name",
        "resolved_path": None,
        "warning": (
            f"Für {value} ist keine failure_domain/location_id hinterlegt; "
            f"der Remote-Name {remote_name} wird nur als Näherung verwendet."
        ),
    }


def target_scope(path: str) -> str:
    """Stabile ID der tatsächlichen bzw. konservativ abgeleiteten Ablageeinheit.

    Für ausführliche Diagnoseinformationen nutzt die Kopien-Matrix zusätzlich
    ``domains`` und ``copies[].failure_domain``.
    """
    return str(_target_failure_domain(path).get("id") or "")


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
    scheduled_ids = scheduled_data_path_ids(cfg)

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
            {"source": source, "copies": [], "domains": {}},
        )
        domain = _target_failure_domain(target, pair)
        scope_id = str(domain.get("id") or "")
        if scope_id:
            aggregate = entry["domains"].setdefault(
                scope_id,
                {
                    **domain,
                    "targets": [],
                    "warnings": [],
                },
            )
            aggregate["targets"].append(target)
            warning = domain.get("warning")
            if warning and warning not in aggregate["warnings"]:
                aggregate["warnings"].append(warning)
            # Eine schwächere Zuordnung innerhalb derselben Domain darf nicht
            # durch eine andere, exakte Zuordnung verdeckt werden.
            confidence_rank = {"low": 0, "medium": 1, "high": 2}
            if confidence_rank.get(
                str(domain.get("confidence")), 0
            ) < confidence_rank.get(str(aggregate.get("confidence")), 0):
                aggregate["confidence"] = domain.get("confidence")
        entry["copies"].append(
            {
                "pair": name,
                "target": target,
                "scope": domain.get("label") or scope_id,
                "scope_id": scope_id,
                "failure_domain": domain,
                "remote": _is_remote(target),
                "direction": str(pair.get("direction") or "bisync"),
                "mode": str(pair.get("mode") or ""),
                "scheduled": stable_data_path_id(pair) in scheduled_ids,
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
        domains = sorted(
            entry["domains"].values(),
            key=lambda item: (str(item.get("label") or "").casefold(), item["id"]),
        )
        scopes = [str(domain.get("label") or domain["id"]) for domain in domains]
        ages = [item["age_hours"] for item in copies if item["age_hours"] is not None]
        offsite = [item for item in copies if item["remote"]]
        domain_warnings = [
            warning for domain in domains for warning in domain.get("warnings", [])
        ]
        findings: list[str] = []
        if len(copies) < MIN_RECOMMENDED_COPIES:
            findings.append("nur eine Kopie")
        if len(scopes) < 2 and len(copies) >= MIN_RECOMMENDED_COPIES:
            findings.append("alle Kopien im selben Speicherort")
        if not offsite:
            findings.append("keine Kopie außer Haus")
        if not any(item["versioned"] for item in copies):
            findings.append("keine Versionsablage")
        uncertain = [
            item
            for item in copies
            if (item.get("failure_domain") or {}).get("confidence") != "high"
        ]
        if uncertain:
            findings.append(
                f"{len(uncertain)} Speicherort-Zuordnung(en) nur abgeleitet oder unsicher"
            )
        never = [item for item in copies if item["last_success"] is None]
        if never:
            findings.append(f"{len(never)} Kopie(n) ohne je erfolgreichen Lauf")

        rows.append(
            {
                "source": entry["source"],
                "id": quote(entry["source"], safe=""),
                "copy_count": len(copies),
                "scope_count": len(domains),
                "scopes": scopes,
                "domains": domains,
                "domain_warnings": domain_warnings,
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
            "uncertain_domains": sum(
                1
                for row in rows
                for domain in row["domains"]
                if domain.get("confidence") != "high"
            ),
            "sources_with_uncertain_domains": sum(
                1 for row in rows if row["domain_warnings"]
            ),
        },
    }
