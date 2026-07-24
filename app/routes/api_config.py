"""API für validierte, atomare Konfigurationsänderungen."""

from __future__ import annotations

import copy
import hashlib
import logging
import os
import stat
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import bcrypt
from croniter import croniter
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..auth import require_auth, verify_password
from ..config_store import ConfigConflictError, get_config
from ..file_lock import acquire as acquire_file_lock
from ..file_lock import release as release_file_lock
from ..config_validation import ConfigValidationError, validate_config
from ..db import get_db
from ..security import ensure_within, require_csrf

router = APIRouter(
    prefix="/api/config",
    tags=["config"],
    dependencies=[Depends(require_auth), Depends(require_csrf)],
)

logger = logging.getLogger(__name__)
_PLACEHOLDER = "***SET***"
_SENSITIVE = (
    ("web", "secret_key"),
    ("web", "password_hash"),
    ("web", "password"),
    ("pbs", "password"),
)
_MAX_FILTER_BYTES = 2 * 1024 * 1024
_BCRYPT_MAX_PASSWORD_BYTES = 72
_SERVER_OWNED_WEB_FIELDS = (
    "password",
    "password_hash",
    "secret_key",
    "session_version",
)


def _walk_parent(
    mapping: dict[str, Any], keys: tuple[str, ...], *, create: bool = False
) -> dict[str, Any] | None:
    current: Any = mapping
    for key in keys[:-1]:
        if not isinstance(current, dict):
            return None
        if key not in current:
            if not create:
                return None
            current[key] = {}
        current = current.get(key)
    return current if isinstance(current, dict) else None


def _redact(data: dict[str, Any], *, revision: str | None = None) -> dict[str, Any]:
    out = copy.deepcopy(data)
    for keys in _SENSITIVE:
        parent = _walk_parent(out, keys)
        if parent is not None and parent.get(keys[-1]):
            parent[keys[-1]] = _PLACEHOLDER
    notifications = out.get("notifications")
    if isinstance(notifications, dict):
        hooks = notifications.get("webhooks")
        if isinstance(hooks, list):
            for hook in hooks:
                if isinstance(hook, dict) and hook.get("url"):
                    hook["url"] = _PLACEHOLDER
    if revision is not None:
        out["_revision"] = revision
    return out


def _preserve_sensitive(new_data: dict[str, Any], old_data: dict[str, Any]) -> None:
    """UI-Platzhalter oder ausgelassene Secrets durch den gespeicherten Wert ersetzen."""
    for keys in _SENSITIVE:
        old_parent = _walk_parent(old_data, keys)
        new_parent = _walk_parent(new_data, keys)
        old_value = old_parent.get(keys[-1], "") if old_parent else ""
        if new_parent is None:
            new_parent = _walk_parent(new_data, keys, create=True)
        if new_parent is None:
            continue
        value = new_parent.get(keys[-1])
        if value in (None, _PLACEHOLDER):
            new_parent[keys[-1]] = old_value

    old_hooks = (
        ((old_data.get("notifications") or {}).get("webhooks") or [])
        if isinstance(old_data.get("notifications"), dict)
        else []
    )
    new_notifications = new_data.get("notifications")
    if isinstance(new_notifications, dict) and isinstance(
        new_notifications.get("webhooks"), list
    ):
        by_id = {
            str(hook.get("id")): hook
            for hook in old_hooks
            if isinstance(hook, dict) and hook.get("id")
        }
        for index, hook in enumerate(new_notifications["webhooks"]):
            if not isinstance(hook, dict) or hook.get("url") != _PLACEHOLDER:
                continue
            old_hook = by_id.get(str(hook.get("id")))
            if (
                old_hook is None
                and index < len(old_hooks)
                and isinstance(old_hooks[index], dict)
            ):
                old_hook = old_hooks[index]
            hook["url"] = str((old_hook or {}).get("url") or "")

    old_web = old_data.get("web") if isinstance(old_data.get("web"), dict) else {}
    new_web = new_data.setdefault("web", {})
    if not isinstance(new_web, dict):
        return
    for key in _SERVER_OWNED_WEB_FIELDS:
        new_web[key] = copy.deepcopy(old_web.get(key, ""))


