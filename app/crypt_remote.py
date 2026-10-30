"""Zero-knowledge crypt remote: keys, path check and rclone payload."""

from __future__ import annotations

import copy
import re
import secrets
import unicodedata

FILENAME_MODES = ("standard", "obfuscate", "off")
_SECRETS = ("password", "password2")


def generate_key() -> str:
    return secrets.token_urlsafe(32)


def validate_base_path(path: str) -> str:
    """Relative path inside the base remote; raises ValueError with a user message."""
    if (
        len(path) > 512
        or path.startswith(("/", "\\"))
        or ".." in re.split(r"[/\\]", path)
        or any(unicodedata.category(c) == "Cc" for c in path)
    ):
        raise ValueError(
            "Pfad: relativ, ohne führenden Schrägstrich, ohne „..“ und Steuerzeichen, höchstens 512 Zeichen"
        )
    return path


def build_create_payload(
    name: str,
    base_remote: str,
    base_path: str,
    filename_encryption: str,
    directory_name_encryption: bool,
    password: str,
    password2: str,
) -> dict:
    return {
        "name": name,
        "type": "crypt",
        "parameters": {
            "remote": f"{base_remote}:{base_path}",
            "filename_encryption": filename_encryption,
            "directory_name_encryption": "true"
            if directory_name_encryption
            else "false",
            "password": password,
            "password2": password2,
        },
        # rclone expects crypt keys obscured in the config (reversible, not encryption).
        "opt": {"obscure": True, "nonInteractive": True},
    }


def strip_secrets(payload: dict) -> dict:
    clean = copy.deepcopy(payload)
    for key in _SECRETS:
        clean.get("parameters", {}).pop(key, None)
    return clean
