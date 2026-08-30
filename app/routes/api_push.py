"""Authenticated native device registration and APNs diagnostics."""

from __future__ import annotations

import re
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..auth import require_auth
from ..config_store import get_config
from ..db import get_db
from ..push_notifications import send_push_notifications
from ..security import require_csrf

router = APIRouter(
    prefix="/api/push",
    tags=["push"],
    dependencies=[Depends(require_auth), Depends(require_csrf)],
)

_DEVICE_TOKEN_RE = re.compile(r"^[a-f0-9]{64,512}$")


class PushDeviceRegistration(BaseModel):
    token: str = Field(min_length=64, max_length=512)
    environment: Literal["sandbox", "production"] = "production"
    app_version: str = Field(default="", max_length=40)


class PushDeviceRemoval(BaseModel):
    token: str = Field(min_length=64, max_length=512)


def _token(value: str) -> str:
    normalized = value.strip().lower()
    if not _DEVICE_TOKEN_RE.fullmatch(normalized):
        raise HTTPException(status_code=422, detail="Ungültiger APNs-Gerätetoken")
    return normalized


@router.get("/status")
def push_status() -> dict[str, object]:
    apns = get_config().get("notifications", "apns", default={}) or {}
    configured = bool(
        isinstance(apns, dict)
        and apns.get("enabled") is True
        and apns.get("team_id")
        and apns.get("key_id")
        and apns.get("key_file")
        and apns.get("topic")
    )
    database = get_db()
    devices = database.push_devices(limit=128)
    outbox = database.push_outbox_status()
    return {
        "configured": configured,
        "registered_devices": len(devices),
        "events": list(apns.get("events") or []) if isinstance(apns, dict) else [],
        "device_lease_days": (
            int(apns.get("device_lease_days") or 7) if isinstance(apns, dict) else 7
        ),
        "outbox": outbox,
    }


@router.post("/devices")
def register_push_device(
    body: PushDeviceRegistration,
    user: str = Depends(require_auth),
) -> dict[str, object]:
    token = _token(body.token)
    database = get_db()
    database.push_device_upsert(
        token,
        body.environment,
        app_version=body.app_version.strip(),
        lease_seconds=int(
            (
                get_config().get(
                    "notifications", "apns", "device_lease_days", default=7
                )
                or 7
            )
            * 86400
        ),
    )
    database.audit_add(
        "push_device_registered",
        actor=user,
        details={
            "environment": body.environment,
            "app_version": body.app_version.strip(),
        },
    )
    return {"ok": True, "registered": True}


@router.delete("/devices")
def unregister_push_device(body: PushDeviceRemoval) -> dict[str, object]:
    removed = get_db().push_device_delete(_token(body.token))
    return {"ok": True, "removed": removed}


@router.post("/test")
def test_push_notification() -> dict[str, object]:
    try:
        result = send_push_notifications(
            "sync_error",
            "Sicherpfad Test",
            "Fehler-Pushs sind auf diesem iPhone eingerichtet.",
            dedupe_key=f"push-test:{uuid.uuid4().hex}",
        )
    except (OSError, ValueError, PermissionError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if result["sent"] < 1:
        raise HTTPException(
            status_code=503,
            detail="Kein Push zugestellt. APNs-Konfiguration und Gerätetoken prüfen.",
        )
    return {"ok": True, **result}


__all__ = ["router"]
