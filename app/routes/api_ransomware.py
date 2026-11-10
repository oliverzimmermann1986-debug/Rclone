"""API-Endpunkte für Ransomware-Früherkennung und Canary-Dateien."""

from __future__ import annotations

from typing import Any, Mapping

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..auth import require_auth, require_reauthentication
from ..config_store import get_config
from ..db import get_db
from ..ransomware_guard import (
    check_canary,
    init_canary,
    ransomware_overview,
    ransomware_settings,
    scan_suspect_extensions,
)
from ..security import require_csrf

router = APIRouter(
    prefix="/api/security",
    tags=["security"],
    dependencies=[Depends(require_auth), Depends(require_csrf)],
)


class CanaryInitBody(BaseModel):
    filename: str | None = Field(default=None, max_length=255)
    current_password: str | None = Field(default=None, max_length=1024)


def _find_pair(
    snapshot: Mapping[str, Any], pair_name: str
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    raw_backup = snapshot.get("backup") if isinstance(snapshot, Mapping) else None
    backup = raw_backup if isinstance(raw_backup, Mapping) else {}
    raw_pairs = backup.get("pairs")
    if isinstance(raw_pairs, list):
        for item in raw_pairs:
            if isinstance(item, Mapping) and str(item.get("name") or "") == pair_name:
                return backup, item
    raise HTTPException(404, f"Pair nicht gefunden: {pair_name}")


@router.get("/ransomware-status")
def get_ransomware_status() -> dict[str, Any]:
    return ransomware_overview(get_db(), get_config().snapshot())


@router.post("/canary/{pair_name}/init")
def initialize_pair_canary(
    pair_name: str,
    request: Request,
    body: CanaryInitBody | None = Body(default=None),
    user: str = Depends(require_auth),
) -> dict[str, Any]:
    db = get_db()
    snapshot = get_config().snapshot()
    _backup, pair = _find_pair(snapshot, pair_name)

    existing = check_canary(pair, database=db)
    if existing.get("configured"):
        password = (body.current_password if body else None) or ""
        if not password:
            raise HTTPException(
                403,
                {
                    "message": "Passwortbestätigung erforderlich, um eine bestehende Canary-Datei zu überschreiben",
                    "reauth_required": True,
                },
            )
        require_reauthentication(request, user, password)

    try:
        result = init_canary(
            pair,
            database=db,
            filename=body.filename if body else None,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    db.audit_add(
        "canary_initialized",
        actor=user,
        details={
            "pair": pair_name,
            "canary_file": result["canary_file"],
            "canary_sha256": result["canary_sha256"],
        },
    )
    return result


@router.post("/canary/{pair_name}/verify")
def verify_pair_canary(pair_name: str) -> dict[str, Any]:
    db = get_db()
    snapshot = get_config().snapshot()
    backup, pair = _find_pair(snapshot, pair_name)
    settings = ransomware_settings(backup, pair)
    canary = check_canary(pair, database=db)
    extension_scan = scan_suspect_extensions(
        str(pair.get("local") or ""),
        suspect_extensions=settings["suspect_extensions"],
        max_suspect_files=settings["max_suspect_files"],
    )
    ok = bool(canary.get("ok", True)) and not bool(extension_scan.get("blocked", False))
    return {
        "pair": pair_name,
        "canary": canary,
        "extension_scan": extension_scan,
        "ok": ok,
    }
