"""API: GFS-Ausdünnung der --backup-dir-Versionsordner eines Datenwegs."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, SecretStr

from .. import versions_lifecycle
from ..auth import require_auth, require_reauthentication
from ..config_store import get_config
from ..db import get_db
from ..jobs import runtime_state
from ..jobs.locks import try_file_lock
from ..lockdown import is_locked_down
from ..security import require_csrf

router = APIRouter(
    prefix="/api/jobs/versions",
    tags=["versions"],
    dependencies=[Depends(require_auth), Depends(require_csrf)],
)


def _find_pair(config: dict[str, Any], name: str) -> dict[str, Any]:
    for pair in (config.get("backup") or {}).get("pairs") or []:
        if isinstance(pair, dict) and str(pair.get("name") or "") == name:
            return pair
    raise HTTPException(404, "Datenweg nicht gefunden")


@router.get("/preview")
def preview_versions(pair: str = Query(min_length=1, max_length=200)) -> dict[str, Any]:
    config = get_config().snapshot()
    return versions_lifecycle.apply_lifecycle(
        config, _find_pair(config, pair), dry_run=True
    )


class PruneBody(BaseModel):
    pair: str = Field(min_length=1, max_length=200)
    password: SecretStr = Field(min_length=1, max_length=1024)


@router.post("/prune")
def prune_versions(
    body: PruneBody, request: Request, user: str = Depends(require_auth)
) -> dict[str, Any]:
    if is_locked_down():
        raise HTTPException(423, "Lockdown aktiv")
    require_reauthentication(request, user, body.password.get_secret_value())
    config = get_config().snapshot()
    pair = _find_pair(config, body.pair)
    db = get_db()
    busy = "Ein Backup läuft gerade; bitte danach erneut ausdünnen"
    # Backup-Lock wie bei der Konfliktauflösung: kein Sync startet mitten hinein.
    lease = try_file_lock(runtime_state.DEFAULT_CANCEL_SCOPE)
    if lease is None:
        raise HTTPException(409, busy)
    try:
        if db.job_running("backup"):
            raise HTTPException(409, busy)
        result = versions_lifecycle.apply_lifecycle(config, pair, dry_run=False)
    finally:
        lease.release()
    roots = result.get("roots") or []
    db.audit_add(
        "versions_pruned",
        actor=user,
        details={
            "pair": body.pair,
            "purged": sum(len(root["purged"]) for root in roots),
            "failed": sum(len(root["failed"]) for root in roots),
        },
    )
    return result
