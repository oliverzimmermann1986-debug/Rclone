"""Authenticated preview, background copy and separately confirmed target cutover."""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .. import target_migration as migration
from ..auth import account_generation_guard, require_auth, require_reauthentication
from ..config_store import ConfigConflictError, get_config
from ..db import JobAlreadyRunningError, get_db
from ..security import require_csrf

router = APIRouter(
    prefix="/api/migrations",
    tags=["migrations"],
    dependencies=[Depends(require_auth), Depends(require_csrf)],
)


class PreviewRequest(BaseModel):
    identity: str = Field(min_length=1, max_length=128)
    target: str = Field(min_length=1, max_length=1024)
    expected_revision: str = Field(min_length=1, max_length=128)
    max_total_mb: int = Field(default=5120, ge=1, le=51_200)


class PasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024)


class SwitchRequest(PasswordRequest):
    expected_revision: str = Field(min_length=1, max_length=128)


def _error(exc):
    if isinstance(exc, migration.MigrationLocked):
        return HTTPException(423, str(exc))
    status = (
        409
        if isinstance(
            exc, (ConfigConflictError, migration.MigrationBusy, JobAlreadyRunningError)
        )
        else 400
    )
    return HTTPException(status, str(exc))


@router.get("")
def migrations():
    migration.recover_cutovers(get_config(), get_db())
    return {"items": migration.list_plans(get_db())}


@router.post("/preview")
def preview_migration(body: PreviewRequest):
    try:
        return migration.preview(get_config(), get_db(), **body.model_dump())
    except (ValueError, ConfigConflictError) as exc:
        raise _error(exc) from exc


@router.get("/{plan_id}")
def migration_plan(plan_id: str):
    try:
        migration.recover_cutovers(get_config(), get_db())
        return migration.get_plan(get_db(), plan_id)
    except migration.MigrationError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/{plan_id}/run", status_code=202)
def run_migration(
    plan_id: str,
    body: PasswordRequest,
    request: Request,
    user: str = Depends(require_auth),
):
    generation = require_reauthentication(request, user, body.current_password)
    try:
        with account_generation_guard(generation):
            return migration.start(get_config(), get_db(), plan_id)
    except (ValueError, ConfigConflictError, JobAlreadyRunningError) as exc:
        raise _error(exc) from exc


@router.post("/{plan_id}/switch", status_code=202)
def switch_migration(
    plan_id: str,
    body: SwitchRequest,
    request: Request,
    user: str = Depends(require_auth),
):
    generation = require_reauthentication(request, user, body.current_password)
    try:
        with account_generation_guard(generation):
            return migration.start_switch(
                get_config(),
                get_db(),
                plan_id,
                expected_revision=body.expected_revision,
                expected_generation=generation,
            )
    except (ValueError, ConfigConflictError, JobAlreadyRunningError) as exc:
        raise _error(exc) from exc
