"""rclone-Pfad-Browser für Pair-Konfiguration."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException

from ..auth import require_auth

router = APIRouter(prefix="/api/browse", tags=["browse"], dependencies=[Depends(require_auth)])


@router.get("/rclone")
def browse_rclone(path: str = "") -> Dict[str, Any]:
    """Listet rclone-Pfad. Leer = alle Remotes."""
    try:
        if not path:
            r = subprocess.run(["rclone", "listremotes"], capture_output=True, text=True, timeout=10)
            if r.returncode != 0:
                raise HTTPException(500, f"listremotes: {r.stderr.strip()}")
            remotes = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
            return {
                "path": "", "parent": None, "is_root": True,
                "entries": [{"name": rmt.rstrip(":"), "path": rmt, "is_dir": True} for rmt in remotes],
            }

        if path.startswith("-") or any(c in path for c in ("\n", "\r", "\x00")):
            raise HTTPException(400, "Pfad enthält ungültige Zeichen")
        if ":" not in path:
            raise HTTPException(400, "Pfad muss 'remote:dir' sein")

        r = subprocess.run(
            ["rclone", "lsjson", "--dirs-only", "--", path],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode != 0:
            raise HTTPException(500, f"lsjson: {r.stderr.strip()[:200]}")
        items = json.loads(r.stdout or "[]")
        entries = [{
            "name": it.get("Name"),
            "path": path.rstrip("/") + "/" + it.get("Name"),
            "is_dir": True,
        } for it in sorted(items, key=lambda x: x.get("Name", "").lower())]

        # Parent
        if path.endswith(":") or path.endswith(":/"):
            parent = ""
        else:
            base, rest = path.split(":", 1)
            rest = rest.rstrip("/")
            parent = base + ":" + rest.rsplit("/", 1)[0] if "/" in rest else base + ":"
        return {"path": path, "parent": parent, "is_root": False, "entries": entries}
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "rclone Timeout")
    except FileNotFoundError:
        raise HTTPException(500, "rclone nicht installiert")


@router.get("/local")
def browse_local(path: str = "/mnt") -> Dict[str, Any]:
    p = Path(path).expanduser().resolve()
    # Erlaubte Roots
    allowed = (Path("/mnt"), Path("/opt/rclone-sync"))
    if not any(str(p).startswith(str(a)) for a in allowed):
        raise HTTPException(403, f"Pfad nicht erlaubt: {p}")
    if not p.exists() or not p.is_dir():
        return {"path": str(p), "entries": [], "error": "Verzeichnis fehlt"}
    entries = []
    try:
        for x in sorted(p.iterdir(), key=lambda x: x.name.lower()):
            if x.is_dir() and not x.name.startswith("."):
                entries.append({"name": x.name, "path": str(x), "is_dir": True})
    except PermissionError:
        raise HTTPException(403, "Keine Leseberechtigung")
    parent = str(p.parent) if str(p.parent) != str(p) else None
    return {"path": str(p), "parent": parent, "entries": entries}
