"""Password-confirmed operational integrations; no secret-bearing reads."""

from __future__ import annotations

import copy
import secrets
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .. import heartbeat, protection_reports
from ..auth import account_generation, require_auth, require_reauthentication
from ..config_store import ConfigConflictError, get_config
from ..config_validation import ConfigValidationError, validate_config
from ..db import get_db
from ..lockdown import is_locked_down
from ..security import require_csrf
from .api_config import _audit_best_effort

router = APIRouter(
    prefix="/api/operations",
    tags=["operations"],
    dependencies=[Depends(require_auth), Depends(require_csrf)],
)
_HEARTBEAT_FIELDS = {
    "enabled",
    "secret_url",
    "period_seconds",
    "timeout_seconds",
    "allow_private_hosts",
}
_WEEKLY_FIELDS = {"enabled", "schedule", "timezone", "retention_weeks", "email"}
_EMAIL_FIELDS = {
    "enabled",
    "host",
    "port",
    "encryption",
    "username",
    "password",
    "from_address",
    "to_addresses",
    "timeout_seconds",
}


class OperationsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: str = Field(min_length=1, max_length=64)
    current_password: str = Field(min_length=1, max_length=1024)
    heartbeat: dict[str, Any] | None = None
    weekly_report: dict[str, Any] | None = None


class PasswordConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_password: str = Field(min_length=1, max_length=1024)


def safe_settings(cfg: Any, *, revision: str | None = None) -> dict[str, Any]:
    raw_hb = heartbeat.settings(cfg)
    hb = {key: raw_hb[key] for key in _HEARTBEAT_FIELDS - {"secret_url"}}
    hb["url_configured"] = bool(raw_hb.get("secret_url"))
    raw_weekly = protection_reports.settings(cfg)
    weekly = {key: raw_weekly[key] for key in _WEEKLY_FIELDS - {"email"}}
    weekly["email"] = {
        key: raw_weekly["email"][key] for key in _EMAIL_FIELDS - {"password"}
    }
    weekly["email"]["password_set"] = bool(raw_weekly["email"].get("password"))
    return {
        "_revision": revision or cfg.revision,
        "heartbeat": hb,
        "weekly_report": weekly,
        "capabilities": {"pdf": True, "email": True, "heartbeat": True},
    }


@router.get("/settings")
def operations_settings() -> dict[str, Any]:
    cfg = get_config()
    data, revision = cfg.snapshot_with_revision()
    return safe_settings(data, revision=revision)


def _patch(
    destination: dict[str, Any],
    updates: dict[str, Any],
    allowed: set[str],
    *,
    ignored: set[str] | None = None,
) -> None:
    ignored = ignored or set()
    if updates.keys() - allowed - ignored:
        raise ValueError("Unbekanntes Feld in den Betriebseinstellungen.")
    for key, value in updates.items():
        if key in ignored:
            continue
        if key in {"secret_url", "password"} and value == "***SET***":
            continue
        destination[key] = copy.deepcopy(value)


@router.put("/settings")
def update_operations_settings(
    body: OperationsUpdate, request: Request, user: str = Depends(require_auth)
) -> dict[str, Any]:
    cfg = get_config()
    if cfg.revision != body.expected_revision:
        raise HTTPException(
            409, "Betriebseinstellungen wurden zwischenzeitlich geändert."
        )
    generation = require_reauthentication(request, user, body.current_password)

    def update(data: dict[str, Any]) -> dict[str, Any]:
        if not secrets.compare_digest(account_generation(data), generation):
            raise HTTPException(409, "Anmeldung wurde geändert. Bitte erneut anmelden.")
        if body.heartbeat is not None:
            hb = data.setdefault("notifications", {}).setdefault("heartbeat", {})
            _patch(hb, body.heartbeat, _HEARTBEAT_FIELDS, ignored={"url_configured"})
        if body.weekly_report is not None:
            weekly = data.setdefault("reports", {}).setdefault("weekly", {})
            updates = copy.deepcopy(body.weekly_report)
            email = updates.pop("email", None)
            _patch(weekly, updates, _WEEKLY_FIELDS)
            if email is not None:
                if not isinstance(email, dict):
                    raise ValueError("E-Mail-Einstellungen müssen ein Objekt sein.")
                _patch(
                    weekly.setdefault("email", {}),
                    email,
                    _EMAIL_FIELDS,
                    ignored={"password_set"},
                )
        normalized, _warnings = validate_config(data)
        hb = heartbeat.settings(normalized)
        if hb.get("secret_url"):
            heartbeat.parse_receiver(
                hb["secret_url"], allow_private_hosts=hb["allow_private_hosts"] is True
            )
        email = protection_reports.settings(normalized)["email"]
        if email["enabled"]:
            protection_reports.validate_email(email)
        return normalized

    try:
        cfg.update(update, expected_revision=body.expected_revision)
    except ConfigConflictError:
        raise HTTPException(
            409, "Betriebseinstellungen wurden zwischenzeitlich geändert."
        ) from None
    except ConfigValidationError:
        # Central validation may include the invalid value. Never echo secrets.
        raise HTTPException(
            422,
            "Betriebseinstellungen sind ungültig. Bitte Adressen, Zeitplan und Grenzen prüfen.",
        ) from None
    except ValueError:
        raise HTTPException(
            422,
            "Betriebseinstellungen sind ungültig. Bitte Adressen, Zeitplan und Grenzen prüfen.",
        ) from None
    _audit_best_effort(
        "operations_settings_saved",
        actor=user,
        details={
            "heartbeat_changed": body.heartbeat is not None,
            "weekly_report_changed": body.weekly_report is not None,
        },
    )
    return {"ok": True, **operations_settings()}


@router.get("/heartbeat")
def heartbeat_status() -> dict[str, Any]:
    return heartbeat.status(get_config(), get_db())


@router.post("/heartbeat/test")
def test_heartbeat(
    body: PasswordConfirmation, request: Request, user: str = Depends(require_auth)
) -> dict[str, Any]:
    generation = require_reauthentication(request, user, body.current_password)
    cfg, db = get_config(), get_db()
    if is_locked_down(db):
        raise HTTPException(423, "Notfall-Lockdown sperrt die Heartbeat-Probe.")
    if not heartbeat.settings(cfg).get("secret_url"):
        raise HTTPException(400, "Zuerst eine geheime Heartbeat-URL speichern.")
    try:
        with cfg.locked_snapshot() as data:
            if not secrets.compare_digest(account_generation(data), generation):
                raise HTTPException(409, "Anmeldung wurde zwischenzeitlich geändert.")
            result = heartbeat.tick(data, db, test=True)
    except heartbeat.HeartbeatRateLimitError as exc:
        raise HTTPException(
            429, str(exc), headers={"Retry-After": str(exc.retry_after)}
        ) from None
    except ValueError:
        raise HTTPException(
            422, "Heartbeat-Empfängeradresse ist nicht freigegeben."
        ) from None
    _audit_best_effort("heartbeat_test", actor=user, details={"ok": result["ok"]})
    return result
