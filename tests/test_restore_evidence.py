import pytest

from app.restore_evidence import (
    MAX_EVIDENCE_AGE_SEC,
    evaluate_restore_evidence,
    pair_binding,
    restore_history_key,
)


PAIR = {
    "id": "photos",
    "name": "Fotos",
    "local": "/photos",
    "remote": "cloud:/Photos",
    "direction": "push",
}


def history(now=1000):
    result = {
        "ok": True,
        "ended_at": now,
        "job_id": 1,
        "pair": {
            "evidence_binding": pair_binding(PAIR),
            "evidence_checked_at": now,
            "sample_status": "complete",
            "verified": 3,
            "sample_size": 3,
        },
    }
    return {"last_result": result, "last_success": result}


def test_rename_keeps_identity_but_new_target_invalidates_proof():
    renamed = {**PAIR, "name": "Urlaub"}
    assert restore_history_key(renamed) == restore_history_key(PAIR)
    assert evaluate_restore_evidence(renamed, history(), now=1100)["valid"] is True
    for changed in (
        {"remote": "other:/Photos"},
        {"local": "/new"},
        {"id": "reused-name"},
    ):
        evidence = evaluate_restore_evidence({**PAIR, **changed}, history(), now=1100)
        assert evidence["state"] == "stale"
        assert evidence["checksum_verified"] is False
        assert evidence["invalid_reason"] == "configuration_changed"


def test_expiry_and_missing_binding_do_not_award_a_current_pass():
    expired = evaluate_restore_evidence(
        PAIR, history(), now=1000 + MAX_EVIDENCE_AGE_SEC
    )
    assert expired["invalid_reason"] == "expired"
    assert expired["last_success_at"] == 1000
    old = history()
    old["last_result"]["pair"].pop("evidence_binding")
    assert evaluate_restore_evidence(PAIR, old, now=1100)["invalid_reason"] == "unbound"


def test_new_failure_keeps_historical_success_but_invalidates_current_proof():
    value = history()
    value["last_result"] = {
        "ok": False,
        "ended_at": 1100,
        "pair": {"error": "mismatch"},
    }
    evidence = evaluate_restore_evidence(PAIR, value, now=1100)
    assert evidence["state"] == "failed"
    assert evidence["last_success_at"] == 1000
    assert evidence["valid"] is False
    assert evidence["checksum_verified"] is False
    assert evidence["verified_files"] is None
    assert evidence["last_success_verified_files"] == 3


@pytest.mark.parametrize("timestamp", [None, float("nan"), float("inf"), 10000])
def test_invalid_future_or_nonfinite_timestamps_cannot_pass(timestamp):
    value = history()
    value["last_result"]["pair"]["evidence_checked_at"] = timestamp
    assert evaluate_restore_evidence(PAIR, value, now=1100)["valid"] is False
