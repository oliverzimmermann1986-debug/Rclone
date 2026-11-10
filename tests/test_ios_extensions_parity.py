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
        "struct BitrotScrubStatusResponse",
        "struct BootDrillStatusResponse",
        "struct PairConflictsResponse",
        "struct ConflictResolveResponse",
        "struct TextDiffResponse",
        "struct CloudQuotaResponse",
        "struct RansomwareStatusResponse",
        "struct CanaryInitResponse",
        "struct TimeTravelBrowseResponse",
        "struct TimeTravelHistoryResponse",
    ):
        assert model_name in text, f"Missing iOS model: {model_name}"


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
        '"/api/storage/quota',
        '"/api/security/ransomware-status"',
        '"/api/security/canary/',
        '"/api/recovery/timetravel/browse',
        '"/api/recovery/timetravel/history',
        '"/api/recovery/timetravel/diff',
    ):
        assert endpoint in text, f"Missing iOS APIClient endpoint: {endpoint}"


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
    ):
        assert view_name in ext_views, f"Missing iOS view: {view_name}"

    system_view = (IOS_VIEWS / "SystemView.swift").read_text(encoding="utf-8")
    assert "LockdownControlView()" in system_view
    assert "ObjectLockAndScrubView()" in system_view
    assert "CloudQuotaAndBudgetView()" in system_view

    recovery_view = (IOS_VIEWS / "RecoveryCenterView.swift").read_text(encoding="utf-8")
    assert "TimeTravelBrowserView(" in recovery_view
    assert "ConflictResolutionView(" in recovery_view
