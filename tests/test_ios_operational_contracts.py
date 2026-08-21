"""Statische iOS-Gates für Betriebszustände, bis Xcode auf macOS kompiliert."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _swift(relative: str) -> str:
    return (ROOT / "ios" / "RcloneMobile" / relative).read_text(encoding="utf-8")


def test_logout_warning_survives_local_session_clear():
    api = _swift("Core/APIClient.swift")
    model = _swift("Core/AppModel.swift")
    login = _swift("Views/LoginView.swift")

    assert "func logout() async throws -> LogoutResult" in api
    assert "partial.localSessionCleared" in api
    assert "defer { clearCookies() }" in api
    assert "!result.globalRevocation" in model
    assert "errorMessage = result.detail" in model
    assert "if let error = model.errorMessage" in login


def test_progress_staleness_and_running_completion_are_explicit():
    model = _swift("Core/AppModel.swift")
    dashboard = _swift("Views/DashboardView.swift")

    assert "progressLastSuccessAt" in model
    assert "progressConsecutiveFailures >= 3" in model
    assert "progress?.running == true && newProgress.running == false" in model
    assert "if completedRunningJob" in model
    assert 'model.progressIsStale ? "Status veraltet"' in dashboard


def test_storage_measurements_support_cache_age_and_explicit_recalculation():
    api = _swift("Core/APIClient.swift")
    models = _swift("Core/Models.swift")
    dashboard = _swift("Views/DashboardView.swift")

    assert "refresh_sizes=\\(forceRefresh ?" in api
    assert 'case measuredAt = "measured_at"' in models
    assert 'case measurementStatus = "measurement_status"' in models
    assert "await model.refreshStorageSizes()" in dashboard
    assert 'case "stale"' in dashboard


def test_diagnostics_retry_and_app_build_are_visible():
    system = _swift("Views/SystemView.swift")
    settings = _swift("Views/RootTabView.swift")

    assert "value: AppFormat.date(checkedAt.timeIntervalSince1970)" in system
    assert 'accessibilityIdentifier("refreshDoctorButton")' in system
    assert (
        'model.doctor == nil ? "Systemdiagnose ausführen" : "Erneut prüfen"' in system
    )
    assert 'object(forInfoDictionaryKey: "CFBundleShortVersionString")' in settings
    assert 'object(forInfoDictionaryKey: "CFBundleVersion")' in settings


def test_operations_hub_is_always_reachable_once_and_filter_dirty_state_is_derived():
    system = _swift("Views/SystemView.swift")
    operations = _swift("Views/OperationalViews.swift")

    assert system.count("NavigationLink { OperationsHubView() }") == 1
    assert "private var isDirty: Bool { filter?.content != content }" in operations
    assert ".onChange(of: content) { _, _ in isDirty = true }" not in operations
