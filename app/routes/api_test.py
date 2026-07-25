"""rclone-Test: konfigurierte Remotes und optional ein Pair prüfen."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..auth import require_auth
from ..config_store import get_config
from ..config_validation import ConfigValidationError, validate_config
from ..jobs.rclone_sync import _is_remote
from ..rclone_args import rclone_subprocess_env
from ..security import require_csrf

router = APIRouter(
    prefix="/api/test",
    tags=["test"],
    dependencies=[Depends(require_auth), Depends(require_csrf)],
)


class RcloneTest(BaseModel):
    pair_index: Optional[int] = Field(default=None, ge=0, le=10000)
    pair: Optional[dict[str, Any]] = None


@router.post("/rclone")
def test_rclone(request: RcloneTest) -> dict[str, Any]:
    try:
        remotes_result = subprocess.run(
            ["rclone", "listremotes"],
            capture_output=True,
            text=True,
            timeout=15,
            stdin=subprocess.DEVNULL,
            env=rclone_subprocess_env(),
        )
        if remotes_result.returncode != 0:
            return {
                "ok": False,
                "error": f"listremotes: {remotes_result.stderr.strip()[:300]}",
            }
        remotes = sorted(
            {
                line.strip().rstrip(":")
                for line in remotes_result.stdout.splitlines()
                if line.strip()
            },
            key=str.casefold,
        )
        if not remotes:
            return {
                "ok": False,
                "error": "Keine Remotes konfiguriert. `rclone config` ausführen.",
            }

        store = get_config()
        snapshot = store.snapshot()
        backup = snapshot.get("backup") or {}
        configured = str(backup.get("rclone_remote") or "")
        response: dict[str, Any] = {
            "ok": True,
            "remotes": remotes,
            "configured_remote": configured,
            "remote_exists": configured in remotes if configured else None,
        }
        if configured and not response["remote_exists"]:
            return {
                **response,
                "ok": False,
                "error": f"Konfigurierter Remote '{configured}' fehlt. Vorhanden: {', '.join(remotes)}",
            }

        pair: dict[str, Any] | None = None
        if request.pair is not None:
            candidate = dict(snapshot)
            candidate_backup = dict(backup)
            candidate_backup["pairs"] = [request.pair]
            candidate["backup"] = candidate_backup
            try:
                normalized, warnings = validate_config(candidate)
            except ConfigValidationError as exc:
                raise HTTPException(
                    422, {"message": "Pair ungültig", "errors": exc.errors}
                ) from exc
            pair = (normalized.get("backup") or {}).get("pairs", [None])[0]
            response["tested_unsaved"] = True
            response["warnings"] = warnings
        elif request.pair_index is not None:
            pairs = backup.get("pairs") or []
            if not 0 <= request.pair_index < len(pairs):
                return {**response, "ok": False, "error": "pair_index ungültig"}
            pair = pairs[request.pair_index]

        if pair is not None:
            remote_path = str(pair.get("remote") or "")
            cache_dir = os.getenv(
                "RCLONE_CACHE_DIR", "/opt/rclone-sync/data/.rclone-cache"
            )
            size_result = subprocess.run(
                [
                    "rclone",
                    "size",
                    "--json",
                    "--cache-dir",
                    cache_dir,
                    "--",
                    remote_path,
                ],
                capture_output=True,
                text=True,
                timeout=60,
                stdin=subprocess.DEVNULL,
                env=rclone_subprocess_env(),
            )
            response["remote_path"] = remote_path
            if size_result.returncode == 0:
                try:
                    size_data = json.loads(size_result.stdout or "{}")
                    response["remote_size"] = {
                        "count": size_data.get("count"),
                        "bytes": size_data.get("bytes"),
                    }
                except json.JSONDecodeError:
                    response["remote_size_output"] = size_result.stdout.strip()[:300]
            else:
                response["ok"] = False
                response["error"] = f"Remote-Pfad nicht lesbar: {remote_path}"
                response["remote_size_output"] = (
                    size_result.stderr or size_result.stdout
                ).strip()[:300]

            local_path = str(pair.get("local") or "")
            response["local_path"] = local_path
            response["local_exists"] = (
                True if _is_remote(local_path) else Path(local_path).is_dir()
            )
            if not response["local_exists"]:
                response["ok"] = False
                response["error"] = (
                    f"Lokaler Pfad fehlt oder ist kein Verzeichnis: {local_path}"
                )

        response["message"] = f"rclone ok — {len(remotes)} Remote(s)"
        return response
    except HTTPException:
        raise
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "rclone Timeout"}
    except FileNotFoundError:
        return {"ok": False, "error": "rclone binary nicht gefunden"}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
