"""Ransomware-Früherkennung über Canary-Dateien und verdächtige Dateiendungen."""

from __future__ import annotations

import errno
import hashlib
import logging
import os
import stat
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import protection
from .db import Database
from .jobs.pair_planner import is_remote
from .jobs.scheduler import rclone_history_key
from .lockdown import activate_lockdown
from .recovery_walk import WalkLimitError, bounded_walk

logger = logging.getLogger(__name__)

DEFAULT_SUSPECT_EXTENSIONS: tuple[str, ...] = (
    ".locked",
    ".enc",
    ".encrypted",
    ".crypt",
    ".crypted",
    ".crypto",
    ".wncry",
    ".wcry",
    ".locky",
    ".zepto",
    ".cerber",
    ".ryuk",
    ".conti",
    ".akira",
    ".blackcat",
)

DEFAULT_CANARY_FILENAME = ".sicherpfad-canary.txt"
_CANARY_STATE_KEY = "ransomware_canaries:v1"
_MAX_CANARY_BYTES = 1024 * 1024


def _normalize_extensions(raw: Any) -> list[str]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        return list(DEFAULT_SUSPECT_EXTENSIONS)
    normalized: list[str] = []
    seen: set[str] = set()
    for item in raw:
        value = str(item or "").strip().lower().lstrip(".").strip()
        if (
            not value
            or "/" in value
            or "\\" in value
            or any(ch.isspace() for ch in value)
        ):
            continue
        value = "." + value
        if value not in seen:
            seen.add(value)
            normalized.append(value)
    return normalized or list(DEFAULT_SUSPECT_EXTENSIONS)


