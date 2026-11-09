"""Interaktiver Time-Travel-Browser und Versions-Diff auf Basis von ``recovery_points``."""

from __future__ import annotations

import os
import stat
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from .rclone_args import rclone_subprocess_env
from .recovery_points import (
    MAX_BROWSE_ITEMS,
    REMOTE_TIMEOUT_SECONDS,
    RecoveryPointError,
    _is_link_stat,
    _is_remote,
    _join,
    _safe_relative,
    browse_point,
    list_points,
    point_target,
)
from .text_diff import SAMPLE_BYTES, diff_rows, is_probably_text

MAX_FILE_BYTES = 1024 * 1024


def _point_side(point_id: str) -> str:
    return "remote" if str(point_id or "").startswith("remote:") else "local"


def _try_browse_delta(
    config: Mapping[str, Any],
    pair: Mapping[str, Any],
    point_id: str,
    relative: str,
) -> tuple[list[dict[str, Any]] | None, bool]:
    try:
        listing = browse_point(config, pair, point_id, relative)
        return list(listing.get("items") or []), bool(listing.get("truncated"))
    except RecoveryPointError as exc:
        msg = str(exc).lower()
        if "unsicher" in msg or "ungültig" in msg or "während des lesens" in msg:
            raise
        return None, False


def browse_virtual_point(
    config: Mapping[str, Any],
    pair: Mapping[str, Any],
    point_id: str = "current",
    relative_path: str = "",
) -> dict[str, Any]:
    relative = _safe_relative(relative_path)
    points = list_points(config, pair)
    by_id = {str(p["id"]): p for p in points}
    if point_id not in by_id:
        raise RecoveryPointError("Recovery-Punkt nicht gefunden")

    pair_name = str(pair.get("name") or "")
    if point_id in {"current", "current-remote"} or point_id.startswith("full-"):
        listing = browse_point(config, pair, point_id, relative)
        items = [
            {
                **item,
                "source_point": point_id,
                "status_at_point": "live",
            }
            for item in (listing.get("items") or [])
        ]
        return {
            "pair": pair_name,
            "point_id": point_id,
            "path": relative,
            "virtual": False,
            "items": items,
            "truncated": bool(listing.get("truncated")),
        }

    side = _point_side(point_id)
    bisync = str(pair.get("direction") or "bisync").lower().strip() == "bisync"
    live_point_id = "current-remote" if (side == "remote" and bisync) else "current"
    target_point = by_id[point_id]
    target_created_at = target_point.get("created_at")

    side_versions = [
        p
        for p in points
        if p.get("kind") == "version" and _point_side(str(p["id"])) == side
    ]
    target_idx = next(
        (i for i, p in enumerate(side_versions) if str(p["id"]) == point_id),
        0,
    )
    # side_versions ist absteigend (neueste zuerst) sortiert; die nach point_id
    # liegenden neueren Versionen werden von alt nach neu durchlaufen.
    newer_points = list(reversed(side_versions[:target_idx]))

    merged: dict[str, dict[str, Any]] = {}
    truncated = False
    any_found = False

    target_items, target_trunc = _try_browse_delta(config, pair, point_id, relative)
    if target_items is not None:
        any_found = True
        truncated = truncated or target_trunc
        for item in target_items:
            name = str(item.get("name") or "")
            if name:
                merged[name] = {
                    **item,
                    "source_point": point_id,
                    "status_at_point": "archived",
                }

    for newer in newer_points:
        newer_id = str(newer["id"])
        newer_items, newer_trunc = _try_browse_delta(config, pair, newer_id, relative)
        if newer_items is None:
            continue
        any_found = True
        truncated = truncated or newer_trunc
        for item in newer_items:
            name = str(item.get("name") or "")
            if name and name not in merged:
                merged[name] = {
                    **item,
                    "source_point": newer_id,
                    "status_at_point": "displaced_later",
                }

    live_items, live_trunc = _try_browse_delta(config, pair, live_point_id, relative)
    if live_items is not None:
        any_found = True
        truncated = truncated or live_trunc
        for item in live_items:
            name = str(item.get("name") or "")
            if not name or name in merged:
                continue
            is_dir = bool(item.get("is_dir"))
            mod_time = item.get("modified_at")
            if (
                not is_dir
                and isinstance(mod_time, (int, float))
                and isinstance(target_created_at, (int, float))
                and float(mod_time) > float(target_created_at) + 60.0
            ):
                continue
            merged[name] = {
                **item,
                "source_point": live_point_id,
                "status_at_point": "unchanged",
            }

    if relative and not any_found:
        raise RecoveryPointError("Ordner nicht gefunden")

    sorted_items = sorted(
        merged.values(),
        key=lambda item: (
            not bool(item.get("is_dir")),
            str(item.get("name") or "").casefold(),
        ),
    )
    if len(sorted_items) > MAX_BROWSE_ITEMS:
        truncated = True
        sorted_items = sorted_items[:MAX_BROWSE_ITEMS]

    return {
        "pair": pair_name,
        "point_id": point_id,
        "path": relative,
        "virtual": True,
        "items": sorted_items,
        "truncated": truncated,
    }


