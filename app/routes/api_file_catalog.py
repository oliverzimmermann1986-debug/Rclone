"""Authenticated metadata-only file search with existing selective restore links."""

from fastapi import APIRouter, Depends, HTTPException, Query

from ..auth import require_auth
from ..config_store import get_config
from ..file_catalog import CatalogCursorError, search_files
from ..security import require_csrf

router = APIRouter(
    prefix="/api/recovery/files",
    tags=["recovery"],
    dependencies=[Depends(require_auth), Depends(require_csrf)],
)


@router.get("/search")
def file_search(
    query: str = Query(min_length=2, max_length=120),
    identity: str = Query(default="", max_length=128),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str = Query(default="", max_length=4096),
):
    snapshot, revision = get_config().snapshot_with_revision()
    try:
        return {
            **search_files(
                snapshot,
                query,
                identity=identity,
                limit=limit,
                cursor=cursor,
                revision=revision,
            ),
            "config_revision": revision,
        }
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except CatalogCursorError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