def _audit_best_effort(event: str, *, actor: str, details: dict[str, Any]) -> None:
    try:
        get_db().audit_add(event, actor=actor, details=details)
    except Exception:
        # Die Aktion ist zu diesem Zeitpunkt bereits atomar persistiert. Ein
        # Audit-Ausfall darf daher keinen irreführenden HTTP-500 erzeugen.
        logger.exception("Audit-Ereignis %s konnte nicht gespeichert werden", event)


def _semantic_bool(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "ja"}:
        return True
    if text in {"0", "false", "no", "off", "nein", ""}:
        return False
    return default


def _sensitive_config_changed(
    old_data: dict[str, Any], new_data: dict[str, Any]
) -> bool:
    old_web = old_data.get("web") if isinstance(old_data.get("web"), dict) else {}
    new_web = new_data.get("web") if isinstance(new_data.get("web"), dict) else {}
    if (
        str(old_web.get("username") or "").casefold()
        != str(new_web.get("username") or "").casefold()
    ):
        return True
    if old_web.get("local_browse_roots") != new_web.get("local_browse_roots"):
        return True

    old_pbs = old_data.get("pbs") if isinstance(old_data.get("pbs"), dict) else {}
    new_pbs = new_data.get("pbs") if isinstance(new_data.get("pbs"), dict) else {}
    for key in ("password", "repository", "namespace", "backup_id", "fingerprint"):
        if str(old_pbs.get(key) or "") != str(new_pbs.get(key) or ""):
            return True

    def target_routes(section: dict[str, Any]) -> dict[str, tuple[str, str]]:
        routes: dict[str, tuple[str, str]] = {}
        for index, target in enumerate(section.get("targets") or []):
            if not isinstance(target, dict):
                continue
            namespace = str(target.get("namespace") or "")
            backup_id = str(target.get("backup_id") or "")
            if not namespace and not backup_id:
                continue
            identity = str(target.get("id") or "").strip()
            if not identity:
                name = str(target.get("name") or "").strip().casefold()
                identity = f"name:{name}" if name else f"index:{index}"
            routes[identity] = (namespace, backup_id)
        return routes

    if target_routes(old_pbs) != target_routes(new_pbs):
        return True

    old_backup = (
        old_data.get("backup") if isinstance(old_data.get("backup"), dict) else {}
    )
    new_backup = (
        new_data.get("backup") if isinstance(new_data.get("backup"), dict) else {}
    )
    for key in ("allow_unsafe_rclone_args", "allow_external_filter_files"):
        if not _semantic_bool(old_backup.get(key), default=False) and _semantic_bool(
            new_backup.get(key), default=False
        ):
            return True
    for key in ("require_delete_confirmation", "require_max_delete_for_sync"):
        if _semantic_bool(old_backup.get(key), default=True) and not _semantic_bool(
            new_backup.get(key), default=True
        ):
            return True

    old_notifications = (
        old_data.get("notifications")
        if isinstance(old_data.get("notifications"), dict)
        else {}
    )
    new_notifications = (
        new_data.get("notifications")
        if isinstance(new_data.get("notifications"), dict)
        else {}
    )
    for key in ("allow_http", "allow_private_targets"):
        if not _semantic_bool(
            old_notifications.get(key), default=False
        ) and _semantic_bool(new_notifications.get(key), default=False):
            return True

    def hook_targets(section: dict[str, Any]) -> dict[str, str]:
        return {
            str(hook.get("id") or index): str(hook.get("url") or "")
            for index, hook in enumerate(section.get("webhooks") or [])
            if isinstance(hook, dict)
        }

    return hook_targets(old_notifications) != hook_targets(new_notifications)


def _filter_revision(path: Path, raw: bytes | None = None) -> str:
    if raw is None:
        raw = path.read_bytes() if path.exists() else b""
    marker = b"present\0" if path.exists() else b"missing\0"
    return hashlib.sha256(str(path).encode("utf-8") + b"\0" + marker + raw).hexdigest()


