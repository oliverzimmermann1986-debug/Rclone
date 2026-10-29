from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
IOS = ROOT / "ios" / "RcloneMobile"


def test_partial_proof_count_budget_and_semantics_reach_both_native_views():
    models = (IOS / "Core/Models.swift").read_text(encoding="utf-8")
    path = (IOS / "Views/ProtectionPathView.swift").read_text(encoding="utf-8")
    recovery = (IOS / "Views/RecoveryCenterView.swift").read_text(encoding="utf-8")
    assert models.count('case requestedSampleSize = "requested_sample_size"') == 2
    assert models.count('case budgetBytes = "budget_bytes"') == 2
    assert "requestedSampleSize ?? sampleSize" in models
    assert 'var isPartial: Bool { state == "partial" }' in models
    assert 'case "partial": return "Prüfumfang begrenzt"' in path
    assert 'case "partial": return .orange' in path
    assert "RestoreSampleScopeView(scope: scope)" in path
    assert "RestoreSampleScopeView(scope: dataPath.restore.sampleScope)" in recovery
    assert 'dataPath.restore.state == "failed" ? .red : .orange' in recovery


def test_incident_runtime_regressions_are_in_native_test_target():
    test = (ROOT / "ios/RcloneMobileTests/IncidentCenterTests.swift").read_text(
        encoding="utf-8"
    )
    for name in (
        "testBlankHealthErrorsAndSuccessfulHealthNeverCreateEmptyOrStaleIncidents",
        "testBlankRestoreErrorUsesActionableFailureFallback",
        "testLimitedSampleShowsRequestedCountBudgetAndOrangeCategoryWithoutValidProof",
        "testDuplicateMergesNavigationContextAndMaximumSeverity",
        "testSameJobPartialSummaryMergesIntoDetailedPairFinding",
        "testRealFailureWithPartialWordingStaysRed",
        "testNewProofFieldsDecodeInBothEndpointsAndOldResponsesRemainCompatible",
        "testHistoricalDisplayStatusDoesNotRewriteStoredJobOutcome",
    ):
        assert f"func {name}(" in test
    assert "XCTAssertEqual(incident.color, Color.orange)" in test
    assert "XCTAssertEqual(incident.color, Color.red)" in test


def test_historical_job_display_status_is_separate_and_used_for_visible_badges():
    models = (IOS / "Core/Models.swift").read_text(encoding="utf-8")
    backups = (IOS / "Views/BackupsView.swift").read_text(encoding="utf-8")
    assert "var effectiveStatus: String { displayStatus ?? status }" in models
    assert 'case displayStatus = "display_status"' in models
    assert "StatusBadge(status: currentJob.effectiveStatus)" in backups
    assert "StatusBadge(status: job.effectiveStatus)" in backups
    assert "StatusStyle.label(for: job.effectiveStatus)" in backups
