"""Browse and compare the version folders created by rclone ``--backup-dir``."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .jobs.restore_test import _endpoints
from .rclone_args import rclone_subprocess_env

MAX_POINTS = 180
MAX_DIFF_FILES = 20_000
MAX_DIFF_RESULTS = 750


class RecoveryPointError(ValueError):
    pass


def _is_remote(path: str) -> bool:
    if len(path) >= 3 and path[1] == ":" and path[2] in {"/", "\\"}:
        return False
    return ":" in path.split("/", 1)[0] and not path.startswith("/")


def _join(root: str, relative: str) -> str:
    if not relative:
        return root.rstrip("/") if not root.endswith(":") else root
    if not _is_remote(root):
        return str(Path(root).joinpath(*PurePosixPath(relative).parts))
    separator = "" if root.endswith(("/", ":")) else "/"
    return f"{root}{separator}{relative.lstrip('/')}"


def _safe_relative(value: str) -> str:
    raw = str(value or "").replace("\\", "/").strip().strip("/")
    candidate = PurePosixPath(raw)
    if (
        candidate.is_absolute()
        or ".." in candidate.parts
        or any(char in raw for char in ("\x00", "\r", "\n"))
    ):
        raise RecoveryPointError("Ungültiger relativer Pfad")
    return "" if raw in {"", "."} else candidate.as_posix()


def _resolve_spec(target: str, spec: str) -> str:
    if _is_remote(spec) or Path(spec).expanduser().is_absolute():
        return spec.rstrip("/")
    return _join(target, spec)


def version_root(config: Mapping[str, Any], pair: Mapping[str, Any]) -> str | None:
    backup = config.get("backup") if isinstance(config.get("backup"), Mapping) else {}
    generic = str(
        pair.get("backup_dir") or (backup or {}).get("backup_dir") or ""
    ).strip()
    direction = str(pair.get("direction") or "bisync").lower().strip()
    _source, target = _endpoints(pair)
    if direction == "bisync":
        specific = str(
            pair.get("backup_dir2") or (backup or {}).get("backup_dir2") or ""
        ).strip()
        if specific:
            generic = specific
        elif generic and (_is_remote(generic) or generic.startswith("/")):
            return None
    if not generic:
        return None
    prefix = (
        generic.split("{date}", 1)[0].rstrip("/")
        if "{date}" in generic
        else generic.rstrip("/")
    )
    if not prefix:
        return None
    return _resolve_spec(target, prefix)


def _configured_timezone(config: Mapping[str, Any]) -> ZoneInfo:
    backup = config.get("backup") if isinstance(config.get("backup"), Mapping) else {}
    name = str((backup or {}).get("timezone") or "Europe/Berlin")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _parse_point_time(name: str, display_timezone: ZoneInfo) -> float | None:
    value = name.rstrip("/")
    for pattern in ("%Y-%m-%dT%H-%M-%S", "%Y-%m-%d_%H-%M-%S", "%Y-%m-%d"):
        try:
            return (
                datetime.strptime(value, pattern)
                .replace(tzinfo=display_timezone)
                .timestamp()
            )
        except ValueError:
            continue
    return None


def _remote_directories(root: str) -> list[str]:
    result = subprocess.run(
        [
            "rclone",
            "lsf",
            "--dirs-only",
            "--max-depth",
            "1",
            "--format",
            "p",
            "--",
            root,
        ],
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
        env=rclone_subprocess_env(),
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[:400]
        raise RecoveryPointError(
            f"Versionsablage konnte nicht gelesen werden: {detail or result.returncode}"
        )
    return [
        line.strip().rstrip("/") for line in result.stdout.splitlines() if line.strip()
    ]


def list_points(
    config: Mapping[str, Any], pair: Mapping[str, Any]
) -> list[dict[str, Any]]:
    root = version_root(config, pair)
    result: list[dict[str, Any]] = [
        {
            "id": "current",
            "label": "Aktueller Sicherungsstand",
            "created_at": None,
            "kind": "current",
        }
    ]
    if not root:
        return result
    if _is_remote(root):
        names = _remote_directories(root)
    else:
        directory = Path(root).expanduser()
        if not directory.is_dir():
            return result
        names = [item.name for item in directory.iterdir() if item.is_dir()]
    display_timezone = _configured_timezone(config)
    points = []
    for name in names:
        if not name or name in {".", ".."} or "/" in name or "\\" in name:
            continue
        created_at = _parse_point_time(name, display_timezone)
        points.append(
            {
                "id": name,
                "label": (
                    datetime.fromtimestamp(created_at, display_timezone).strftime(
                        "%d.%m.%Y · %H:%M"
                    )
                    if created_at is not None
                    else name
                ),
                "created_at": created_at,
                "kind": "version",
            }
        )
    points.sort(
        key=lambda item: (float(item.get("created_at") or 0), str(item["id"])),
        reverse=True,
    )
    return result + points[:MAX_POINTS]


def point_target(
    config: Mapping[str, Any], pair: Mapping[str, Any], point_id: str
) -> str:
    value = str(point_id or "current")
    if value == "current":
        return _endpoints(pair)[1]
    known = {
        str(item["id"]) for item in list_points(config, pair) if item["id"] != "current"
    }
    if value not in known:
        raise RecoveryPointError("Recovery-Punkt nicht gefunden")
    root = version_root(config, pair)
    if not root:
        raise RecoveryPointError("Für diesen Datenweg gibt es keine Versionsablage")
    return _join(root, value)


def browse_point(
    config: Mapping[str, Any],
    pair: Mapping[str, Any],
    point_id: str,
    relative_path: str,
) -> dict[str, Any]:
    root = point_target(config, pair, point_id)
    relative = _safe_relative(relative_path)
    target = _join(root, relative)
    if _is_remote(target):
        result = subprocess.run(
            ["rclone", "lsjson", "--max-depth", "1", "--", target],
            capture_output=True,
            text=True,
            timeout=60,
            stdin=subprocess.DEVNULL,
            env=rclone_subprocess_env(),
        )
        if result.returncode != 0:
            raise RecoveryPointError("Recovery-Punkt konnte nicht gelesen werden")
        try:
            entries = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise RecoveryPointError("Recovery-Punkt lieferte ungültige Daten") from exc
        items = [
            {
                "name": str(item.get("Name") or ""),
                "path": _join(relative, str(item.get("Name") or "")),
                "is_dir": bool(item.get("IsDir")),
                "size": item.get("Size"),
                "modified_at": item.get("ModTime"),
            }
            for item in entries
            if isinstance(item, Mapping)
            and str(item.get("Name") or "") not in {"", ".", ".."}
        ]
    else:
        base = Path(root).expanduser().resolve()
        directory = (base / relative).resolve()
        if directory != base and not directory.is_relative_to(base):
            raise RecoveryPointError("Pfad liegt außerhalb des Recovery-Punkts")
        if not directory.is_dir():
            raise RecoveryPointError("Ordner nicht gefunden")
        items = []
        for item in sorted(
            directory.iterdir(),
            key=lambda entry: (not entry.is_dir(), entry.name.casefold()),
        )[:500]:
            stat = item.stat()
            items.append(
                {
                    "name": item.name,
                    "path": _join(relative, item.name),
                    "is_dir": item.is_dir(),
                    "size": stat.st_size if item.is_file() else None,
                    "modified_at": stat.st_mtime,
                }
            )
    return {"point_id": point_id, "path": relative, "items": items}


def _local_inventory(root: str) -> tuple[dict[str, tuple[int, str]], bool]:
    base = Path(root).expanduser().resolve()
    if not base.is_dir():
        raise RecoveryPointError("Recovery-Punkt ist nicht erreichbar")
    inventory: dict[str, tuple[int, str]] = {}
    truncated = False
    for current, directories, filenames in os.walk(base):
        directories.sort(key=str.casefold)
        filenames.sort(key=str.casefold)
        for filename in filenames:
            path = Path(current) / filename
            try:
                stat = path.stat()
            except OSError:
                continue
            relative = path.relative_to(base).as_posix()
            fingerprint = f"mtime:{stat.st_mtime_ns}"
            inventory[relative] = (stat.st_size, fingerprint)
            if len(inventory) >= MAX_DIFF_FILES:
                truncated = True
                return inventory, truncated
    return inventory, truncated


def _remote_inventory(root: str) -> tuple[dict[str, tuple[int, str]], bool]:
    process = subprocess.Popen(
        [
            "rclone",
            "lsf",
            "--recursive",
            "--files-only",
            "--format",
            "psh",
            "--separator",
            "\t",
            "--",
            root,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        stdin=subprocess.DEVNULL,
        env=rclone_subprocess_env(),
    )
    inventory: dict[str, tuple[int, str]] = {}
    truncated = False
    assert process.stdout is not None
    for raw in process.stdout:
        parts = raw.rstrip("\r\n").split("\t", 2)
        if len(parts) < 2:
            continue
        try:
            size = int(parts[1])
        except ValueError:
            continue
        inventory[parts[0].rstrip("/")] = (size, parts[2] if len(parts) > 2 else "")
        if len(inventory) >= MAX_DIFF_FILES:
            truncated = True
            process.terminate()
            break
    try:
        _stdout, stderr = process.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        _stdout, stderr = process.communicate()
    if process.returncode != 0 and not truncated:
        detail = (stderr or "").strip()[:400]
        raise RecoveryPointError(
            f"Recovery-Punkt konnte nicht verglichen werden: {detail or process.returncode}"
        )
    return inventory, truncated


def _inventory(target: str) -> tuple[dict[str, tuple[int, str]], bool]:
    return _remote_inventory(target) if _is_remote(target) else _local_inventory(target)


def compare_points(
    config: Mapping[str, Any],
    pair: Mapping[str, Any],
    from_point: str,
    to_point: str,
) -> dict[str, Any]:
    source, source_truncated = _inventory(point_target(config, pair, from_point))
    destination, destination_truncated = _inventory(
        point_target(config, pair, to_point)
    )
    source_paths = set(source)
    destination_paths = set(destination)
    added = sorted(destination_paths - source_paths, key=str.casefold)
    removed = sorted(source_paths - destination_paths, key=str.casefold)
    changed = sorted(
        (
            path
            for path in source_paths & destination_paths
            if source[path][0] != destination[path][0]
            or (
                source[path][1]
                and destination[path][1]
                and source[path][1] != destination[path][1]
            )
        ),
        key=str.casefold,
    )

    def entries(
        paths: list[str], values: Mapping[str, tuple[int, str]]
    ) -> list[dict[str, Any]]:
        return [
            {"path": path, "size": values[path][0]} for path in paths[:MAX_DIFF_RESULTS]
        ]

    return {
        "from_point": from_point,
        "to_point": to_point,
        "added": entries(added, destination),
        "removed": entries(removed, source),
        "changed": [
            {
                "path": path,
                "from_size": source[path][0],
                "to_size": destination[path][0],
            }
            for path in changed[:MAX_DIFF_RESULTS]
        ],
        "counts": {
            "added": len(added),
            "removed": len(removed),
            "changed": len(changed),
        },
        "truncated": source_truncated
        or destination_truncated
        or any(len(items) > MAX_DIFF_RESULTS for items in (added, removed, changed)),
    }


__all__ = [
    "RecoveryPointError",
    "browse_point",
    "compare_points",
    "list_points",
    "point_target",
    "version_root",
]
