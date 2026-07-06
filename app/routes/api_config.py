"""API für Config-Lesen + -Schreiben."""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth import require_auth
from ..config_store import get_config

router = APIRouter(prefix="/api/config", tags=["config"], dependencies=[Depends(require_auth)])

# Sensitiv-Felder die nie zurückgesendet werden (sind hier minimal — nur secret_key)
_SENSITIVE = (("web", "secret_key"), ("web", "password_hash"), ("web", "password"))


def _redact(cfg: Dict[str, Any]) -> Dict[str, Any]:
    import copy as _c
    out = _c.deepcopy(cfg)
    for keys in _SENSITIVE:
        cur = out
        for k in keys[:-1]:
            if not isinstance(cur, dict):
                break
            cur = cur.get(k, {})
        if isinstance(cur, dict) and keys[-1] in cur and cur[keys[-1]]:
            cur[keys[-1]] = "***SET***"
    return out


@router.get("")
def get_config_endpoint() -> Dict[str, Any]:
    cfg = get_config()
    return _redact(cfg._data)


class ConfigUpdate(BaseModel):
    config: Dict[str, Any]


@router.put("")
def update_config(body: ConfigUpdate) -> Dict[str, Any]:
    cfg = get_config()
    new_data = body.config
    # Sensitive Felder die als '***SET***' kamen behalten, nicht überschreiben
    for keys in _SENSITIVE:
        cur_new = new_data
        cur_old = cfg._data
        ok = True
        for k in keys[:-1]:
            if not isinstance(cur_new, dict) or not isinstance(cur_old, dict):
                ok = False
                break
            cur_new = cur_new.get(k, {})
            cur_old = cur_old.get(k, {})
        if ok and isinstance(cur_new, dict) and cur_new.get(keys[-1]) == "***SET***":
            cur_new[keys[-1]] = cur_old.get(keys[-1], "")
    cfg._data = new_data
    cfg.save()
    return {"ok": True}


class PasswordChange(BaseModel):
    current_password: str
    new_password: str


@router.post("/change-password")
def change_password(body: PasswordChange, user: str = Depends(require_auth)) -> Dict[str, Any]:
    """Ändert das eigene Web-Passwort. Erfordert aktuelles Passwort als
    Verification, damit ein gestohlenes Session-Cookie nicht reicht."""
    import bcrypt
    from ..auth import verify_password
    cfg = get_config()

    # Current verifizieren
    if not verify_password(user, body.current_password):
        raise HTTPException(403, "Aktuelles Passwort falsch")

    new = body.new_password.strip()
    if len(new) < 8:
        raise HTTPException(400, "Neues Passwort muss min. 8 Zeichen haben")
    if new == body.current_password:
        raise HTTPException(400, "Neues Passwort muss vom alten abweichen")

    new_hash = bcrypt.hashpw(new.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("ascii")
    cfg.set("web", "password_hash", new_hash)
    cfg.set("web", "password", "")  # Klartext-Fallback aufräumen
    cfg.save()
    return {"ok": True, "message": "Passwort geändert"}


class FilterPayload(BaseModel):
    content: str


@router.get("/filter-file")
def get_filter_file() -> dict:
    """Inhalt der rclone-filters.txt. Pfad aus config.backup.filter_file
    oder Default /opt/rclone-sync/data/rclone-filters.txt."""
    cfg = get_config()
    p = cfg.get("backup", "filter_file",
                default="/opt/rclone-sync/data/rclone-filters.txt")         or "/opt/rclone-sync/data/rclone-filters.txt"
    from pathlib import Path as _P
    base = _P("/opt/rclone-sync").resolve()
    resolved = _P(p).resolve()
    if not str(resolved).startswith(str(base)):
        raise HTTPException(400, "filter_file muss unter /opt/rclone-sync liegen")
    if not resolved.exists():
        return {"path": str(resolved), "exists": False, "content": ""}
    try:
        return {"path": str(resolved), "exists": True,
                "content": resolved.read_text(encoding="utf-8")}
    except Exception as e:
        raise HTTPException(500, f"Lesefehler: {e}")


@router.put("/filter-file")
def save_filter_file(body: FilterPayload) -> dict:
    """rclone-Filter-Datei schreiben (idempotent, atomic via .tmp+rename)."""
    cfg = get_config()
    p = cfg.get("backup", "filter_file",
                default="/opt/rclone-sync/data/rclone-filters.txt")         or "/opt/rclone-sync/data/rclone-filters.txt"
    from pathlib import Path as _P
    base = _P("/opt/rclone-sync").resolve()
    path = _P(p).resolve()
    if not str(path).startswith(str(base)):
        raise HTTPException(400, "filter_file muss unter /opt/rclone-sync liegen")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(body.content, encoding="utf-8")
        tmp.replace(path)
        return {"ok": True, "path": str(path), "bytes": len(body.content.encode())}
    except Exception as e:
        raise HTTPException(500, f"Schreibfehler: {e}")


class WebhookTest(BaseModel):
    index: int
    event: str = "sync_ok"


@router.post("/test-webhook")
def test_webhook(body: WebhookTest) -> Dict[str, Any]:
    """Sendet einen Test an genau einen konfigurierten Webhook."""
    from ..notifications import notify_one, EVENTS
    cfg = get_config()
    hooks = cfg.get("notifications", "webhooks", default=[]) or []
    if body.index < 0 or body.index >= len(hooks):
        raise HTTPException(404, "Webhook nicht gefunden")
    if body.event not in EVENTS:
        raise HTTPException(400, "Unbekanntes Event")
    try:
        notify_one(
            hooks[body.index],
            body.event,
            "rclone-sync Test",
            "Das ist ein Test aus der rclone-sync Web-UI.",
            source="ui-test",
        )
        return {"ok": True}
    except Exception as e:
        raise HTTPException(502, f"Webhook-Test fehlgeschlagen: {e}")
