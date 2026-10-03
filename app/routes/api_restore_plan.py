"""Revision-bound restore scheduling with an evidence-expiry preview."""

from __future__ import annotations

import copy
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from croniter import croniter
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from ..auth import require_auth
from ..config_store import ConfigConflictError, get_config
from ..config_validation import ConfigValidationError, validate_config
from ..db import get_db
from ..jobs.restore_test import restore_test_settings
from ..jobs.scheduler import next_run_after, restore_test_due
from ..restore_evidence import (
    MAX_EVIDENCE_AGE_SEC,
    evaluate_restore_evidence,
    restore_history_key,
)
from ..scheduler_control import scheduler_state
from ..security import require_csrf
from .api_config import _audit_best_effort

router = APIRouter(
    prefix="/api/recovery/restore-plan",
    tags=["recovery"],
    dependencies=[Depends(require_auth), Depends(require_csrf)],
)


class RestorePlanSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool
    schedule: str = Field(min_length=1, max_length=128)
    sample_files: int = Field(ge=1, le=500)
    max_total_mb: int = Field(ge=1, le=51_200)
    max_scan_files: int = Field(ge=100, le=1_000_000)


class RestorePlanUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    settings: RestorePlanSettings


def _draft(config: dict[str, Any], settings: RestorePlanSettings) -> dict[str, Any]:
    expression = settings.schedule.strip()
    manual = expression.casefold() in {"manual", "off", "disabled", "none"}
    if not manual and (
        len(expression.split()) != 5 or not croniter.is_valid(expression)
    ):
        raise HTTPException(422, "Zeitplan ist keine gültige 5-stellige Cron-Angabe")
    if settings.enabled and manual:
        raise HTTPException(422, "Für automatische Prüfungen einen Zeitplan wählen")
    if not manual:
        try:
            next_run_after(
                expression,
                timezone_name=str(
                    (config.get("backup") or {}).get("timezone") or "Europe/Berlin"
                ),
            )
        except (ValueError, OverflowError) as exc:
            raise HTTPException(
                422, "Zeitplan hat keinen erreichbaren Prüftermin"
            ) from exc
    draft = copy.deepcopy(config)
    draft.setdefault("backup", {})["restore_test"] = {
        **settings.model_dump(),
        "schedule": expression,
    }
    return draft


