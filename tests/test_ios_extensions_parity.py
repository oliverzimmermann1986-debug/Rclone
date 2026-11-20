"""Contract tests for iOS operational extensions parity (Feature 6)."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IOS_CORE = ROOT / "ios" / "RcloneMobile" / "Core"
IOS_VIEWS = ROOT / "ios" / "RcloneMobile" / "Views"


def test_ios_operational_extension_models_exist() -> None:
    text = (IOS_CORE / "OperationalExtensionModels.swift").read_text(encoding="utf-8")
    for model_name in (
        "struct LockdownStatusResponse",
        "struct LockdownActivationResponse",
        "struct LockdownReleaseResponse",
        "struct ObjectLockReportResponse",
        "struct DueStatusDetail",
        "struct BitrotScrubStatusResponse",
        "struct BootDrillStatusResponse",
        "struct PairConflictsResponse",
        "struct ConflictResolveResponse",
        "struct TextDiffResponse",
        "struct CloudQuotaResponse",
        "struct RansomwareStatusResponse",
        "struct CanaryInitResponse",
        "struct CanaryVerifyResponse",
        "struct TimeTravelBrowseResponse",
        "struct TimeTravelHistoryResponse",
        "struct CryptCandidatesResponse",
        "struct CryptCreateResponse",
        "struct VersionsPreviewResponse",
        "struct BandwidthHeatmapResponse",
        "struct BandwidthSuggestionsResponse",
    ):
        assert model_name in text, f"Missing iOS model: {model_name}"
    assert "container.decode(DueStatusDetail.self, forKey: .due)" in text
    assert "container.decode(Bool.self, forKey: .due)" in text


def test_ios_api_client_exposes_all_operational_endpoints() -> None:
    text = (IOS_CORE / "APIClient.swift").read_text(encoding="utf-8")
    for endpoint in (
        '"/api/lockdown"',
        '"/api/lockdown/release"',
        '"/api/storage/object-lock',
        '"/api/jobs/bitrot-scrub/status"',
        '"/api/jobs/bitrot-scrub"',
        '"/api/pbs/boot-drill/status"',
        '"/api/pbs/boot-drill"',
        '/conflicts"',
        '/conflicts/resolve"',
        '"/api/recovery/staging/',
        '/download"',
        '"/api/storage/quota',
        '"/api/security/ransomware-status"',
        '"/api/security/canary/',
        '/verify"',
        '"/api/recovery/timetravel/browse',
        '"/api/recovery/timetravel/history',
        '"/api/recovery/timetravel/diff',
        '"/api/remotes/crypt/candidates"',
        '"/api/remotes/crypt"',
        '"/api/jobs/versions/preview',
        '"/api/jobs/versions/prune"',
        '"/api/jobs/bandwidth/heatmap',
        '"/api/jobs/bandwidth/suggestions"',
        '"/api/jobs/progress/stream"',
    ):
        assert endpoint in text, f"Missing iOS APIClient endpoint: {endpoint}"
    assert "func verifyPairCanary(pairName: String)" in text
    assert "func downloadRecoveryStagingArchive(recoveryID: String)" in text


def test_ios_operational_extension_views_and_navigation() -> None:
    ext_views = (IOS_VIEWS / "OperationalExtensionsView.swift").read_text(
        encoding="utf-8"
    )
    for view_name in (
        "struct LockdownControlView",
        "struct ObjectLockAndScrubView",
        "struct CloudQuotaAndBudgetView",
        "struct ConflictResolutionView",
        "struct LineDiffViewerView",
        "struct TimeTravelBrowserView",
        "struct CryptRemoteWizardView",
        "struct VersionsLifecycleView",
        "struct BandwidthHeatmapView",
    ):
        assert view_name in ext_views, f"Missing iOS view: {view_name}"

    assert '$0.level == "immutable"' in ext_views
    assert '$0.level == "locked"' not in ext_views
    assert 'case "governance":' in ext_views
    assert 'case "versioned":' in ext_views
    assert 'last.status == "success"' not in ext_views
    assert "StatusBadge(status: last.effectiveStatus)" in ext_views
    assert "verifyPairCanary(pairName: pairName)" in ext_views
    assert "getTimeTravelHistory(identity: identity, path: item.path)" in ext_views
    assert "Zeilen eingefügt" in ext_views
    assert "Zeilen gelöscht" in ext_views
    assert "Zeilen geändert" in ext_views
    assert "Zeilen unverändert" in ext_views

    system_view = (IOS_VIEWS / "SystemView.swift").read_text(encoding="utf-8")
    assert "LockdownControlView()" in system_view
    assert "ObjectLockAndScrubView()" in system_view
    assert "CloudsScreen(showingSettings: $showingSettings)" in system_view
    assert "VersionsLifecycleView()" in system_view
    assert "BandwidthHeatmapView()" in system_view

    clouds_view = (IOS_VIEWS / "CloudsScreen.swift").read_text(encoding="utf-8")
    assert 'browseRemote(path: "")' in clouds_view
    assert (
        "DataPathsScreen(showingSettings: $showingSettings, cloudPath: remote.path)"
        in clouds_view
    )
    assert "CloudQuotaAndBudgetView()" in clouds_view
    assert "CryptRemoteWizardView()" in clouds_view
    assert "RemoteSetupView()" in clouds_view

    recovery_view = (IOS_VIEWS / "RecoveryCenterView.swift").read_text(encoding="utf-8")
    assert "TimeTravelBrowserView(" in recovery_view
    assert "ConflictResolutionView(" in recovery_view
