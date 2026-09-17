import pytest

from app.restore_evidence import (
    MAX_EVIDENCE_AGE_SEC,
    evaluate_restore_evidence,
    is_partial_restore_summary,
    is_verified_partial,
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


def historical_partial():
    return {
        "name": "Fotos",
        "ok": False,
        "sample_status": "partial_selection",
        "requested_sample_size": 20,
        "sample_size": 19,
        "verified": 19,
        "restored_files": 19,
        "return_code": 0,
        "sample_shortfall": 1,
        "sample_shortfall_reason": "byte_budget",
        "budget_bytes": 268435456,
        "evidence_binding": pair_binding(PAIR),
        "evidence_checked_at": 1000,
        "error": "Teil-Stichprobe: 19 von 20 angeforderten Dateien ausgewählt und erfolgreich geprüft",
    }


def test_historical_partial_is_warning_without_awarding_full_evidence():
    result = historical_partial()
    value = history()
    value["last_result"] = {"ok": False, "ended_at": 1000, "pair": result}
    evidence = evaluate_restore_evidence(PAIR, value, now=1100)
    assert is_verified_partial(result) is True
    assert evidence["state"] == "partial"
    assert evidence["valid"] is False
    assert evidence["checksum_verified"] is False
    assert evidence["sample_checksum_verified"] is True
    assert evidence["coverage_complete"] is False
    assert evidence["verified_files"] == 19
    assert evidence["requested_sample_size"] == 20
    assert evidence["budget_bytes"] == 268435456
    assert evidence["sample_shortfall_reason"] == "byte_budget"
    assert evidence["last_success_verified_files"] == 3
    assert evidence["error"] is None
    assert "Datenlimit" in evidence["warning"]


@pytest.mark.parametrize(
    "change",
    [
        {"verified": 18},
        {"restored_files": 18},
        {"sample_size": 0, "verified": 0, "restored_files": 0},
        {"return_code": 1},
        {"return_code": False},
        {"verified": "19"},
        {"sample_shortfall": 2},
        {"requested_sample_size": 19},
        {"cancelled": True},
        {"temp_cleanup_failed": True},
        {"cleanup_error": "permission denied"},
        {"sample_status": "verification_failed"},
        {"error": "Prüfsummen weichen ab"},
        {"error": historical_partial()["error"] + "; Bereinigung fehlgeschlagen"},
        {"integrity_ok": False},
    ],
)
def test_real_failures_never_become_verified_partial(change):
    result = {**historical_partial(), **change}
    assert is_verified_partial(result) is False
    value = {"last_result": {"ok": False, "pair": result}}
    evidence = evaluate_restore_evidence(PAIR, value, now=1100)
    assert evidence["state"] == "failed"
    assert evidence["sample_checksum_verified"] is False


@pytest.mark.parametrize(
    "change,now,reason",
    [
        ({"evidence_binding": None}, 1100, "unbound"),
        ({"evidence_binding": {}}, 1100, "unbound"),
        ({"evidence_binding": {"fingerprint": "other"}}, 1100, "configuration_changed"),
        ({}, 1000 + MAX_EVIDENCE_AGE_SEC, "expired"),
        ({"evidence_checked_at": 5000}, 1100, "invalid_timestamp"),
    ],
)
def test_partial_evidence_must_still_be_bound_and_current(change, now, reason):
    value = {"last_result": {"ok": False, "pair": {**historical_partial(), **change}}}
    evidence = evaluate_restore_evidence(PAIR, value, now=now)
    assert evidence["state"] == "stale"
    assert evidence["invalid_reason"] == reason
    assert evidence["valid"] is False
    assert evidence["sample_checksum_verified"] is False


def test_partial_summary_accepts_historical_aggregate_but_real_errors_win():
    result = historical_partial()
    summary = {
        "ok": False,
        "pairs": [
            result,
            {"name": "Rezepte", "ok": True},
            {"name": "restore-drill", "ok": False, "pairs_tested": 2},
        ],
    }
    assert is_partial_restore_summary(summary) is True
    assert is_partial_restore_summary({**summary, "kind": "restoretest"}) is True
    for changes in (
        {"kind": "backup"},
        {"cancelled": True},
        {"error": "global failure"},
        {"pairs": [result, {"name": "Rezepte", "ok": False, "error": "mismatch"}]},
        {"pairs": [{"name": "restore-drill", "ok": False}]},
        {"pairs": [result, {"name": "other-aggregate", "ok": False}]},
        {"pairs": [result, {"name": "restore-drill", "ok": False}]},
        {
            "pairs": [
                result,
                {
                    "name": "restore-drill",
                    "ok": False,
                    "pairs_tested": 1,
                    "evidence_binding": pair_binding(PAIR),
                },
            ]
        },
    ):
        assert is_partial_restore_summary({**summary, **changes}) is False


def test_truncated_summary_is_not_proof_that_every_pair_succeeded():
    summary = {"kind": "restoretest", "pairs": [historical_partial()]}
    assert is_partial_restore_summary(summary) is True
    assert is_partial_restore_summary({**summary, "truncated": False}) is True
    assert is_partial_restore_summary({**summary, "truncated": True}) is False


def test_complete_summary_can_contain_a_bounded_listing_partial():
    # Listing bounds are a valid limited-sample reason, unlike lost results
    # from serialization of the enclosing job summary.
    partial = {
        **historical_partial(),
        "truncated": True,
        "sample_shortfall_reason": "listing_truncated",
    }
    assert is_partial_restore_summary({"pairs": [partial]}) is True


@pytest.mark.parametrize("value", [None, [], "partial", 1, True])
def test_malformed_result_containers_cannot_be_classified_as_partial(value):
    assert is_verified_partial(value) is False
    assert is_partial_restore_summary(value) is False
