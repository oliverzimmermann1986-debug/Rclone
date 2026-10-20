"""Browse and compare the version folders created by rclone ``--backup-dir``."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .jobs.restore_test import _endpoints
from .rclone_args import rclone_subprocess_env

MAX_POINTS = 180
MAX_DIFF_FILES = 20_000
MAX_DIFF_RESULTS = 750
MAX_BROWSE_ITEMS = 500
MAX_BROWSE_SCAN = 10_000


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
    raw = str(value or "").replace("\\", "/").strip()
    candidate = PurePosixPath(raw)
    if (
        candidate.is_absolute()
        or ".." in candidate.parts
        or ":" in raw
        or any(char in raw for char in ("\x00", "\r", "\n"))
    ):
        raise RecoveryPointError("Ungültiger relativer Pfad")
    return "" if raw in {"", "."} else candidate.as_posix()


def _local_browse(root: str, relative: str) -> tuple[list[dict[str, Any]], bool]:
    """Read bounded metadata once, never following an entry's symlink.

    POSIX traversal and scanning stay anchored to no-follow directory handles.
    Other platforms reject links/reparse points and check directory identities;
    they do not provide the same descriptor-relative race guarantee.
    """
    relative = _safe_relative(relative)
    # Do not resolve a root that may have been replaced since point_target().
    # Every component, including the root itself, must pass the no-follow walk.
    base = Path(root).expanduser().absolute()
    items = []
    truncated = False
    try:
        with ExitStack() as stack:
            anchored = (
                os.open in os.supports_dir_fd
                and os.scandir in os.supports_fd
                and hasattr(os, "O_NOFOLLOW")
                and hasattr(os, "O_DIRECTORY")
            )
            if anchored:
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                flags |= getattr(os, "O_CLOEXEC", 0)
                directory = os.open(base.anchor, flags)
                stack.callback(os.close, directory)
                for part in (*base.parts[1:], *PurePosixPath(relative).parts):
                    directory = os.open(part, flags, dir_fd=directory)
                    stack.callback(os.close, directory)
                scan_target = directory
                identities = []
            else:
                directory = Path(base.anchor)
                identities = []
                for part in (None, *base.parts[1:], *PurePosixPath(relative).parts):
                    if part is not None:
                        directory = directory / part
                    info = directory.lstat()
                    if not stat.S_ISDIR(info.st_mode) or _is_link_stat(info):
                        raise RecoveryPointError("Unsicherer Ordner im Recovery-Punkt")
                    identities.append((directory, info))
                scan_target = directory
            entries = stack.enter_context(os.scandir(scan_target))
            for index, entry in enumerate(entries):
                if index >= MAX_BROWSE_SCAN:
                    truncated = True
                    break
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                is_dir = stat.S_ISDIR(info.st_mode)
                is_file = stat.S_ISREG(info.st_mode)
                if _is_link_stat(info) or not (is_dir or is_file):
                    continue
                items.append(
                    {
                        "name": entry.name,
                        "path": f"{relative}/{entry.name}" if relative else entry.name,
                        "is_dir": is_dir,
                        "size": info.st_size if is_file else None,
                        "modified_at": info.st_mtime,
                    }
                )
            for directory, previous in identities:
                current = directory.lstat()
                if _is_link_stat(current) or (
                    current.st_dev,
                    current.st_ino,
                    current.st_mode,
                ) != (previous.st_dev, previous.st_ino, previous.st_mode):
                    raise RecoveryPointError(
                        "Recovery-Ordner wurde während des Lesens geändert"
                    )
    except FileNotFoundError as exc:
        raise RecoveryPointError("Ordner nicht gefunden") from exc
    except OSError as exc:
        raise RecoveryPointError(
            "Recovery-Ordner konnte nicht sicher gelesen werden"
        ) from exc
    items.sort(key=lambda item: (not item["is_dir"], item["name"].casefold()))
    return items[:MAX_BROWSE_ITEMS], truncated or len(items) > MAX_BROWSE_ITEMS


def _is_link_stat(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


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
    from .recovery_snapshots import SnapshotError, list_snapshots

    root = version_root(config, pair)
    result: list[dict[str, Any]] = [
        {
            "id": "current",
            "label": "Aktueller Sicherungsstand",
            "created_at": None,
            "kind": "current",
            "complete": False,
        }
    ]
    try:
        complete = list_snapshots(config, pair)
    except SnapshotError as exc:
        raise RecoveryPointError(str(exc)) from exc
    display_timezone = _configured_timezone(config)
    for point in complete:
        point["label"] = (
            datetime.fromtimestamp(point["created_at"], display_timezone).strftime(
                "%d.%m.%Y · %H:%M"
            )
            + " · Vollständig"
        )
    result.extend(complete)
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
                "complete": False,
                "warning": "Änderungsarchiv: enthält nur ersetzte oder gelöschte Dateien, keinen vollständigen Stand.",
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
    if value.startswith("full-"):
        from .recovery_snapshots import SnapshotError, snapshot_target

        try:
            return snapshot_target(config, pair, value)
        except SnapshotError as exc:
            raise RecoveryPointError(str(exc)) from exc
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
    truncated = False
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
        if not isinstance(entries, list):
            raise RecoveryPointError("Recovery-Punkt lieferte ungültige Daten")
        truncated = len(entries) > MAX_BROWSE_ITEMS
        items = [
            {
                "name": str(item.get("Name") or ""),
                "path": _join(relative, str(item.get("Name") or "")),
                "is_dir": bool(item.get("IsDir")),
                "size": item.get("Size"),
                "modified_at": item.get("ModTime"),
            }
            for item in entries[:MAX_BROWSE_ITEMS]
            if isinstance(item, Mapping)
            and str(item.get("Name") or "") not in {"", ".", ".."}
        ]
    else:
        items, truncated = _local_browse(root, relative)
    return {
        "point_id": point_id,
        "path": relative,
        "items": items,
        "truncated": truncated,
    }


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
    comparing_full = from_point.startswith("full-") or to_point.startswith("full-")

    def inventory(point: str) -> tuple[dict[str, tuple[int, str]], bool]:
        if not comparing_full:
            return _inventory(point_target(config, pair, point))
        from .recovery_snapshots import (
            MAX_BYTES,
            SnapshotError,
            _inventory_local,
            _inventory_remote as remote_snapshot_inventory,
            load_manifest,
            snapshot_root,
        )

        try:
            if point.startswith("full-"):
                manifest = load_manifest(config, pair, point)
                return {
                    item["path"]: (item["size"], item["sha256"])
                    for item in manifest["entries"]
                }, False
            target = point_target(config, pair, point)
            if _is_remote(target):
                entries, _dirs = remote_snapshot_inventory(
                    target, snapshot_root(config), MAX_BYTES
                )
                # Provider hashes may be MD5/SHA1; never compare them to SHA-256.
                return {
                    name: (value["size"], "") for name, value in entries.items()
                }, False
            files, _directories = _inventory_local(
                Path(target).expanduser().resolve(), limit_bytes=MAX_BYTES, hashes=True
            )
            return {
                name: (value["size"], value["sha256"]) for name, value in files.items()
            }, False
        except SnapshotError as exc:
            raise RecoveryPointError(str(exc)) from exc

    source, source_truncated = inventory(from_point)
    destination, destination_truncated = inventory(to_point)
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
    unverified = sorted(
        path
        for path in source_paths & destination_paths
        if comparing_full
        and source[path][0] == destination[path][0]
        and not (source[path][1] and destination[path][1])
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
        "unverified": entries(unverified, destination),
        "verification": "sha256" if comparing_full and not unverified else "metadata",
        "warning": (
            "Für gleich große Dateien fehlen vergleichbare Cloud-Prüfsummen. Diese Dateien sind nicht als unverändert bestätigt."
            if unverified
            else None
        ),
        "counts": {
            "added": len(added),
            "removed": len(removed),
            "changed": len(changed),
        },
        "truncated": source_truncated
        or destination_truncated
        or bool(unverified)
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