def build_plan(
    config: dict[str, Any], revision: str, database, *, now: float | None = None
) -> dict[str, Any]:
    current = time.time() if now is None else now
    settings = restore_test_settings(config)
    backup = config.get("backup") or {}
    timezone = str(backup.get("timezone") or "Europe/Berlin")
    active = settings["enabled"] and settings["schedule"].casefold() not in {
        "manual",
        "off",
        "disabled",
        "none",
    }
    runs: list[float] = []
    max_interval = 0.0
    schedule_error = False
    if active:
        try:
            if len(settings["schedule"].split()) != 5 or not croniter.is_valid(
                settings["schedule"]
            ):
                raise ValueError("Unsupported stored cron expression")
            cursor = current
            for _ in range(2):
                next_run = next_run_after(
                    settings["schedule"], after=cursor, timezone_name=timezone
                )
                if next_run is None:
                    raise ValueError("Stored schedule has no next occurrence")
                runs.append(next_run)
                cursor = next_run
            # Daily anchors inspect every long gap in the next year without
            # enumerating minute-by-minute bursts (e.g. every minute on day 1).
            # Ask for the predecessor of the next occurrence, strictly before it,
            # so an anchor exactly on a scheduled minute does not double a gap.
            for day in range(367):
                next_run = next_run_after(
                    settings["schedule"],
                    after=current + day * 86400,
                    timezone_name=timezone,
                )
                if next_run is None:
                    raise ValueError("Stored schedule has no next occurrence")
                previous = (
                    croniter(
                        settings["schedule"],
                        datetime.fromtimestamp(next_run - 1, tz=ZoneInfo(timezone)),
                    )
                    .get_prev(datetime)
                    .timestamp()
                )
                max_interval = max(max_interval, next_run - previous)
        except (ValueError, OverflowError):
            # A legacy config can contain syntax that its older validator
            # accepted. Keep its settings visible so the user can repair it.
            schedule_error = True
            runs = []
            max_interval = 0.0
    pairs = [pair for pair in backup.get("pairs", []) if pair.get("enabled", True)]
    identities = {restore_history_key(pair): str(pair["name"]) for pair in pairs}
    legacy = {f"restore:{name}": name for name in identities.values()}
    histories = database.pair_last_history({**identities, **legacy}) if pairs else {}
    control = scheduler_state(database, now=current, cleanup_expired=False)
    automatik = (
        bool(backup.get("enabled", True))
        and active
        and not schedule_error
        and not control["paused"]
    )
    paths = []
    for pair in pairs:
        history = histories.get(restore_history_key(pair)) or {}
        if not history.get("last_result") and not history.get("last_success"):
            history = histories.get(f"restore:{pair['name']}") or {}
        evidence = evaluate_restore_evidence(pair, history, now=current)
        valid_until = evidence.get("valid_until")
        gap = not (
            automatik and evidence["valid"] and runs and runs[0] < (valid_until or 0)
        )
        paths.append(
            {
                "id": str(pair.get("id") or pair["name"]),
                "name": str(pair["name"]),
                "evidence_state": evidence["state"],
                "valid_until": valid_until,
                "coverage_gap": gap,
            }
        )
    warnings = []
    if schedule_error:
        warnings.append(
            "Der gespeicherte Prüfzeitplan ist ungültig oder hat keinen erreichbaren "
            "Prüftermin. Einen gültigen Zeitplan wählen und den Plan übernehmen."
        )
    elif not automatik:
        warnings.append(
            "Automatische Prüfungen sind deaktiviert oder durch ein Wartungsfenster pausiert."
        )
    if max_interval >= MAX_EVIDENCE_AGE_SEC:
        warnings.append(
            "Der Abstand zwischen Prüfterminen erreicht oder überschreitet die "
            "siebentägige Gültigkeit. Laufzeit und Zeitumstellung brauchen Reserve; "
            "zweimal wöchentlich prüfen."
        )
    if any(path["coverage_gap"] for path in paths):
        warnings.append(
            "Mindestens ein Datenweg hat keinen aktuellen Nachweis oder eine "
            "Lücke bis zur nächsten geplanten Prüfung."
        )
    if not paths:
        warnings.append("Es sind keine aktiven Datenwege eingerichtet.")
    due = (
        {"due": False}
        if schedule_error
        else restore_test_due(config, database, now=current)
    )
    return {
        "ok": True,
        "revision": revision,
        "settings": settings,
        "timezone": timezone,
        "generated_at": current,
        "next_runs": runs,
        "max_interval_seconds": max_interval or None,
        "due_now": bool(due.get("due") and automatik),
        "scheduler_paused": control["paused"],
        "data_paths": paths,
        "warnings": warnings,
    }


@router.get("")
def get_plan() -> dict[str, Any]:
    config, revision = get_config().snapshot_with_revision()
    return build_plan(config, revision, get_db())


@router.post("/preview")
def preview_plan(body: RestorePlanUpdate) -> dict[str, Any]:
    config, revision = get_config().snapshot_with_revision()
    if body.revision != revision:
        raise HTTPException(
            409, "Konfiguration wurde parallel geändert. Plan neu laden."
        )
    return build_plan(_draft(config, body.settings), revision, get_db())


@router.put("")
def save_plan(
    body: RestorePlanUpdate, user: str = Depends(require_auth)
) -> dict[str, Any]:
    store = get_config()
    config, revision = store.snapshot_with_revision()
    if body.revision != revision:
        raise HTTPException(
            409, "Konfiguration wurde parallel geändert. Plan neu laden."
        )
    try:
        draft = _draft(config, body.settings)
        normalized, _ = validate_config(draft)
        # Validate against the complete config, but persist only the requested
        # section. Adding unrelated defaults here would overwrite user intent.
        draft["backup"]["restore_test"] = normalized["backup"]["restore_test"]
        revision = store.replace(draft, expected_revision=body.revision)
    except ConfigConflictError as exc:
        raise HTTPException(
            409, "Konfiguration wurde parallel geändert. Plan neu laden."
        ) from exc
    except ConfigValidationError as exc:
        raise HTTPException(
            422, {"message": "Plan ungültig", "errors": exc.errors}
        ) from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(500, "Prüfplan konnte nicht gespeichert werden") from exc
    result = build_plan(draft, revision, get_db())
    if not _audit_best_effort(
        "restore_plan_saved",
        actor=user,
        details={"revision": revision, "settings": body.settings.model_dump()},
    ):
        result["warnings"].append(
            "Plan gespeichert; Audit-Protokoll war nicht verfügbar."
        )
    return result
