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


def _resolve_endpoints(pair: dict[str, Any]) -> tuple[str, str]:
    """Löst Quelle/Ziel eines Pairs anhand der Richtung auf (wie die GUI-Anzeige).

    pull: Remote -> lokal (Quelle=Remote, Ziel=lokal). push/bisync: lokal -> Remote.
    """
    local = str(pair.get("local") or "")
    remote = str(pair.get("remote") or "")
    if str(pair.get("direction") or "").lower() == "pull":
        return remote, local
    return local, remote


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
        source, target = _resolve_endpoints(pair)
        info: dict[str, Any] = {
            "name": name,
            "local": local,
            "remote": pair.get("remote"),
            "direction": pair.get("direction", ""),
            "source": source,
            "target": target,
            "schedule": pair.get("schedule", ""),
            "local_disk": _disk_usage(local)
            if local and not _is_remote(local)
            else None,
            **last_success.get(name, {}),
        }
        output.append(info)

    # Größen für Quelle UND Ziel jedes Pairs sind teuer (rclone size traversiert
    # beide Endpunkte). Daher nur auf ausdrückliche Anforderung und parallelisiert.
    if include_remote and output:
        tasks: list[tuple[int, str]] = []
        for index, item in enumerate(output):
            tasks.append((index, "source"))
            tasks.append((index, "target"))
        workers = min(6, max(1, len(tasks)))
        with ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="pair-size"
        ) as pool:
            futures = {
                pool.submit(_rclone_size, str(output[index].get(side) or "")): (
                    index,
                    side,
                )
                for index, side in tasks
            }
            for future in as_completed(futures):
                index, side = futures[future]
                key = f"{side}_size"
                try:
                    output[index][key] = future.result()
                except Exception as exc:
                    output[index][key] = {
                        "path": output[index].get(side),
                        "error": str(exc),
                    }
    return {"pairs": output}
