"""API: Bisync-Konflikte je Datenweg anzeigen und auflösen."""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, SecretStr

from .. import conflicts
from ..auth import require_auth, require_reauthentication
from ..config_store import get_config
from ..db import get_db
from ..jobs import runtime_state
from ..jobs.locks import try_file_lock
from ..lockdown import is_locked_down
from ..security import require_csrf

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/api/pairs",
    tags=["conflicts"],
    dependencies=[Depends(require_auth), Depends(require_csrf)],
)

_BUSY = "Während eines Backups können Konflikte nicht aufgelöst werden"


class ResolveBody(BaseModel):
    base_path: str = Field(min_length=1, max_length=4096)
    choice: Literal["keep_side1", "keep_side2", "keep_both"]
    password: Optional[SecretStr] = Field(default=None, max_length=1024)


def _bisync_pair(name: str) -> dict[str, Any]:
    backup = get_config().snapshot().get("backup") or {}
    pair = next(
        (
            p
            for p in backup.get("pairs") or []
            if isinstance(p, dict) and str(p.get("name") or "") == name
        ),
        None,
    )
    if pair is None:
        raise HTTPException(404, "Datenweg nicht gefunden")
    if str(pair.get("direction") or "bisync").lower().strip() != "bisync":
        raise HTTPException(400, "Konflikte gibt es nur bei bisync-Datenwegen")
    return pair


@router.get("/{name}/conflicts")
def list_pair_conflicts(name: str) -> dict[str, Any]:
    pair = _bisync_pair(name)
    try:
        return conflicts.list_conflicts(pair)
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc


@router.post("/{name}/conflicts/resolve")
def resolve_pair_conflict(
    name: str,
    body: ResolveBody,
    request: Request,
    user: str = Depends(require_auth),
) -> dict[str, Any]:
    if is_locked_down():
        raise HTTPException(423, "Lockdown aktiv")
    pair = _bisync_pair(name)
    if body.choice != "keep_both":  # löscht Konfliktkopien
        password = body.password.get_secret_value() if body.password else ""
        if not password:
            raise HTTPException(
                403,
                {
                    "message": "Zum Löschen von Konfliktkopien ist das aktuelle "
                    "Passwort nötig",
                    "reauth_required": True,
                },
            )
        require_reauthentication(request, user, password)
    # Der Backup-Lock verhindert, dass ein Sync mitten in die Schritte startet.
    lease = try_file_lock(runtime_state.DEFAULT_CANCEL_SCOPE)
    if lease is None:
        raise HTTPException(409, _BUSY)
    try:
        if get_db().job_running("backup"):
            raise HTTPException(409, _BUSY)
        result = conflicts.resolve_conflict(pair, body.base_path, body.choice)
    except conflicts.OriginUnclear as exc:
        raise HTTPException(409, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc
    finally:
        lease.release()
    try:
        get_db().audit_add(
            "conflict_resolved",
            actor="web",
            details={
                "pair": name,
                "base_path_sha256": hashlib.sha256(
                    body.base_path.encode("utf-8")
                ).hexdigest()[:16],
                "choice": body.choice,
                "steps": len(result["steps"]),
                "completed": result["completed"],
            },
        )
    except Exception:
        logger.exception("Audit-Ereignis conflict_resolved nicht gespeichert")
    return result
