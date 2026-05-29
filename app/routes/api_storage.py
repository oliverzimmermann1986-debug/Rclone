"""Storage-Übersicht pro Pair: lokaler Disk-Free + Remote-Size."""
from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, Depends

from ..auth import require_auth
from ..config_store import get_config
from ..db import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/storage", tags=["storage"], dependencies=[Depends(require_auth)])


def _disk_usage(path: str) -> Dict[str, Any]:
    """statvfs-Stats für einen Pfad. Liefert None-Werte wenn Pfad fehlt."""
    p = Path(path)
    if not p.exists():
        return {"path": path, "exists": False}
    try:
        u = shutil.disk_usage(str(p))
        return {
            "path": path, "exists": True,
            "total_bytes": u.total, "used_bytes": u.used, "free_bytes": u.free,
            "percent_used": round(u.used * 100.0 / u.total, 1) if u.total else 0,
        }
    except Exception as e:
        return {"path": path, "exists": True, "error": str(e)}


def _rclone_size(remote: str, timeout: int = 30) -> Dict[str, Any]:
    """rclone size --json — Cache-friendly via 1 process."""
    try:
        r = subprocess.run(
            ["rclone", "size", "--json", remote],
            capture_output=True, text=True, timeout=timeout,
        )
        if r.returncode == 0:
            import json
            d = json.loads(r.stdout)
            return {"remote": remote, "count": d.get("count"), "bytes": d.get("bytes")}
        return {"remote": remote, "error": r.stderr.strip()[:200]}
    except subprocess.TimeoutExpired:
        return {"remote": remote, "error": "Timeout"}
    except Exception as e:
        return {"remote": remote, "error": str(e)}


@router.get("/overview")
def overview(include_remote: bool = False) -> Dict[str, Any]:
    """Pro Pair: lokaler Disk-Free + optional Remote-Size.
    include_remote=true ist langsam (5-30s pro Pair je nach Cloud)."""
    cfg = get_config()
    db = get_db()
    pairs = cfg.get("backup", "pairs", default=[]) or []
    out = []
    for p in pairs:
        local = p.get("local", "")
        info: Dict[str, Any] = {
            "name": p.get("name"),
            "local": local, "remote": p.get("remote"),
            "schedule": p.get("schedule", ""),
            "local_disk": _disk_usage(local) if local and not local.endswith(":") else None,
        }
        # Letzter erfolgreicher Job für dieses Pair (aus jobs.summary)
        jobs = db.job_list(kind="backup", limit=20)
        for j in jobs:
            if not j.get("summary"):
                continue
            for pr in j["summary"].get("pairs", []):
                if pr.get("name") == p.get("name") and pr.get("ok"):
                    info["last_sync"] = j.get("ended_at")
                    info["last_transferred"] = pr.get("transferred", 0)
                    break
            if "last_sync" in info:
                break
        if include_remote:
            info["remote_size"] = _rclone_size(p.get("remote", ""))
        out.append(info)
    return {"pairs": out}
