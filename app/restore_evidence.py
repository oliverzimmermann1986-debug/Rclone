"""Time-limited restore evidence bound to one configured data path."""

from __future__ import annotations

import hashlib
import json
import math
import time
from typing import Any, Mapping

MAX_EVIDENCE_AGE_SEC = 7 * 24 * 3600


def is_verified_partial(result: Mapping[str, Any]) -> bool:
    """Recognize a verified but undersized sample, including historical results.

    The old human-readable partial message is the only tolerated error value.
    A failed transfer, mismatch or failed cleanup must never become a warning.
    """
    if not isinstance(result, Mapping):
        return False
    if (
        result.get("ok") is not False
        or result.get("sample_status") != "partial_selection"
    ):
        return False
    if any(
        result.get(key)
        for key in (
            "cancelled",
            "temp_cleanup_failed",
            "cleanup_error",
            "skipped",
            "timed_out",
        )
    ):
        return False
    if result.get("outcome") not in (None, "partial"):
        return False
    if result.get("integrity_ok") is False or result.get("coverage_complete") is True:
        return False
    counts = [
        result.get(key)
        for key in (
            "verified",
            "sample_size",
            "requested_sample_size",
            "restored_files",
        )
    ]
    if any(type(value) is not int for value in counts):
        return False
    verified, sampled, requested, restored = counts
    if not (0 < verified == sampled == restored < requested):
        return False
    if type(result.get("return_code")) is not int or result["return_code"] != 0:
        return False
    if result.get("sample_shortfall", requested - sampled) != requested - sampled:
        return False
    if result.get("sample_shortfall_reason") not in {
        "byte_budget",
        "insufficient_eligible_files",
        "listing_truncated",
    }:
        return False
    old_message = (
        f"Teil-Stichprobe: {verified} von {requested} angeforderten "
        "Dateien ausgewählt und erfolgreich geprüft"
    )
    return result.get("error") in (None, "", old_message)


def partial_restore_warning(result: Mapping[str, Any]) -> str:
    reason = {
        "byte_budget": "Das Datenlimit begrenzt den Prüfumfang.",
        "insufficient_eligible_files": "Es waren weniger geeignete Dateien verfügbar.",
        "listing_truncated": "Das Scanlimit begrenzt den Prüfumfang.",
    }.get(
        str(result.get("sample_shortfall_reason") or ""), "Der Prüfumfang ist begrenzt."
    )
    return (
        f"{result.get('verified')} von {result.get('requested_sample_size')} "
        f"angeforderten Dateien erfolgreich geprüft. {reason} "
        "Die angeforderte Stichprobe ist noch nicht vollständig geprüft."
    )


def is_partial_restore_summary(summary: Mapping[str, Any]) -> bool:
    """Classify only all-success/partial restore runs; genuine errors win."""
    if not isinstance(summary, Mapping):
        return False
    # Bounded persisted JSON can omit later pair results, including failures.
    # Never infer an all-success/partial run from that incomplete preview. This
    # is the summary's serialization marker, not a pair's bounded-listing flag.
    if summary.get("truncated"):
        return False
    if summary.get("kind") not in (None, "restoretest"):
        return False
    if any(
        summary.get(key)
        for key in ("cancelled", "error", "temp_cleanup_failed", "cleanup_error")
    ):
        return False
    results = summary.get("pairs")
    if not isinstance(results, list) or not results:
        return False
    partial_seen = False
    for result in results:
        if not isinstance(result, Mapping):
            return False
        if any(
            result.get(key)
            for key in ("cancelled", "temp_cleanup_failed", "cleanup_error")
        ):
            return False
        if (
            result.get("name") == "restore-drill"
            and type(result.get("pairs_tested")) is int
            and result["pairs_tested"] > 0
            and "sample_status" not in result
            and "evidence_binding" not in result
        ):
            if result.get("error"):
                return False
            continue
        if is_verified_partial(result):
            partial_seen = True
        elif result.get("ok") is not True or result.get("error"):
            return False
    return partial_seen


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
    partial = is_verified_partial(result)
    if not attempt:
        state = "never"
    elif not attempt.get("ok") and not partial:
        state, reason = "failed", "latest_attempt_failed"
    elif binding != pair_binding(pair):
        state, reason = "stale", "configuration_changed" if binding else "unbound"
    elif valid_until is None or float(checked_at) > current + 60:
        state, reason = "stale", "invalid_timestamp"
    elif current >= valid_until:
        state, reason = "stale", "expired"
    elif partial:
        state, reason = "partial", "incomplete_sample"
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
        "requested_sample_size": result.get("requested_sample_size"),
        "budget_bytes": result.get("budget_bytes"),
        "sample_shortfall_reason": result.get("sample_shortfall_reason"),
        "last_success_verified_files": proof.get("verified"),
        "last_success_sample_size": proof.get("sample_size"),
        "checksum_verified": state == "passed",
        "sample_checksum_verified": state in {"passed", "partial"},
        "coverage_complete": state == "passed",
        "warning": partial_restore_warning(result) if state == "partial" else None,
        "error": result.get("error") if state == "failed" else None,
    }
    if attempt.get("ended_at") and attempt.get("started_at"):
        evidence["duration_sec"] = max(
            0.0, float(attempt["ended_at"]) - float(attempt["started_at"])
        )
    return evidence
