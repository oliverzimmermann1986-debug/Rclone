"""rclone-Test: konfigurierte Remotes und optional ein Pair prüfen."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..auth import require_auth
from ..config_store import get_config
from ..config_validation import ConfigValidationError, validate_config
from ..jobs.rclone_sync import _is_remote
from ..rclone_args import (
    UnsafeRcloneConfigError,
    assert_safe_rclone_config,
    rclone_subprocess_env,
)
from ..security import ensure_within, parse_browse_roots, require_csrf

router = APIRouter(
    prefix="/api/test",
    tags=["test"],
    dependencies=[Depends(require_auth), Depends(require_csrf)],
)


class RcloneTest(BaseModel):
    pair_index: Optional[int] = Field(default=None, ge=0, le=10000)
    pair: Optional[dict[str, Any]] = None


def _saved_local_paths(backup: dict[str, Any]) -> set[Path]:
    allowed: set[Path] = set()
    for saved in backup.get("pairs") or []:
        if not isinstance(saved, dict):
            continue
        for key in ("local", "remote"):
            raw = str(saved.get(key) or "").strip()
            if raw and not _is_remote(raw):
                try:
                    allowed.add(Path(raw).expanduser().resolve(strict=False))
                except (OSError, RuntimeError):
                    continue
    return allowed


def _enforce_unsaved_local_browse_roots(
    pair: dict[str, Any], snapshot: dict[str, Any], backup: dict[str, Any]
) -> None:
    saved_paths = _saved_local_paths(backup)
    configured_roots = (snapshot.get("web") or {}).get(
        "local_browse_roots",
        ["/mnt", "/media", "/srv", "/opt/rclone-sync/data"],
    )
    browse_roots = parse_browse_roots(configured_roots)
    for key in ("local", "remote"):
        raw = str(pair.get(key) or "").strip()
        if not raw or _is_remote(raw):
            continue
        resolved = Path(raw).expanduser().resolve(strict=False)
        if resolved in saved_paths:
            continue
        ensure_within(Path(raw), browse_roots)


@router.post("/rclone")
def test_rclone(request: RcloneTest) -> dict[str, Any]:
    try:
        try:
            assert_safe_rclone_config()
        except UnsafeRcloneConfigError as exc:
            raise HTTPException(400, str(exc)) from exc
        remotes_result = subprocess.run(
            ["rclone", "listremotes"],
            capture_output=True,
            text=True,
            timeout=15,
            stdin=subprocess.DEVNULL,
            env=rclone_subprocess_env(),
        )
        if remotes_result.returncode != 0:
            return {
                "ok": False,
                "error": f"listremotes: {remotes_result.stderr.strip()[:300]}",
            }
        remotes = sorted(
            {
                line.strip().rstrip(":")
                for line in remotes_result.stdout.splitlines()
                if line.strip()
            },
            key=str.casefold,
        )
        if not remotes and request.pair is None and request.pair_index is None:
            return {
                "ok": False,
                "error": "Keine Remotes konfiguriert. `rclone config` ausführen.",
            }

        store = get_config()
        snapshot = store.snapshot()
        backup = snapshot.get("backup") or {}
        configured = str(backup.get("rclone_remote") or "")
        response: dict[str, Any] = {
            "ok": True,
            "remotes": remotes,
            "configured_remote": configured,
            "remote_exists": configured in remotes if configured else None,
        }
        if (
            configured
            and not response["remote_exists"]
            and request.pair is None
            and request.pair_index is None
        ):
            return {
                **response,
                "ok": False,
                "error": f"Konfigurierter Remote '{configured}' fehlt. Vorhanden: {', '.join(remotes)}",
            }

        pair: dict[str, Any] | None = None
        if request.pair is not None:
            candidate = dict(snapshot)
            candidate_backup = dict(backup)
            candidate_pairs = list(backup.get("pairs") or [])
            draft_index = next(
                (
                    index
                    for index, existing in enumerate(candidate_pairs)
                    if (
                        request.pair.get("id")
                        and existing.get("id") == request.pair["id"]
                    )
                    or (
                        not request.pair.get("id")
                        and existing.get("name") == request.pair.get("name")
                    )
                ),
                len(candidate_pairs),
            )
            if draft_index == len(candidate_pairs):
                candidate_pairs.append(request.pair)
            else:
                candidate_pairs[draft_index] = request.pair
            candidate_backup["pairs"] = candidate_pairs
            candidate["backup"] = candidate_backup
            try:
                normalized, warnings = validate_config(candidate)
            except ConfigValidationError as exc:
                raise HTTPException(
                    422, {"message": "Pair ungültig", "errors": exc.errors}
                ) from exc
            pair = normalized["backup"]["pairs"][draft_index]
            _enforce_unsaved_local_browse_roots(pair, snapshot, backup)
            response["tested_unsaved"] = True
            response["warnings"] = warnings
        elif request.pair_index is not None:
            pairs = backup.get("pairs") or []
            if not 0 <= request.pair_index < len(pairs):
                return {**response, "ok": False, "error": "pair_index ungültig"}
            pair = pairs[request.pair_index]

        if pair is not None:
            remote_path = str(pair.get("remote") or "")
            cache_dir = os.getenv(
                "RCLONE_CACHE_DIR", "/opt/rclone-sync/data/.rclone-cache"
            )
            size_result = subprocess.run(
                [
                    "rclone",
                    "size",
                    "--json",
                    "--cache-dir",
                    cache_dir,
                    "--",
                    remote_path,
                ],
                capture_output=True,
                text=True,
                timeout=60,
                stdin=subprocess.DEVNULL,
                env=rclone_subprocess_env(),
            )
            response["remote_path"] = remote_path
            if size_result.returncode == 0:
                try:
                    size_data = json.loads(size_result.stdout or "{}")
                    response["remote_size"] = {
                        "count": size_data.get("count"),
                        "bytes": size_data.get("bytes"),
                    }
                except json.JSONDecodeError:
                    response["remote_size_output"] = size_result.stdout.strip()[:300]
            else:
                response["ok"] = False
                response["error"] = f"Remote-Pfad nicht lesbar: {remote_path}"
                response["remote_size_output"] = (
                    size_result.stderr or size_result.stdout
                ).strip()[:300]

            local_path = str(pair.get("local") or "")
            response["local_path"] = local_path
            response["local_exists"] = (
                False if _is_remote(local_path) else Path(local_path).is_dir()
            )
            if _is_remote(local_path):
                source_result = subprocess.run(
                    [
                        "rclone",
                        "size",
                        "--json",
                        "--cache-dir",
                        cache_dir,
                        "--",
                        local_path,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    stdin=subprocess.DEVNULL,
                    env=rclone_subprocess_env(),
                )
                response["local_exists"] = source_result.returncode == 0
                if source_result.returncode:
                    response["local_size_output"] = (
                        source_result.stderr or source_result.stdout
                    ).strip()[:300]
            if not response["local_exists"]:
                response["ok"] = False
                response["error"] = (
                    f"Lokaler Pfad fehlt oder ist kein Verzeichnis: {local_path}"
                )

        response["message"] = (
            f"rclone ok — {len(remotes)} Remote(s)"
            if response["ok"]
            else "rclone-Prüfung fehlgeschlagen"
        )
        return response
    except HTTPException:
        raise
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "rclone Timeout"}
    except FileNotFoundError:
        return {"ok": False, "error": "rclone binary nicht gefunden"}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
