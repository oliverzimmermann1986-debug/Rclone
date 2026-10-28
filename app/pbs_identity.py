"""Canonical PBS snapshot-group identity shared by validation and execution."""

from __future__ import annotations

import re


def canonical_pbs_backup_id(value: str) -> str:
    """Preserve the existing runner mapping without merging distinct targets silently."""
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "host")).strip("._")
    return cleaned[:80] or "host"
