"""Bounded metadata search across recovery points; never downloads file contents."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import stat
import time
from collections import deque
from contextlib import ExitStack, closing
from pathlib import Path
from typing import Any, Mapping

from . import recovery_points as points
from .recovery_snapshots import (
    POINT_RE,
    SnapshotError,
    load_manifest,
    snapshot_root,
    snapshot_target,
)

MAX_SEARCH_PAIRS = 32
MAX_SEARCH_POINTS = 40
MAX_SEARCH_FILES = 20_000
SEARCH_SECONDS = 15


def _available_points(
    config: Mapping[str, Any], pair: Mapping[str, Any], offset: int = 0
) -> tuple[list[dict], bool]:
    """Read a finite slice of point descriptors; manifests are loaded later."""
    from itertools import islice

    bisync = str(pair.get("direction") or "bisync").lower() == "bisync"

    def descriptors():
        yield {
            "id": "current",
            "kind": "current",
            "label": "Aktueller Sicherungsstand",
            "_target": points._endpoints(pair)[1],
        }
        root = snapshot_root(config)
        if root.is_dir():
            with os.scandir(root) as entries:
                for index, entry in enumerate(entries):
                    if index >= 100_000:
                        raise ValueError("Zu viele Recovery-Punkt-Verzeichnisse")
                    if POINT_RE.fullmatch(entry.name) and not entry.is_symlink():
                        yield {
                            "id": entry.name,
                            "kind": "full",
                            "label": "Vollständiger Stand",
                        }
        for side in ("local", "remote") if bisync else ("local",):
            if side == "remote":
                yield {
                    "id": "current-remote",
                    "kind": "current",
                    "label": "Remote-Live-Seite (Bi-Sync)",
                    "_target": str(pair.get("remote") or ""),
                }
            archive = points.version_root(config, pair, side=side)
            if not archive:
                continue
            if points._is_remote(archive):
                names = points._remote_directories(archive)
            else:
                directory = Path(archive)
                if not directory.is_dir() or directory.is_symlink():
                    continue
                names = []
                with os.scandir(directory) as entries:
                    for index, entry in enumerate(entries):
                        if index >= 100_000:
                            raise ValueError("Zu viele Änderungsarchive")
                        if entry.is_dir(follow_symlinks=False):
                            names.append(entry.name)
            for name in sorted(names, reverse=True):
                if name and "/" not in name and points._safe_relative(name) == name:
                    yield {
                        "id": f"{side}:{name}" if bisync else name,
                        "kind": "version",
                        "label": name,
                        "_target": points._join(archive, name),
                    }

    window = list(islice(descriptors(), offset, offset + MAX_SEARCH_POINTS + 1))
    return window[:MAX_SEARCH_POINTS], len(window) > MAX_SEARCH_POINTS


def _metadata_entries(root: str, relative: str) -> tuple[list[dict], bool]:
    if not points._is_remote(root):
        return points._local_browse(root, relative)
    target = points._join(root, relative)
    rows, truncated = points._remote_listing(
        ["rclone", "lsjson", "--max-depth", "1", "--", target]
    )
    result = []
    for row in rows:
        name = str(row.get("Name") or "")
        if not name or name in {".", ".."} or "/" in name or "\\" in name:
            continue
        path = points._safe_relative(f"{relative}/{name}" if relative else name)
        if row.get("IsLink"):
            continue
        result.append(
            {
                "path": path,
                "is_dir": bool(row.get("IsDir")),
                "size": row.get("Size"),
                "modified_at": row.get("ModTime"),
            }
        )
    return result, truncated


def _point_entries(root: str):
    """Stream metadata beyond a single 500-entry browser page, with hard bounds."""
    if points._is_remote(root):
        command = [
            "rclone",
            "lsf",
            "--recursive",
            "--files-only",
            "--format",
            "ps",
            "--separator",
            "\t",
            "--",
            root,
        ]
        with closing(points._remote_lines(command)) as lines:
            for index, raw in enumerate(lines):
                if index >= 100_000:
                    raise ValueError("Dateikatalog überschreitet das Metadatenlimit")
                path, separator, size = raw.rstrip("\r\n").rpartition("\t")
                if not separator or any(ord(c) < 32 for c in path):
                    raise ValueError("Ungültige Dateimetadaten")
                size = int(size)
                if size < 0:
                    raise ValueError("Unbekannte Dateigröße")
                yield {"path": path, "size": size, "modified_at": None}
        return
    anchored = (
        os.open in os.supports_dir_fd
        and os.scandir in os.supports_fd
        and hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "O_DIRECTORY")
    )
    if not anchored:
        pending = deque([""])
        while pending:
            rows, truncated = _metadata_entries(root, pending.popleft())
            for row in rows:
                if row.get("is_dir"):
                    pending.append(row["path"])
                else:
                    yield row
            if truncated:
                raise ValueError("Metadatenfenster auf dieser Plattform begrenzt")
        return
    base = Path(root).expanduser().absolute()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    scanned = 0

    def walk(directory, relative, depth):
        nonlocal scanned
        if depth > 64:
            raise ValueError("Dateikatalog überschreitet die Verzeichnistiefe")
        with os.scandir(directory) as entries:
            for entry in entries:
                scanned += 1
                if scanned > 100_000:
                    raise ValueError("Dateikatalog überschreitet das Metadatenlimit")
                info = entry.stat(follow_symlinks=False)
                if points._is_link_stat(info):
                    continue
                path = f"{relative}/{entry.name}" if relative else entry.name
                if stat.S_ISREG(info.st_mode):
                    yield {
                        "path": path,
                        "size": info.st_size,
                        "modified_at": info.st_mtime,
                    }
                elif stat.S_ISDIR(info.st_mode):
                    child = os.open(entry.name, flags, dir_fd=directory)
                    try:
                        yield from walk(child, path, depth + 1)
                    finally:
                        os.close(child)

    with ExitStack() as stack:
        directory = os.open(base.anchor, flags)
        stack.callback(os.close, directory)
        for component in base.parts[1:]:
            directory = os.open(component, flags, dir_fd=directory)
            stack.callback(os.close, directory)
        yield from walk(directory, "", 0)


_CURSOR_SECRET = secrets.token_bytes(32)


class CatalogCursorError(ValueError):
    pass


def _cursor_key(config):
    return (
        str((config.get("web") or {}).get("secret_key") or "").encode()
        or _CURSOR_SECRET
    )


def _encode_cursor(config, binding, state):
    value = {**state, "binding": binding, "expires": int(time.time()) + 1800}
    raw = json.dumps(value, separators=(",", ":")).encode()
    signature = hmac.new(_cursor_key(config), raw, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(raw + signature).decode().rstrip("=")


def _decode_cursor(config, binding, token):
    if not token:
        return {"pair": 0, "point": 0, "file": 0}
    try:
        if len(token) > 4096:
            raise ValueError()
        decoded = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        raw, signature = decoded[:-32], decoded[-32:]
        if not hmac.compare_digest(
            signature, hmac.new(_cursor_key(config), raw, hashlib.sha256).digest()
        ):
            raise ValueError()
        value = json.loads(raw)
        if value["binding"] != binding or value["expires"] < time.time():
            raise ValueError()
        state = {k: value[k] for k in ("pair", "point", "file")}
        if any(
            not isinstance(v, int) or isinstance(v, bool) or v < 0 or v > 1_000_000
            for v in state.values()
        ):
            raise ValueError()
        return state
    except (ValueError, KeyError, TypeError) as exc:
        raise CatalogCursorError(
            "Suchfortsetzung ist ungültig, abgelaufen oder gehört zu einer anderen Konfiguration/Suche"
        ) from exc


def search_files(
    config: Mapping[str, Any],
    query: str,
    *,
    identity: str = "",
    limit: int = 50,
    cursor: str = "",
    revision: str = "",
) -> dict[str, Any]:
    """Page through bounded metadata windows; no file content is downloaded.

    Pagination is bound to configuration and query. Live directory changes can
    reorder entries, so every page includes its actual coverage and timestamp.
    Full immutable manifests have stable file offsets.
    """
    needle = str(query or "").strip().casefold()
    if len(needle) < 2 or len(needle) > 120 or any(ord(c) < 32 for c in needle):
        raise ValueError("Suchbegriff muss 2 bis 120 Zeichen enthalten")
    limit = max(1, min(int(limit), 200))
    pairs = [
        p
        for p in (config.get("backup") or {}).get("pairs", [])
        if isinstance(p, Mapping)
        and (
            not identity
            or identity in {str(p.get("id") or ""), str(p.get("name") or "")}
        )
    ]
    if identity and not pairs:
        raise LookupError("Datenweg nicht gefunden")
    binding = hashlib.sha256(
        json.dumps(
            [needle, identity, revision or config], sort_keys=True, default=str
        ).encode()
    ).hexdigest()
    state = _decode_cursor(config, binding, cursor)
    result = {
        "items": [],
        "scanned_points": 0,
        "scanned_files": 0,
        "truncated": False,
        "warnings": [],
        "coverage": [],
        "next_cursor": None,
        "generated_at": time.time(),
    }
    warnings = set()
    deadline = time.monotonic() + SEARCH_SECONDS
    visited_pairs = 0

    def page():
        result["next_cursor"] = _encode_cursor(config, binding, state)
        result["truncated"] = True

    def exhausted():
        return (
            result["scanned_files"] >= MAX_SEARCH_FILES
            or len(result["items"]) >= limit
            or time.monotonic() >= deadline
        )

    while state["pair"] < len(pairs):
        if visited_pairs >= MAX_SEARCH_PAIRS:
            page()
            break
        visited_pairs += 1
        pair = pairs[state["pair"]]
        pair_id = str(pair.get("id") or pair.get("name") or "")
        try:
            available, more_points = _available_points(config, pair, state["point"])
        except (ValueError, OSError):
            warnings.add(
                f"Recovery-Punkte für {pair.get('name') or pair_id} nicht vollständig erreichbar."
            )
            result["truncated"] = True
            state = {"pair": state["pair"] + 1, "point": 0, "file": 0}
            continue
        for point in available:
            if exhausted() or result["scanned_points"] >= MAX_SEARCH_POINTS:
                page()
                break
            point_id = str(point["id"])
            full = point_id.startswith("full-")
            coverage = {
                "identity": pair_id,
                "point_id": point_id,
                "point_kind": point.get("kind"),
                "complete": False,
                "truncated": False,
            }
            result["coverage"].append(coverage)
            result["scanned_points"] += 1
            if point.get("kind") == "version":
                warnings.add(
                    "Änderungsarchive enthalten nur ersetzte/gelöschte Dateien, keinen vollständigen Stand."
                )
            try:
                if full:
                    manifest = load_manifest(config, pair, point_id)
                    snapshot_target(config, pair, point_id)
                else:
                    root = point.get("_target") or points.point_target(
                        config, pair, point_id
                    )
                stream = (
                    iter(manifest["entries"]) if full else iter(_point_entries(root))
                )
                file_index = 0
                try:
                    for entry in stream:
                        relative = points._safe_relative(str(entry.get("path") or ""))
                        if not relative or relative != str(entry.get("path") or ""):
                            raise ValueError("Unsicherer Dateipfad im Suchergebnis")
                        file_index += 1
                        if file_index <= state["file"]:
                            if time.monotonic() >= deadline:
                                raise ValueError(
                                    "Metadatenfortsetzung überschreitet das Zeitlimit"
                                )
                            continue
                        if exhausted():
                            coverage["truncated"] = True
                            page()
                            break
                        state["file"] = file_index
                        result["scanned_files"] += 1
                        if needle in relative.casefold():
                            result["items"].append(
                                {
                                    "identity": pair_id,
                                    "pair": str(pair.get("name") or pair_id),
                                    "point_id": point_id,
                                    "point_kind": point.get("kind"),
                                    "point_label": str(point.get("label") or point_id),
                                    "path": relative,
                                    "size_bytes": entry.get("size"),
                                    "modified_at": (
                                        entry.get("modified", 0) / 1_000_000_000
                                        if full
                                        and isinstance(entry.get("modified"), int)
                                        else entry.get("modified_at")
                                    ),
                                    "verification": "manifest_sha256"
                                    if full
                                    else "metadata_only",
                                    "restorable": True,
                                    "historical_binding": bool(
                                        full and manifest.get("historical_binding")
                                    ),
                                    "current_restore_proof": False,
                                    "restore_request": {
                                        "identity": pair_id,
                                        "point_id": point_id,
                                        "paths": [relative],
                                    },
                                }
                            )
                finally:
                    close = getattr(stream, "close", None)
                    if close:
                        close()
                coverage["complete"] = bool(
                    full and not coverage["truncated"] and not cursor
                )
            except (ValueError, OSError, SnapshotError):
                coverage["truncated"] = True
                warnings.add(
                    f"Punkt {point.get('label') or point_id} konnte nicht vollständig durchsucht werden."
                )
            result["truncated"] |= coverage["truncated"]
            if result["next_cursor"]:
                break
            state["point"] += 1
            state["file"] = 0
        if result["next_cursor"]:
            break
        if more_points:
            page()
            break
        state = {"pair": state["pair"] + 1, "point": 0, "file": 0}
    if result["truncated"]:
        warnings.add(
            "Suchfenster begrenzt oder Punkte nicht erreichbar; fehlende Treffer beweisen nicht, dass eine Datei fehlt."
        )
    if result["next_cursor"]:
        warnings.add(
            "Weitere Stände/Dateien können mit der Suchfortsetzung durchsucht werden. Live-Änderungen können die Reihenfolge verändern."
        )
    if any(i["verification"] == "manifest_sha256" for i in result["items"]):
        warnings.add(
            "SHA-256 stammt aus dem vollständigen Manifest; die Suche prüft Dateiinhalt und Verfügbarkeit nicht erneut."
        )
    result["warnings"] = sorted(warnings)
    return result
