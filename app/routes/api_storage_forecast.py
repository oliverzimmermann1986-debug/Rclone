"""Authenticated, read-only capacity forecasts and explicit bounded sampling."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, StrictInt

from ..auth import require_auth
from ..config_store import ConfigConflictError, get_config
from ..config_validation import ConfigValidationError, validate_config
from ..db import get_db
from ..security import require_csrf
from .. import storage_forecast

router = APIRouter(
    prefix="/api/storage/forecast",
    tags=["storage"],
    dependencies=[Depends(require_auth), Depends(require_csrf)],
)


class SampleRequest(BaseModel):
    identity: str | None = Field(default=None, max_length=200)


class CapacityRequest(BaseModel):
    identity: str = Field(min_length=1, max_length=200)
    capacity_bytes: StrictInt | None = Field(default=None, ge=1, le=2**60)
    expected_revision: str = Field(min_length=1, max_length=128)


def _current_forecast(store: Any) -> dict[str, Any]:
    snapshot, revision = store.snapshot_with_revision()
    return {**storage_forecast.forecasts(snapshot, get_db()), "_revision": revision}


@router.get("")
def forecast(identity: str | None = Query(None, max_length=200)) -> dict[str, Any]:
    result = _current_forecast(get_config())
    if identity:
        result["items"] = [
            item for item in result["items"] if item["identity"] == identity
        ]
        if not result["items"]:
            raise HTTPException(404, "Speicherziel nicht gefunden")
    return result


@router.get("/history")
def forecast_history(
    identity: str = Query(..., min_length=1, max_length=200),
) -> dict[str, Any]:
    try:
        snapshot, _revision = get_config().snapshot_with_revision()
        return storage_forecast.history(snapshot, get_db(), identity)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/sample")
def sample(request: SampleRequest | None = None) -> dict[str, Any]:
    try:
        snapshot, revision = get_config().snapshot_with_revision()
        result = storage_forecast.sample_targets(
            snapshot,
            get_db(),
            identity=request.identity if request else None,
            force=True,
        )
        return {**result, "_revision": revision}
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.put("/capacity")
def set_capacity(
    request: CapacityRequest, user: str = Depends(require_auth)
) -> dict[str, Any]:
    store = get_config()

    def update(config: dict[str, Any]) -> dict[str, Any]:
        target = next(
            (
                item
                for item in storage_forecast.targets(config)
                if item["identity"] == request.identity
            ),
            None,
        )
        if target is None:
            raise LookupError("Speicherziel nicht gefunden")
        if target["kind"] != "remote":
            raise ValueError("Eigene Kapazitäten sind nur für Remote-Ziele vorgesehen")
        options = config.setdefault("backup", {}).setdefault("storage_forecast", {})
        capacities = options.setdefault("capacities", {})
        if request.capacity_bytes is None:
            capacities.pop(request.identity, None)
        else:
            capacities[request.identity] = request.capacity_bytes
        return validate_config(config)[0]

    try:
        store.update(update, expected_revision=request.expected_revision)
    except ConfigConflictError as exc:
        raise HTTPException(
            409,
            {
                "message": "Konfiguration wurde parallel geändert",
                "reload_required": True,
                "current_revision": store.revision,
            },
        ) from exc
    except ConfigValidationError as exc:
        raise HTTPException(
            422, {"message": "Konfiguration ungültig", "errors": exc.errors}
        ) from exc
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    try:
        get_db().audit_add(
            "storage_capacity_updated",
            actor=user,
            details={
                "identity": request.identity,
                "capacity_bytes": request.capacity_bytes,
            },
        )
    except Exception:
        pass
    return {"ok": True, **_current_forecast(store)}
