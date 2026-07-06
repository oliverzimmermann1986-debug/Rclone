"""Wartungs-APIs: Logs suchen, alte Logs löschen, Config-Backup exportieren."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from fastapi import APIRouter, Depends, HTTPException, Query, Response

from ..auth import require_auth
from ..config_store import get_config

router = APIRouter(prefix="/api/maintenance", tags=["maintenance"], dependencies=[Depends(require_auth)])


def _logs_root() -> Path:
    return Path(get_config().get("paths", "logs_dir", default="/opt/rclone-sync/logs")).resolve()


def _safe_log_path(path: Path) -> bool:
    root = _logs_root()
    try:
        return path.resolve().is_file() and str(path.resolve()).startswith(str(root))
    except Exception:
        return False


@router.get("/logs")
def list_logs(limit: int = 200, query: str = "") -> Dict[str, Any]:
    root = _logs_root()
    if not root.exists():
        return {"logs": [], "root": str(root)}
    files: List[Dict[str, Any]] = []
    q = query.lower().strip()
    for p in root.rglob("*.log"):
        if not p.is_file():
            continue
        rel = str(p.relative_to(root))
        if q and q not in rel.lower():
            continue
        st = p.stat()
        files.append({"path": rel, "size": st.st_size, "mtime": st.st_mtime})
    files.sort(key=lambda x: x["mtime"], reverse=True)
    return {"root": str(root), "logs": files[: max(1, min(limit, 1000))]}


@router.post("/logs/prune")
def prune_logs(days: int = Query(30, ge=1, le=3650), dry_run: bool = True) -> Dict[str, Any]:
    root = _logs_root()
    cutoff = time.time() - days * 86400
    candidates: List[Dict[str, Any]] = []
    deleted = 0
    bytes_deleted = 0
    if root.exists():
        for p in root.rglob("*.log"):
            if not p.is_file() or p.stat().st_mtime >= cutoff:
                continue
            rel = str(p.relative_to(root))
            size = p.stat().st_size
            candidates.append({"path": rel, "size": size, "mtime": p.stat().st_mtime})
            if not dry_run:
                try:
                    p.unlink()
                    deleted += 1
                    bytes_deleted += size
                except OSError:
                    pass
    return {"ok": True, "dry_run": dry_run, "days": days, "matched": len(candidates), "deleted": deleted, "bytes_deleted": bytes_deleted, "files": candidates[:200]}


@router.get("/config/export")
def export_config() -> Response:
    cfg = get_config()._data.copy()
    # Secret-Felder entfernen, Export ist bewusst für Doku/Review gedacht.
    web = dict(cfg.get("web") or {})
    for k in ("password", "password_hash", "secret_key"):
        if k in web:
            web[k] = "***REDACTED***"
    cfg["web"] = web
    body = yaml.dump(cfg, allow_unicode=True, sort_keys=False)
    return Response(body, media_type="text/yaml", headers={"Content-Disposition": "attachment; filename=rclone-sync-config-redacted.yaml"})