def _filter_path() -> Path:
    cfg = get_config()
    data_root = Path(cfg.get("paths", "data_dir", default="/opt/rclone-sync/data"))
    configured = cfg.get(
        "backup", "filter_file", default=str(data_root / "rclone-filters.txt")
    ) or str(data_root / "rclone-filters.txt")
    return ensure_within(Path(str(configured)), [data_root])


@router.get("")
def get_config_endpoint() -> dict[str, Any]:
    snapshot, revision = get_config().snapshot_with_revision()
    return _redact(snapshot, revision=revision)


class ConfigUpdate(BaseModel):
    config: dict[str, Any]
    current_password: str | None = Field(default=None, min_length=1, max_length=1024)


class SchedulePreviewRequest(BaseModel):
    expression: str = Field(default="manual", max_length=128)
    timezone: str = Field(default="Europe/Berlin", min_length=1, max_length=128)
    count: int = Field(default=5, ge=1, le=10)


@router.put("")
def update_config(
    body: ConfigUpdate, user: str = Depends(require_auth)
) -> dict[str, Any]:
    store = get_config()
    old_data, current_revision = store.snapshot_with_revision()
    new_data = copy.deepcopy(body.config)
    expected_revision = str(new_data.pop("_revision", "") or "") or None
    if expected_revision is None:
        raise HTTPException(
            428,
            {
                "message": "Konfigurationsrevision fehlt",
                "reload_required": True,
                "current_revision": current_revision,
            },
        )
    _preserve_sensitive(new_data, old_data)
    old_web = old_data.get("web") if isinstance(old_data.get("web"), dict) else {}
    new_web = new_data.get("web") if isinstance(new_data.get("web"), dict) else {}
    username_changed = (
        str(old_web.get("username") or "").casefold()
        != str(new_web.get("username") or "").casefold()
    )
    if _sensitive_config_changed(old_data, new_data):
        if not body.current_password or not verify_password(
            user, body.current_password
        ):
            raise HTTPException(
                403,
                {
                    "message": "Für diese sicherheitsrelevante Änderung ist das "
                    "aktuelle Passwort nötig",
                    "reauth_required": True,
                },
            )
    if username_changed:
        try:
            version = int(old_web.get("session_version", 1) or 1)
        except (TypeError, ValueError):
            version = 1
        new_web["session_version"] = max(1, version) + 1

    try:
        normalized, warnings = validate_config(new_data)
    except ConfigValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={"message": "Konfiguration ungültig", "errors": exc.errors},
        )
    try:
        revision = store.replace(normalized, expected_revision=expected_revision)
    except ConfigConflictError:
        raise HTTPException(
            409,
            {
                "message": "Konfiguration wurde parallel geändert",
                "reload_required": True,
                "current_revision": store.revision,
            },
        )
    except (OSError, ValueError) as exc:
        raise HTTPException(
            500, f"Konfiguration konnte nicht gespeichert werden: {exc}"
        )
    _audit_best_effort(
        "config_saved",
        actor=user,
        details={
            "revision": revision,
            "pair_count": len((normalized.get("backup") or {}).get("pairs") or []),
            "warning_count": len(warnings),
        },
    )
    return {
        "ok": True,
        "warnings": warnings,
        "config": _redact(normalized, revision=revision),
    }


@router.post("/validate")
def validate_config_endpoint(body: ConfigUpdate) -> dict[str, Any]:
    """Prüft den aktuellen GUI-Entwurf vollständig, ohne ihn zu speichern."""
    store = get_config()
    old_data, current_revision = store.snapshot_with_revision()
    candidate = copy.deepcopy(body.config)
    expected_revision = str(candidate.pop("_revision", "") or "") or None
    _preserve_sensitive(candidate, old_data)
    try:
        normalized, warnings = validate_config(candidate)
    except ConfigValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={"message": "Konfiguration ungültig", "errors": exc.errors},
        )
    return {
        "ok": True,
        "warnings": warnings,
        "revision_matches": expected_revision in (None, current_revision),
        "current_revision": current_revision,
        "config": _redact(normalized, revision=current_revision),
    }


