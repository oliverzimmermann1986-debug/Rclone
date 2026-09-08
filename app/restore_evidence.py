"""Time-limited restore evidence bound to one configured data path."""

from __future__ import annotations

import hashlib
import json
import math
import time
from typing import Any, Mapping

MAX_EVIDENCE_AGE_SEC = 7 * 24 * 3600


def pair_binding(pair: Mapping[str, Any]) -> dict[str, str]:
    direction = str(pair.get("direction") or "bisync").strip().lower()
    local = str(pair.get("local") or "").strip().rstrip("/")
    remote = str(pair.get("remote") or "").strip().rstrip("/")
    source, target = (local, remote) if direction == "push" else (remote, local)
    fingerprint = hashlib.sha256(
        json.dumps([source, target, direction], separators=(",", ":")).encode()
    ).hexdigest()
    return {"pair_id": str(pair.get("id") or "").strip(), "fingerprint": fingerprint}


def restore_history_key(pair: Mapping[str, Any]) -> str:
    binding = pair_binding(pair)
    if binding["pair_id"]:
        return f"restore:id:{binding['pair_id']}"
    return f"restore:fingerprint:{binding['fingerprint']}"


def evaluate_restore_evidence(
    pair: Mapping[str, Any], history: Mapping[str, Any], *, now: float | None = None
) -> dict[str, Any]:
    current = time.time() if now is None else now
    attempt = history.get("last_result") or history.get("last_success") or {}
    success = history.get("last_success") or {}
    result = attempt.get("pair") or {}
    proof = success.get("pair") or {}
    binding = result.get("evidence_binding")
    checked_at = result.get("evidence_checked_at")
    valid_until = None
    if isinstance(checked_at, (int, float)) and math.isfinite(checked_at):
        valid_until = float(checked_at) + MAX_EVIDENCE_AGE_SEC
    reason = None
    if not attempt:
        state = "never"
    elif not attempt.get("ok"):
        state, reason = "failed", "latest_attempt_failed"
    elif binding != pair_binding(pair):
        state, reason = "stale", "configuration_changed" if binding else "unbound"
    elif valid_until is None or float(checked_at) > current + 60:
        state, reason = "stale", "invalid_timestamp"
    elif current >= valid_until:
        state, reason = "stale", "expired"
    elif (
        result.get("sample_status") != "complete"
        or int(result.get("verified") or 0) <= 0
        or int(result.get("verified") or 0) != int(result.get("sample_size") or 0)
    ):
        state, reason = "stale", "incomplete_sample"
    else:
        state = "passed"
    evidence = {
        "state": state,
        "valid": state == "passed",
        "binding": binding,
        "valid_until": valid_until,
        "invalid_reason": reason,
        "scope": "sample",
        "last_attempt_at": attempt.get("ended_at"),
        "last_success_at": success.get("ended_at"),
        "job_id": attempt.get("job_id"),
        "verified_files": result.get("verified"),
        "sample_size": result.get("sample_size"),
        "last_success_verified_files": proof.get("verified"),
        "last_success_sample_size": proof.get("sample_size"),
        "checksum_verified": state == "passed",
        "error": result.get("error") if state == "failed" else None,
    }
    if attempt.get("ended_at") and attempt.get("started_at"):
        evidence["duration_sec"] = max(
            0.0, float(attempt["ended_at"]) - float(attempt["started_at"])
        )
    return evidence
