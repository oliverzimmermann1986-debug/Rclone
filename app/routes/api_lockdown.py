"""API: Notfall-Lockdown mit Passwortbestätigung."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, SecretStr

from ..auth import require_auth, require_reauthentication
from ..lockdown import activate_lockdown, lockdown_state, release_lockdown
from ..security import require_csrf

router = APIRouter(
    prefix="/api/lockdown",
    tags=["lockdown"],
    dependencies=[Depends(require_auth), Depends(require_csrf)],
)


class ReleaseRequest(BaseModel):
    password: SecretStr = Field(min_length=1, max_length=1024)


class LockdownRequest(ReleaseRequest):
    reason: str = Field(default="", max_length=300)


@router.get("")
def get_lockdown() -> dict[str, Any]:
    return lockdown_state()


@router.post("")
def start_lockdown(
    body: LockdownRequest, request: Request, user: str = Depends(require_auth)
) -> dict[str, Any]:
    require_reauthentication(request, user, body.password.get_secret_value())
    return activate_lockdown(actor=user, reason=body.reason)


@router.post("/release")
def end_lockdown(
    body: ReleaseRequest, request: Request, user: str = Depends(require_auth)
) -> dict[str, Any]:
    require_reauthentication(request, user, body.password.get_secret_value())
    return release_lockdown(actor=user)