@router.post("/schedule-preview")
def preview_schedule(body: SchedulePreviewRequest) -> dict[str, Any]:
    """Liefert eine sichere Vorschau für einen noch ungespeicherten Cron-Entwurf."""
    expression = body.expression.strip()
    disabled = {"", "manual", "off", "disabled", "none"}
    if expression.casefold() in disabled:
        return {
            "ok": True,
            "enabled": False,
            "expression": expression or "manual",
            "timezone": body.timezone,
            "next_runs": [],
        }

    if len(expression.split()) != 5 or not croniter.is_valid(expression):
        raise HTTPException(422, "Zeitplan ist keine gültige 5-stellige Cron-Angabe")
    try:
        timezone = ZoneInfo(body.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise HTTPException(422, f"Unbekannte Zeitzone: {body.timezone}")

    now = datetime.now(timezone)
    iterator = croniter(expression, now)
    runs: list[dict[str, Any]] = []
    for _ in range(body.count):
        next_run = iterator.get_next(datetime)
        runs.append(
            {
                "timestamp": next_run.timestamp(),
                "iso": next_run.isoformat(),
            }
        )
    return {
        "ok": True,
        "enabled": True,
        "expression": expression,
        "timezone": body.timezone,
        "generated_at": now.isoformat(),
        "next_runs": runs,
    }


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=12, max_length=1024)


