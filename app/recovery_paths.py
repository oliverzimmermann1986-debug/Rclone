"""Side-effect-free locations and isolation rules for recovery storage."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .device_vault import _is_remote
from .jobs.restore_test import _endpoints


def recovery_path(config: Mapping[str, Any], key: str, default_name: str) -> Path:
    paths = config.get("paths")
    paths = paths if isinstance(paths, Mapping) else {}
    data = Path(str(paths.get("data_dir") or Path("/opt/rclone-sync/data").resolve()))
    root = Path(str(paths.get(key) or data / default_name)).expanduser()
    if not root.is_absolute():
        raise ValueError(f"paths.{key} muss absolut sein")
    return root


def validated_staging_path(
    config: Mapping[str, Any], *, pair: Mapping[str, Any] | None = None
) -> Path:
    """Reject overlapping paths before creating or changing any directory."""
    proposed = recovery_path(config, "recovery_dir", "recovery").resolve()
    endpoints = [
        recovery_path(config, "recovery_snapshots_dir", "recovery-snapshots").resolve()
    ]
    backup = config.get("backup")
    backup = backup if isinstance(backup, Mapping) else {}
    pairs = [*(backup.get("pairs") or []), pair]
    for configured in pairs:
        if not isinstance(configured, Mapping):
            continue
        endpoints.extend(
            Path(value).expanduser().resolve()
            for value in _endpoints(configured)
            if value and not _is_remote(value)
        )
    for target in (config.get("pbs") or {}).get("targets") or []:
        if isinstance(target, Mapping):
            endpoints.extend(
                Path(str(value)).expanduser().resolve()
                for value in target.get("paths") or []
                if value and not _is_remote(str(value))
            )
    if any(
        proposed == endpoint
        or proposed.is_relative_to(endpoint)
        or endpoint.is_relative_to(proposed)
        for endpoint in endpoints
    ):
        raise ValueError(
            "Recovery-Staging muss außerhalb aller Datenwege und vollständigen Stände liegen"
        )
    return proposed