def file_version_history(
    config: Mapping[str, Any],
    pair: Mapping[str, Any],
    relative_path: str,
    *,
    max_points: int = 30,
) -> dict[str, Any]:
    relative = _safe_relative(relative_path)
    if not relative:
        raise RecoveryPointError("Dateipfad fehlt")

    posix = PurePosixPath(relative)
    parent = "" if str(posix.parent) == "." else posix.parent.as_posix()
    filename = posix.name

    points = list_points(config, pair)[: max(1, int(max_points))]
    versions: list[dict[str, Any]] = []

    for point in points:
        pid = str(point["id"])
        items, _trunc = _try_browse_delta(config, pair, pid, parent)
        if not items:
            continue
        match = next(
            (
                item
                for item in items
                if str(item.get("name") or "") == filename and not item.get("is_dir")
            ),
            None,
        )
        if match is not None:
            versions.append(
                {
                    "point_id": pid,
                    "label": str(point.get("label") or pid),
                    "kind": str(point.get("kind") or "version"),
                    "created_at": point.get("created_at"),
                    "size": match.get("size"),
                    "modified_at": match.get("modified_at"),
                }
            )

    return {
        "pair": str(pair.get("name") or ""),
        "path": relative,
        "versions": versions,
    }


def _read_point_text(
    root: str,
    rel: str,
    *,
    run: Callable[..., Any] = subprocess.run,
) -> tuple[str, dict[str, Any]]:
    if _is_remote(root):
        target = _join(root, rel)
        try:
            completed = run(
                ["rclone", "cat", "--", target],
                env=rclone_subprocess_env(),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=REMOTE_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            raise RecoveryPointError(
                "Datei im Recovery-Punkt hat das Zeitlimit überschritten"
            ) from exc
        except OSError as exc:
            raise RecoveryPointError(
                "Datei im Recovery-Punkt konnte nicht gelesen werden"
            ) from exc

        if getattr(completed, "returncode", 1) != 0:
            raise RecoveryPointError("Datei im Recovery-Punkt nicht gefunden")

        raw_out = getattr(completed, "stdout", b"")
        data = (
            raw_out
            if isinstance(raw_out, bytes)
            else str(raw_out or "").encode("utf-8")
        )
        if len(data) > MAX_FILE_BYTES:
            raise RecoveryPointError("Datei ist für den Vergleich zu groß (max. 1 MiB)")
        if not is_probably_text(data[:SAMPLE_BYTES]):
            raise RecoveryPointError("Nur Textdateien können verglichen werden")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RecoveryPointError(
                "Nur Textdateien können verglichen werden"
            ) from exc
        return text, {"size": len(data), "mtime": None}

    base = Path(root).expanduser().absolute()
    current = Path(base.anchor)
    st_info: os.stat_result | None = None
    for part in (None, *base.parts[1:], *PurePosixPath(rel).parts):
        if part is not None:
            current = current / part
        try:
            st_info = current.lstat()
        except FileNotFoundError as exc:
            raise RecoveryPointError("Datei im Recovery-Punkt nicht gefunden") from exc
        except OSError as exc:
            raise RecoveryPointError(
                "Datei im Recovery-Punkt konnte nicht gelesen werden"
            ) from exc
        if _is_link_stat(st_info):
            raise RecoveryPointError("Ungültiger relativer Pfad")

    real_root = os.path.realpath(base)
    real_target = os.path.realpath(current)
    if not Path(real_target).is_relative_to(real_root):
        raise RecoveryPointError("Ungültiger relativer Pfad")

    assert st_info is not None
    if not stat.S_ISREG(st_info.st_mode):
        raise RecoveryPointError("Datei im Recovery-Punkt nicht gefunden")

    try:
        with open(real_target, "rb") as handle:
            fstat = os.fstat(handle.fileno())
            data = handle.read(MAX_FILE_BYTES + 1)
    except FileNotFoundError as exc:
        raise RecoveryPointError("Datei im Recovery-Punkt nicht gefunden") from exc
    except OSError as exc:
        raise RecoveryPointError(
            "Datei im Recovery-Punkt konnte nicht gelesen werden"
        ) from exc

    if len(data) > MAX_FILE_BYTES or fstat.st_size > MAX_FILE_BYTES:
        raise RecoveryPointError("Datei ist für den Vergleich zu groß (max. 1 MiB)")
    if not is_probably_text(data[:SAMPLE_BYTES]):
        raise RecoveryPointError("Nur Textdateien können verglichen werden")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RecoveryPointError("Nur Textdateien können verglichen werden") from exc
    return text, {"size": fstat.st_size, "mtime": fstat.st_mtime}


def diff_point_files(
    config: Mapping[str, Any],
    pair: Mapping[str, Any],
    relative_path: str,
    from_point: str,
    to_point: str = "current",
    *,
    run: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    relative = _safe_relative(relative_path)
    if not relative:
        raise RecoveryPointError("Dateipfad fehlt")

    left_root = point_target(config, pair, from_point)
    right_root = point_target(config, pair, to_point)
    left_text, left_meta = _read_point_text(left_root, relative, run=run)
    right_text, right_meta = _read_point_text(right_root, relative, run=run)

    return {
        "pair": str(pair.get("name") or ""),
        "path": relative,
        "from_point": from_point,
        "to_point": to_point,
        "left": left_meta,
        "right": right_meta,
        **diff_rows(left_text, right_text),
    }


__all__ = [
    "MAX_FILE_BYTES",
    "browse_virtual_point",
    "diff_point_files",
    "file_version_history",
]
