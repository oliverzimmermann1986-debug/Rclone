"""Native device-vault API for photos and Files imports."""

from __future__ import annotations

from typing import Any, Mapping, Literal

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    Request,
)
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..auth import require_auth
from ..config_store import get_config
from ..db import get_db
from ..device_vault import (
    VaultError,
    append_chunk,
    complete_upload,
    create_upload,
    download_blob,
    library,
    queue_completion,
    upload_status,
)
from ..jobs.restore_test import _endpoints
from ..security import require_csrf

router = APIRouter(
    prefix="/api/vault",
    tags=["device-vault"],
    dependencies=[Depends(require_auth), Depends(require_csrf)],
)


def _pairs(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    backup = config.get("backup") if isinstance(config.get("backup"), Mapping) else {}
    return [
        dict(item)
        for item in (backup or {}).get("pairs", [])
        if isinstance(item, Mapping)
    ]


def _find_pair(config: Mapping[str, Any], identity: str) -> dict[str, Any]:
    needle = str(identity or "").strip()
    for pair in _pairs(config):
        if needle in {str(pair.get("id") or ""), str(pair.get("name") or "")}:
            return pair
    raise HTTPException(404, "Datenweg nicht gefunden")


def _safe(callable_, *args, **kwargs):
    try:
        return callable_(*args, **kwargs)
    except VaultError as exc:
        message = str(exc)
        status = 404 if "nicht gefunden" in message.lower() else 409
        raise HTTPException(status, message) from exc


class CreateUploadRequest(BaseModel):
    identity: str = Field(min_length=1, max_length=128)
    filename: str = Field(min_length=1, max_length=240)
    size: int = Field(ge=1, le=50 * 1024 * 1024 * 1024)
    sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    source_type: Literal["photo", "file"]
    device_name: str = Field(default="iPhone", min_length=1, max_length=80)


@router.post("/uploads", status_code=201)
def start_upload(body: CreateUploadRequest) -> dict[str, Any]:
    config = get_config().snapshot()
    pair = _find_pair(config, body.identity)
    _source, target = _endpoints(pair)
    result = _safe(
        create_upload,
        config,
        pair=pair,
        filename=body.filename,
        size=body.size,
        sha256=body.sha256,
        source_type=body.source_type,
        device_name=body.device_name,
        target_root=target,
    )
    get_db().audit_add(
        "device_vault_upload_started",
        actor="ios",
        details={
            "id": result["id"],
            "pair": result["pair"],
            "filename": result["filename"],
            "size": result["size"],
            "deduplicated": result["deduplicated"],
        },
    )
    return result


@router.put("/uploads/{upload_id}")
async def upload_chunk(
    upload_id: str,
    request: Request,
    offset: int = Query(ge=0),
) -> dict[str, Any]:
    payload = await request.body()
    return _safe(
        append_chunk,
        get_config().snapshot(),
        upload_id,
        offset=offset,
        payload=payload,
    )


@router.post("/uploads/{upload_id}/complete", status_code=202)
def finish_upload(upload_id: str, background: BackgroundTasks) -> dict[str, Any]:
    config = get_config().snapshot()
    result = _safe(queue_completion, config, upload_id)
    if result.get("status") in {"queued", "transferring"}:
        background.add_task(complete_upload, get_db(), config, upload_id)
    return result


@router.get("/uploads/{upload_id}")
def get_upload_status(upload_id: str) -> dict[str, Any]:
    return _safe(upload_status, get_config().snapshot(), upload_id)


@router.get("/library")
def get_library(
    identity: str | None = Query(default=None, max_length=128),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    return {"items": library(get_config().snapshot(), identity=identity, limit=limit)}


@router.get("/library/{upload_id}/download")
def download_vault_item(upload_id: str) -> FileResponse:
    try:
        path, filename = download_blob(get_config().snapshot(), upload_id)
    except VaultError as exc:
        message = str(exc)
        status = (
            404
            if "fehlt" in message.lower() or "nicht gefunden" in message.lower()
            else 409
        )
        raise HTTPException(status, message) from exc
    get_db().audit_add(
        "device_vault_restored_to_iphone",
        actor="ios",
        details={"id": upload_id, "filename": filename},
    )
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=filename,
        headers={"Cache-Control": "no-store"},
    )


__all__ = ["router"]
