"""Storage-Übersicht pro Pair: lokaler freier Platz und optionale Remote-Größe."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends

from ..auth import require_auth
from ..config_store import get_config
from ..db import get_db
from ..jobs.rclone_sync import _is_remote
from ..rclone_args import rclone_subprocess_env
from ..security import require_csrf

router = APIRouter(
    prefix="/api/storage",
    tags=["storage"],
    dependencies=[Depends(require_auth), Depends(require_csrf)],
)


def _disk_usage(path: str) -> dict[str, Any]:
    target = Path(path)
    if not target.exists():
        return {"path": path, "exists": False}
    try:
        usage = shutil.disk_usage(str(target))
        return {
            "path": path,
            "exists": True,
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "percent_used": round(usage.used * 100.0 / usage.total, 1)
            if usage.total
            else 0,
        }
    except OSError as exc:
        return {"path": path, "exists": True, "error": str(exc)}


def _rclone_size(remote: str, timeout: int = 45) -> dict[str, Any]:
    if not remote:
        return {"remote": remote, "error": "Remote-Pfad fehlt"}
    cache_dir = os.getenv("RCLONE_CACHE_DIR", "/opt/rclone-sync/data/.rclone-cache")
    try:
        result = subprocess.run(
            ["rclone", "size", "--json", "--cache-dir", cache_dir, "--", remote],
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            env=rclone_subprocess_env(),
        )
        if result.returncode == 0:
            data = json.loads(result.stdout or "{}")
            return {
                "remote": remote,
                "count": data.get("count"),
                "bytes": data.get("bytes"),
            }
        return {
            "remote": remote,
            "error": (result.stderr or result.stdout).strip()[:300],
        }
    except subprocess.TimeoutExpired:
        return {"remote": remote, "error": "Timeout"}
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"remote": remote, "error": str(exc)}


def _last_success_by_pair() -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for name, result in get_db().pair_last_successes().items():
        pair = result.get("pair") or {}
        found[name] = {
            "last_sync": result.get("ended_at"),
            "last_transferred": pair.get("transferred", 0),
        }
    return found


@router.get("/overview")
def overview(include_remote: bool = False) -> dict[str, Any]:
    pairs = get_config().get("backup", "pairs", default=[]) or []
    last_success = _last_success_by_pair()
    output: list[dict[str, Any]] = []
    for pair in pairs:
        local = str(pair.get("local") or "")
        name = str(pair.get("name") or "")
        info: dict[str, Any] = {
            "name": name,
            "local": local,
            "remote": pair.get("remote"),
            "schedule": pair.get("schedule", ""),
            "local_disk": _disk_usage(local)
            if local and not _is_remote(local)
            else None,
            **last_success.get(name, {}),
        }
        output.append(info)

    if include_remote and output:
        workers = min(4, max(1, len(output)))
        with ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="remote-size"
        ) as pool:
            futures = {
                pool.submit(_rclone_size, str(item.get("remote") or "")): index
                for index, item in enumerate(output)
            }
            for future in as_completed(futures):
                index = futures[future]
                try:
                    output[index]["remote_size"] = future.result()
                except Exception as exc:
                    output[index]["remote_size"] = {
                        "remote": output[index].get("remote"),
                        "error": str(exc),
                    }
    return {"pairs": output}
