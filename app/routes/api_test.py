"""rclone Test-Endpoint: listremotes + optional Pair-Check."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth import require_auth
from ..config_store import get_config

router = APIRouter(prefix="/api/test", tags=["test"], dependencies=[Depends(require_auth)])


class RcloneTest(BaseModel):
    pair_index: Optional[int] = None


@router.post("/rclone")
def test_rclone(req: RcloneTest) -> Dict[str, Any]:
    try:
        r = subprocess.run(["rclone", "listremotes"], capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            return {"ok": False, "error": f"listremotes: {r.stderr.strip()}"}
        remotes = [ln.strip().rstrip(":") for ln in r.stdout.splitlines() if ln.strip()]
        if not remotes:
            return {"ok": False, "error": "Keine Remotes konfiguriert. `rclone config` ausführen."}

        backup = get_config().get("backup", default={}) or {}
        configured = backup.get("rclone_remote", "")
        result = {
            "ok": True, "remotes": remotes,
            "configured_remote": configured,
            "remote_exists": configured in remotes,
        }
        if not result["remote_exists"]:
            result["ok"] = False
            result["error"] = f"Konfigurierter Remote '{configured}' fehlt. Vorhanden: {', '.join(remotes)}"
            return result

        if req.pair_index is not None:
            pairs = backup.get("pairs") or []
            if req.pair_index >= len(pairs):
                return {**result, "ok": False, "error": "pair_index ungültig"}
            pair = pairs[req.pair_index]
            r2 = subprocess.run(
                ["rclone", "size", pair.get("remote", "")],
                capture_output=True, text=True, timeout=60,
            )
            result["remote_path"] = pair.get("remote")
            result["remote_size_output"] = (r2.stdout if r2.returncode == 0 else r2.stderr).strip()[:300]
            result["local_path"] = pair.get("local")
            result["local_exists"] = Path(pair.get("local", "")).exists()
            if not result["local_exists"]:
                result["ok"] = False
                result["error"] = f"Lokaler Pfad fehlt: {pair.get('local')}"

        result["message"] = f"rclone ok — {len(remotes)} Remote(s)"
        return result
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "rclone Timeout"}
    except FileNotFoundError:
        return {"ok": False, "error": "rclone binary nicht gefunden"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
