"""Zero-knowledge crypt wizard: encrypted remote on top of an existing cloud remote."""

from __future__ import annotations

import re
import subprocess

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, SecretStr

from ..auth import require_auth
from ..crypt_remote import (
    FILENAME_MODES,
    build_create_payload,
    generate_key,
    validate_base_path,
)
from ..db import get_db
from ..rclone_args import (
    DANGEROUS_RCLONE_REMOTE_TYPES,
    UnsafeRcloneConfigError,
    assert_safe_rclone_config,
    rclone_subprocess_env,
)
from ..recovery_points import RecoveryPointError, _remote_listing
from ..security import require_csrf
from .api_remotes import _lock, _rc

router = APIRouter(
    prefix="/api/remotes/crypt",
    tags=["remotes"],
    dependencies=[Depends(require_auth), Depends(require_csrf)],
)
WARNING = (
    "Diese Schlüssel werden nur jetzt angezeigt. Ohne sie sind die verschlüsselten "
    "Cloud-Daten unwiederbringlich verloren. Bitte im Passwortmanager sichern."
)


class CreateCrypt(BaseModel):
    name: str
    base_remote: str
    base_path: str = ""
    filename_encryption: str = "standard"
    directory_name_encryption: bool = True
    password: SecretStr | None = None
    password2: SecretStr | None = None


def _check_name(name: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{1,63}", name):
        raise HTTPException(
            400, "Remote-Name: 2–64 Buchstaben, Ziffern, Bindestriche oder Unterstriche"
        )


def _require_safe_rclone_config() -> None:
    try:
        assert_safe_rclone_config()
    except UnsafeRcloneConfigError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/candidates")
def candidates() -> dict:
    _require_safe_rclone_config()
    remotes = _rc("config/dump")
    return {
        "candidates": [
            {"name": name, "type": conf.get("type", "")}
            for name, conf in sorted(remotes.items())
            if str(conf.get("type") or "").strip().lower()
            not in {"crypt", *DANGEROUS_RCLONE_REMOTE_TYPES}
        ]
    }


@router.post("")
def create(payload: CreateCrypt) -> dict:
    _require_safe_rclone_config()
    _check_name(payload.name)
    if payload.filename_encryption not in FILENAME_MODES:
        raise HTTPException(400, "Unbekannte Art der Dateinamen-Verschlüsselung")
    try:
        base_path = validate_base_path(payload.base_path)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    keys, generated = {}, False
    for field in ("password", "password2"):
        own = getattr(payload, field)
        value = own.get_secret_value() if own else ""
        if value and not 16 <= len(value) <= 1024:
            raise HTTPException(400, "Eigene Schlüssel: 16 bis 1024 Zeichen")
        generated |= not value
        keys[field] = value or generate_key()
    with _lock:
        existing = _rc("config/listremotes").get("remotes", [])
        if payload.name.casefold() in {n.rstrip(":").casefold() for n in existing}:
            raise HTTPException(409, "Ein Remote mit diesem Namen existiert bereits")
        if payload.base_remote not in {c["name"] for c in candidates()["candidates"]}:
            raise HTTPException(400, "Unbekanntes oder ungeeignetes Basis-Remote")
        try:
            result = _rc(
                "config/create",
                build_create_payload(
                    payload.name,
                    payload.base_remote,
                    base_path,
                    payload.filename_encryption,
                    payload.directory_name_encryption,
                    keys["password"],
                    keys["password2"],
                ),
            )
            if result.get("State") or result.get("Error"):
                raise HTTPException(
                    502, "Verschlüsseltes Remote konnte nicht angelegt werden"
                )
        except HTTPException:
            # A half-written remote with keys nobody saw would encrypt backups unrecoverably.
            try:
                _rc("config/delete", {"name": payload.name})
            except HTTPException:
                pass
            raise
    try:
        get_db().audit_add(
            "crypt_remote_created",
            actor="web",
            details={
                "name": payload.name,
                "base_remote": payload.base_remote,
                "filename_encryption": payload.filename_encryption,
                "generated": generated,
            },
        )
    except Exception:
        pass
    return {
        "name": payload.name,
        "remote": f"{payload.base_remote}:{base_path}",
        "filename_encryption": payload.filename_encryption,
        "directory_name_encryption": payload.directory_name_encryption,
        "keys": keys,
        "warning": WARNING,
    }


@router.post("/{name}/verify")
def verify(name: str) -> dict:
    _require_safe_rclone_config()
    _check_name(name)
    try:
        code = subprocess.run(
            ["rclone", "lsd", f"{name}:"],
            env=rclone_subprocess_env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=60,
        ).returncode
    except (OSError, subprocess.TimeoutExpired):
        code = -1
    # Exit 3 = directory not found: base remote answered, the new path just has no data yet.
    if code not in (0, 3):
        raise HTTPException(
            502,
            "Verschlüsseltes Remote ist nicht lesbar (Basis-Remote oder Pfad prüfen)",
        )
    try:
        files, _truncated = _remote_listing(
            ["rclone", "lsjson", "--files-only", "--max-depth", "1", "--", f"{name}:"]
        )
        sample = next(
            (
                item
                for item in files
                if not item.get("IsDir") and int(item.get("Size") or 0) > 0
            ),
            None,
        )
        if sample is None:
            return {
                "ok": True,
                "verified": False,
                "message": "Erreichbar; ohne vorhandene Datei wurde keine Entschlüsselung geprüft.",
            }
        result = subprocess.run(
            ["rclone", "cat", "--head", "1", "--", f"{name}:{sample['Path']}"],
            env=rclone_subprocess_env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=60,
        )
    except (
        OSError,
        subprocess.TimeoutExpired,
        RecoveryPointError,
        ValueError,
        KeyError,
    ) as exc:
        raise HTTPException(502, "Entschlüsselung konnte nicht geprüft werden") from exc
    if result.returncode != 0 or not result.stdout:
        raise HTTPException(
            502,
            "Vorhandene Datei konnte mit diesen Schlüsseln nicht entschlüsselt werden",
        )
    return {
        "ok": True,
        "verified": True,
        "message": "Vorhandene Datei erfolgreich entschlüsselt.",
    }