def _clamp_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def ransomware_settings(
    backup: Mapping[str, Any], pair: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    raw_backup = backup.get("ransomware_guard") if isinstance(backup, Mapping) else None
    base = raw_backup if isinstance(raw_backup, Mapping) else {}
    pair_map = pair if isinstance(pair, Mapping) else {}
    raw_pair = pair_map.get("ransomware_guard")
    pair_guard = raw_pair if isinstance(raw_pair, Mapping) else {}

    def value(name: str, default: Any) -> Any:
        if name in pair_guard and pair_guard[name] is not None:
            return pair_guard[name]
        if name in base and base[name] is not None:
            return base[name]
        return default

    canary_file = str(
        pair_map.get("canary_file")
        or pair_guard.get("canary_file")
        or base.get("canary_file")
        or ""
    ).strip()
    canary_sha256 = (
        str(
            pair_map.get("canary_sha256")
            or pair_guard.get("canary_sha256")
            or base.get("canary_sha256")
            or ""
        )
        .strip()
        .lower()
    )

    return {
        "enabled": bool(value("enabled", True)),
        "suspect_extensions": _normalize_extensions(
            value("suspect_extensions", DEFAULT_SUSPECT_EXTENSIONS)
        ),
        "max_suspect_files": _clamp_int(
            value("max_suspect_files", 1), default=1, minimum=1, maximum=10_000
        ),
        "auto_lockdown": bool(value("auto_lockdown", False)),
        "canary_file": canary_file,
        "canary_sha256": canary_sha256,
    }


def validate_canary_filename(value: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned or len(cleaned) > 255:
        raise ValueError("Ungültiger Canary-Dateiname")
    if cleaned.startswith(("/", "\\")) or ":" in cleaned or "\\" in cleaned:
        raise ValueError("Canary-Dateiname muss ein sicherer relativer Pfad sein")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in cleaned):
        raise ValueError("Canary-Dateiname enthält ungültige Steuerzeichen")
    parts = cleaned.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError("Canary-Dateiname darf keine Pfadnavigation enthalten")
    return cleaned


def _ensure_no_symlink_chain(root: Path, rel: str) -> Path:
    current = root
    parts = rel.split("/")
    for part in parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise ValueError(
                f"Übergeordnetes Verzeichnis ist ein symbolischer Link: {part}"
            )
    target = current / parts[-1]
    if target.is_symlink():
        raise ValueError(f"Canary-Datei ist ein symbolischer Link: {rel}")
    return target


def _has_symlink_in_chain(root: Path, rel: str) -> bool:
    if root.is_symlink():
        return True
    current = root
    for part in rel.split("/"):
        current = current / part
        if current.is_symlink():
            return True
    return False


def init_canary(
    pair: Mapping[str, Any],
    *,
    database: Database | None = None,
    filename: str | None = None,
) -> dict[str, Any]:
    local = str(pair.get("local") or "").strip()
    if not local or is_remote(local):
        raise ValueError(
            "Canary-Datei kann nur auf einem lokalen Quellpfad initialisiert werden"
        )
    root = Path(local)
    if root.is_symlink() or not root.exists() or not root.is_dir():
        raise ValueError(f"Lokales Quellverzeichnis ist ungültig: {local}")

    pair_guard = pair.get("ransomware_guard")
    configured_name = (
        filename
        or str(pair.get("canary_file") or "").strip()
        or (
            str(pair_guard.get("canary_file") or "").strip()
            if isinstance(pair_guard, Mapping)
            else ""
        )
        or DEFAULT_CANARY_FILENAME
    )
    rel = validate_canary_filename(configured_name)
    target = _ensure_no_symlink_chain(root, rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise ValueError(f"Canary-Datei ist ein symbolischer Link: {rel}")

    pair_name = str(pair.get("name") or "")
    payload = (
        f"SICHERPFAD CANARY DO NOT MODIFY\npair={pair_name}\nfile={rel}\n"
    ).encode("utf-8")

    fd, tmp_name = tempfile.mkstemp(
        prefix=".sicherpfad-canary.", suffix=".tmp", dir=str(target.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        if target.is_symlink():
            raise ValueError(f"Canary-Datei ist ein symbolischer Link: {rel}")
        os.replace(tmp_path, target)
    finally:
        tmp_path.unlink(missing_ok=True)

    digest = hashlib.sha256(payload).hexdigest()

    if database is not None:
        history_key = rclone_history_key(dict(pair))
        record = {
            "pair": pair_name,
            "canary_file": rel,
            "canary_sha256": digest,
            "updated_at": time.time(),
        }

        def update(raw: Any) -> dict[str, Any]:
            state = dict(raw) if isinstance(raw, dict) else {}
            state[history_key] = record
            return state

        database.runtime_update(_CANARY_STATE_KEY, update, default={})

    return {
        "ok": True,
        "pair": pair_name,
        "canary_file": rel,
        "canary_sha256": digest,
    }


def _stored_canary_entry(
    pair: Mapping[str, Any], database: Database | None
) -> dict[str, Any]:
    if database is None:
        return {}
    raw = database.runtime_get(_CANARY_STATE_KEY, {})
    if not isinstance(raw, Mapping):
        return {}
    entry = raw.get(rclone_history_key(dict(pair)))
    return dict(entry) if isinstance(entry, Mapping) else {}


def check_canary(
    pair: Mapping[str, Any], *, database: Database | None = None
) -> dict[str, Any]:
    pair_settings = ransomware_settings({}, pair)
    stored = _stored_canary_entry(pair, database)
    canary_file = (
        pair_settings["canary_file"] or str(stored.get("canary_file") or "").strip()
    )
    expected_sha256 = (
        pair_settings["canary_sha256"]
        or str(stored.get("canary_sha256") or "").strip().lower()
    )

    if not canary_file:
        return {"configured": False, "ok": True, "reason": "not_configured"}

    local = str(pair.get("local") or "").strip()
    if is_remote(local):
        return {"configured": True, "ok": True, "reason": "remote_source_skipped"}

    try:
        rel = validate_canary_filename(canary_file)
    except ValueError as exc:
        return {
            "configured": True,
            "ok": False,
            "canary_file": canary_file,
            "reason": "invalid_filename",
            "error": str(exc),
        }

    if not local:
        return {
            "configured": True,
            "ok": False,
            "canary_file": rel,
            "reason": "missing",
            "error": f"Canary-Datei fehlt: {rel}",
        }

    root = Path(local)
    if _has_symlink_in_chain(root, rel):
        return {
            "configured": True,
            "ok": False,
            "canary_file": rel,
            "reason": "symlink",
            "error": f"Canary-Datei ist ein symbolischer Link: {rel}",
        }

    target = root / rel
    try:
        st = target.lstat()
    except OSError:
        return {
            "configured": True,
            "ok": False,
            "canary_file": rel,
            "reason": "missing",
            "error": f"Canary-Datei fehlt: {rel}",
        }

    if stat.S_ISLNK(st.st_mode):
        return {
            "configured": True,
            "ok": False,
            "canary_file": rel,
            "reason": "symlink",
            "error": f"Canary-Datei ist ein symbolischer Link: {rel}",
        }
    if not stat.S_ISREG(st.st_mode):
        return {
            "configured": True,
            "ok": False,
            "canary_file": rel,
            "reason": "missing",
            "error": f"Canary-Datei fehlt: {rel}",
        }
    if st.st_size > _MAX_CANARY_BYTES:
        return {
            "configured": True,
            "ok": False,
            "canary_file": rel,
            "reason": "hash_mismatch",
            "error": f"Canary-Datei wurde verändert: {rel}",
        }

    open_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(target, open_flags)
    except OSError as exc:
        if exc.errno == getattr(errno, "ELOOP", 40):
            return {
                "configured": True,
                "ok": False,
                "canary_file": rel,
                "reason": "symlink",
                "error": f"Canary-Datei ist ein symbolischer Link: {rel}",
            }
        return {
            "configured": True,
            "ok": False,
            "canary_file": rel,
            "reason": "read_error",
            "error": f"Canary-Datei nicht lesbar: {exc}",
        }
    try:
        fd_st = os.fstat(fd)
        if not stat.S_ISREG(fd_st.st_mode):
            return {
                "configured": True,
                "ok": False,
                "canary_file": rel,
                "reason": "missing",
                "error": f"Canary-Datei fehlt: {rel}",
            }
        if os.name != "nt" and st.st_ino and fd_st.st_ino != st.st_ino:
            return {
                "configured": True,
                "ok": False,
                "canary_file": rel,
                "reason": "symlink",
                "error": f"Canary-Datei wurde während des Zugriffs ersetzt: {rel}",
            }
        if fd_st.st_size > _MAX_CANARY_BYTES:
            return {
                "configured": True,
                "ok": False,
                "canary_file": rel,
                "reason": "hash_mismatch",
                "error": f"Canary-Datei wurde verändert: {rel}",
            }
        chunks: list[bytes] = []
        total_read = 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total_read += len(chunk)
            if total_read > _MAX_CANARY_BYTES:
                return {
                    "configured": True,
                    "ok": False,
                    "canary_file": rel,
                    "reason": "hash_mismatch",
                    "error": f"Canary-Datei wurde verändert: {rel}",
                }
            chunks.append(chunk)
        data = b"".join(chunks)
    except OSError as exc:
        return {
            "configured": True,
            "ok": False,
            "canary_file": rel,
            "reason": "read_error",
            "error": f"Canary-Datei nicht lesbar: {exc}",
        }
    finally:
        try:
            os.close(fd)
        except OSError:
            pass

    actual_sha256 = hashlib.sha256(data).hexdigest()
    if expected_sha256 and actual_sha256 != expected_sha256.lower():
        return {
            "configured": True,
            "ok": False,
            "canary_file": rel,
            "expected_sha256": expected_sha256.lower(),
            "actual_sha256": actual_sha256,
            "reason": "hash_mismatch",
            "error": f"Canary-Datei wurde verändert: {rel}",
        }

    return {
        "configured": True,
        "ok": True,
        "canary_file": rel,
        "expected_sha256": expected_sha256.lower()
        if expected_sha256
        else actual_sha256,
        "actual_sha256": actual_sha256,
        "reason": "ok",
    }


_DIR_WATERMARK_CACHE: dict[
    tuple[str, tuple[str, ...]], tuple[int, int, str, str, list[str]]
] = {}


def scan_suspect_extensions(
    source: str,
    *,
    suspect_extensions: Sequence[str] = DEFAULT_SUSPECT_EXTENSIONS,
    max_suspect_files: int = 1,
    max_scan_entries: int = 50_000,
    ignore_dirs: Sequence[str] = (
        ".rclone-versions",
        ".sicherpfad-sqlite",
        ".zfs",
    ),
) -> dict[str, Any]:
    cleaned_source = str(source or "").strip()
    if not cleaned_source or is_remote(cleaned_source):
        return {
            "scanned": 0,
            "suspect_count": 0,
            "suspect_files": [],
            "blocked": False,
            "truncated": False,
            "skipped": True,
        }
    root = Path(cleaned_source)
    if root.is_symlink() or not root.is_dir():
        return {
            "scanned": 0,
            "suspect_count": 0,
            "suspect_files": [],
            "blocked": False,
            "truncated": False,
            "skipped": True,
        }

    normalized_exts = tuple(_normalize_extensions(suspect_extensions))
    ignore_set = {str(item) for item in ignore_dirs if str(item)}
    threshold = max(1, int(max_suspect_files))
    scanned = 0
    cached_dirs = 0
    matches: list[str] = []
    truncated = False

    try:
        for current, directories, filenames in bounded_walk(
            root, max_entries=max(1, int(max_scan_entries))
        ):
            directories[:] = [d for d in directories if d not in ignore_set]
            directories.sort()
            filenames.sort()
            cache_key = (str(current), normalized_exts)
            dir_mtime_ns = 0
            try:
                dir_mtime_ns = current.stat().st_mtime_ns
            except OSError:
                dir_mtime_ns = 0
            file_count = len(filenames)
            first_name = filenames[0] if filenames else ""
            last_name = filenames[-1] if filenames else ""
            cached = _DIR_WATERMARK_CACHE.get(cache_key)
            if (
                file_count >= 32
                and dir_mtime_ns > 0
                and cached is not None
                and cached[0] == dir_mtime_ns
                and cached[1] == file_count
                and cached[2] == first_name
                and cached[3] == last_name
            ):
                scanned += file_count
                cached_dirs += 1
                matches.extend(cached[4])
                continue

            dir_matches: list[str] = []
            for filename in filenames:
                scanned += 1
                lower_name = filename.lower()
                if any(lower_name.endswith(ext) for ext in normalized_exts):
                    try:
                        rel_path = (current / filename).relative_to(root).as_posix()
                    except ValueError:
                        rel_path = filename
                    dir_matches.append(rel_path)
            matches.extend(dir_matches)
            if file_count >= 32 and dir_mtime_ns > 0:
                if len(_DIR_WATERMARK_CACHE) > 4096:
                    _DIR_WATERMARK_CACHE.clear()
                _DIR_WATERMARK_CACHE[cache_key] = (
                    dir_mtime_ns,
                    file_count,
                    first_name,
                    last_name,
                    dir_matches,
                )
    except WalkLimitError:
        truncated = True

    return {
        "scanned": scanned,
        "cached_dirs": cached_dirs,
        "suspect_count": len(matches),
        "suspect_files": matches[:50],
        "blocked": len(matches) >= threshold,
        "truncated": truncated,
    }


def _quarantine_pair(
    database: Database,
    *,
    pair: Mapping[str, Any],
    reason: str,
    message: str,
    canary: Mapping[str, Any],
    extension_scan: Mapping[str, Any],
) -> dict[str, Any]:
    key = protection._state_key(pair)
    quarantine = {
        "blocked": True,
        "enabled": True,
        "pair": str(pair.get("name") or ""),
        "history_key": rclone_history_key(dict(pair)),
        "reason": reason,
        "message": message,
        "detected_at": time.time(),
        "acknowledged": False,
        "endpoints": protection._quarantine_endpoints(pair),
        "canary_reason": canary.get("reason"),
        "suspect_count": int(extension_scan.get("suspect_count") or 0),
        "suspect_files": list(extension_scan.get("suspect_files") or [])[:10],
    }

    def update(raw: Any) -> dict[str, Any]:
        state = protection._normalize_state(raw)
        state["quarantines"][key] = quarantine
        return protection._bounded_state(state)

    database.runtime_update(protection._ANOMALY_STATE_KEY, update, default={})
    return quarantine


def preflight_ransomware_guard(
    database: Database,
    *,
    pair: Mapping[str, Any],
    backup: Mapping[str, Any],
    source: str,
) -> dict[str, Any]:
    settings = ransomware_settings(backup, pair)
    canary = check_canary(pair, database=database)
    if settings["enabled"]:
        extension_scan = scan_suspect_extensions(
            source,
            suspect_extensions=settings["suspect_extensions"],
            max_suspect_files=settings["max_suspect_files"],
        )
    else:
        extension_scan = {
            "scanned": 0,
            "suspect_count": 0,
            "suspect_files": [],
            "blocked": False,
            "truncated": False,
            "skipped": True,
        }

    canary_failed = bool(canary.get("configured") and not canary.get("ok"))
    ext_blocked = bool(extension_scan.get("blocked"))

    if canary_failed or ext_blocked:
        if canary_failed:
            reason = "canary_tampered"
            message = str(
                canary.get("error")
                or "Canary-Datei fehlt oder wurde unerwartet verändert"
            )
        else:
            reason = "suspect_extensions"
            sample = ", ".join(extension_scan.get("suspect_files", [])[:3])
            count = int(extension_scan.get("suspect_count") or 0)
            message = (
                f"Verdächtige Ransomware-Dateiendungen erkannt ({count} Datei(en): {sample})"
                if sample
                else f"Verdächtige Ransomware-Dateiendungen erkannt ({count} Datei(en))"
            )

        quarantine = _quarantine_pair(
            database,
            pair=pair,
            reason=reason,
            message=message,
            canary=canary,
            extension_scan=extension_scan,
        )
        database.audit_add(
            "ransomware_blocked",
            actor="system",
            details={
                "pair": str(pair.get("name") or ""),
                "reason": reason,
                "message": message,
                "suspect_count": int(extension_scan.get("suspect_count") or 0),
                "suspect_files": list(extension_scan.get("suspect_files") or [])[:10],
                "canary_reason": canary.get("reason"),
            },
        )
        auto_lockdown_triggered = False
        if settings["auto_lockdown"]:
            try:
                try:
                    activate_lockdown(actor="system", reason=message[:300], db=database)
                except TypeError:
                    activate_lockdown(actor="system", reason=message[:300])
                auto_lockdown_triggered = True
            except Exception:
                logger.exception(
                    "Automatischer Lockdown nach Ransomware-Erkennung fehlgeschlagen"
                )

        return {
            "blocked": True,
            "reason": reason,
            "message": message,
            "canary": canary,
            "extension_scan": extension_scan,
            "auto_lockdown_triggered": auto_lockdown_triggered,
            "quarantine": quarantine,
        }

    return {
        "blocked": False,
        "reason": "ok",
        "canary": canary,
        "extension_scan": extension_scan,
        "auto_lockdown_triggered": False,
    }


def ransomware_overview(
    database: Database, config: Mapping[str, Any]
) -> dict[str, Any]:
    raw_backup = config.get("backup") if isinstance(config, Mapping) else None
    backup = (
        raw_backup
        if isinstance(raw_backup, Mapping)
        else (config if isinstance(config, Mapping) else {})
    )
    raw_pairs = backup.get("pairs") if isinstance(backup, Mapping) else []
    pairs_out: list[dict[str, Any]] = []
    if isinstance(raw_pairs, Sequence):
        for pair in raw_pairs:
            if not isinstance(pair, Mapping):
                continue
            pairs_out.append(
                {
                    "name": str(pair.get("name") or ""),
                    "enabled": bool(pair.get("enabled", True)),
                    "canary": check_canary(pair, database=database),
                    "settings": ransomware_settings(backup, pair),
                }
            )
    return {
        "settings": ransomware_settings(backup),
        "pairs": pairs_out,
    }


__all__ = [
    "DEFAULT_CANARY_FILENAME",
    "DEFAULT_SUSPECT_EXTENSIONS",
    "check_canary",
    "init_canary",
    "preflight_ransomware_guard",
    "ransomware_overview",
    "ransomware_settings",
    "scan_suspect_extensions",
    "validate_canary_filename",
]
