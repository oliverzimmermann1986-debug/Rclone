"""API: Preflight-Matrix eines Dry-Run-Laufs."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from ..auth import require_auth
from ..preflight_matrix import job_matrix
from ..security import require_csrf

router = APIRouter(
    prefix="/api/jobs",
    tags=["jobs"],
    dependencies=[Depends(require_auth), Depends(require_csrf)],
)


@router.get("/{job_id}/preflight")
def job_preflight(job_id: int) -> dict[str, Any]:
    try:
        return job_matrix(job_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