@router.post("/change-password")
def change_password(
    body: PasswordChange, user: str = Depends(require_auth)
) -> dict[str, Any]:
    """Ändert das Passwort und invalidiert alle bestehenden Sessions."""
    if not verify_password(user, body.current_password):
        raise HTTPException(403, "Aktuelles Passwort falsch")

    new = body.new_password
    if new != new.strip():
        raise HTTPException(
            400, "Passwort darf nicht mit Leerzeichen beginnen oder enden"
        )
    if new == body.current_password:
        raise HTTPException(400, "Neues Passwort muss vom alten abweichen")
    if len(new.encode("utf-8")) > _BCRYPT_MAX_PASSWORD_BYTES:
        raise HTTPException(400, "Passwort darf höchstens 72 UTF-8-Bytes lang sein")

    try:
        new_hash = bcrypt.hashpw(new.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode(
            "ascii"
        )
    except ValueError as exc:
        raise HTTPException(400, f"Passwort kann nicht verarbeitet werden: {exc}")

    def updater(data: dict[str, Any]) -> None:
        web = data.setdefault("web", {})
        if not isinstance(web, dict):
            raise ValueError("web muss ein Mapping sein")
        web["password_hash"] = new_hash
        web["password"] = ""
        try:
            current_version = int(web.get("session_version", 1) or 1)
        except (TypeError, ValueError):
            current_version = 1
        web["session_version"] = max(1, current_version) + 1

    try:
        store = get_config()
        store.update(updater)
        data_dir = Path(store.get("paths", "data_dir", default="/opt/rclone-sync/data"))
        (data_dir / ".initial-password").unlink(missing_ok=True)
    except (OSError, ValueError) as exc:
        raise HTTPException(500, f"Passwort konnte nicht gespeichert werden: {exc}")
    _audit_best_effort("password_changed", actor=user, details={})
    return {"ok": True, "message": "Passwort geändert", "reauthenticate": True}


class FilterPayload(BaseModel):
    content: str = Field(max_length=_MAX_FILTER_BYTES)
    revision: str = Field(min_length=64, max_length=64)


@router.get("/filter-file")
def get_filter_file() -> dict[str, Any]:
    path = _filter_path()
    if not path.exists():
        return {
            "path": str(path),
            "exists": False,
            "content": "",
            "revision": _filter_revision(path, b""),
        }
    if not path.is_file():
        raise HTTPException(400, "filter_file ist keine Datei")
    try:
        if path.stat().st_size > _MAX_FILTER_BYTES:
            raise HTTPException(413, "Filter-Datei ist zu groß")
        raw = path.read_bytes()
        return {
            "path": str(path),
            "exists": True,
            "content": raw.decode("utf-8"),
            "revision": _filter_revision(path, raw),
        }
    except HTTPException:
        raise
    except (OSError, UnicodeError) as exc:
        raise HTTPException(500, f"Lesefehler: {exc}")


@router.put("/filter-file")
def save_filter_file(body: FilterPayload) -> dict[str, Any]:
    path = _filter_path()
    encoded = body.content.encode("utf-8")
    if len(encoded) > _MAX_FILTER_BYTES:
        raise HTTPException(413, "Filter-Datei ist zu groß")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_name(f".{path.name}.lock")
        lock_flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        lock_fd = os.open(lock_path, lock_flags, stat.S_IRUSR | stat.S_IWUSR)
        os.fchmod(lock_fd, stat.S_IRUSR | stat.S_IWUSR)
        try:
            acquire_file_lock(lock_fd)
            previous = path.read_bytes() if path.exists() else b""
            current_revision = _filter_revision(path, previous)
            if body.revision != current_revision:
                raise HTTPException(
                    409,
                    {
                        "message": "Filter-Datei wurde parallel geändert",
                        "reload_required": True,
                        "current_revision": current_revision,
                    },
                )
            if path.exists() and previous != encoded:
                backup = path.with_suffix(path.suffix + ".bak")
                backup_fd, backup_tmp_name = tempfile.mkstemp(
                    prefix=f".{backup.name}.", suffix=".tmp", dir=str(path.parent)
                )
                backup_tmp = Path(backup_tmp_name)
                try:
                    with os.fdopen(backup_fd, "wb") as stream:
                        stream.write(previous)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.chmod(backup_tmp, stat.S_IRUSR | stat.S_IWUSR)
                    os.replace(backup_tmp, backup)
                finally:
                    backup_tmp.unlink(missing_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
            )
            tmp = Path(tmp_name)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
                os.replace(tmp, path)
                try:
                    dir_fd = os.open(path.parent, getattr(os, "O_DIRECTORY", 0))
                    try:
                        os.fsync(dir_fd)
                    finally:
                        os.close(dir_fd)
                except OSError:
                    pass
            finally:
                tmp.unlink(missing_ok=True)
        finally:
            try:
                release_file_lock(lock_fd)
            finally:
                os.close(lock_fd)
        revision = _filter_revision(path, encoded)
        get_db().audit_add(
            "filter_saved",
            actor="web",
            details={"bytes": len(encoded), "revision": revision},
        )
        return {
            "ok": True,
            "path": str(path),
            "bytes": len(encoded),
            "revision": revision,
        }
    except HTTPException:
        raise
    except OSError as exc:
        raise HTTPException(500, f"Schreibfehler: {exc}")


class WebhookTest(BaseModel):
    index: int | None = Field(default=None, ge=0, le=9999)
    id: str | None = Field(default=None, min_length=8, max_length=64)
    event: str = Field(default="sync_ok", max_length=64)


@router.post("/test-webhook")
def test_webhook(body: WebhookTest) -> dict[str, Any]:
    from ..notifications import EVENTS, notify_one

    hooks = get_config().get("notifications", "webhooks", default=[]) or []
    hook = None
    if body.id:
        hook = next(
            (
                item
                for item in hooks
                if isinstance(item, dict) and str(item.get("id")) == body.id
            ),
            None,
        )
    elif body.index is not None and body.index < len(hooks):
        hook = hooks[body.index]
    if not isinstance(hook, dict):
        raise HTTPException(404, "Webhook nicht gefunden")
    if body.event not in EVENTS:
        raise HTTPException(400, "Unbekanntes Event")
    try:
        notify_one(
            hook,
            body.event,
            "rclone-sync Test",
            "Das ist ein Test aus der rclone-sync Web-UI.",
            source="ui-test",
        )
        return {"ok": True}
    except Exception as exc:
        raise HTTPException(502, f"Webhook-Test fehlgeschlagen: {exc}")
