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
    assert "warnings.append(result.detail" in model
    assert "if let error = model.errorMessage" in login


def test_progress_staleness_and_running_completion_are_explicit():
    model = _swift("Core/AppModel.swift")
    dashboard = _swift("Views/DashboardView.swift")

    assert "progressLastSuccessAt" in model
    assert "progressConsecutiveFailures >= 3" in model
    assert "progress?.running == true && newProgress.running == false" in model
    assert "if completedRunningJob" in model
    assert 'model.progressIsStale ? "Status veraltet"' in dashboard
    assert 'case lastProgressAt = "last_progress_at"' in _swift("Core/Models.swift")
    assert "Letzter echter Fortschritt" in dashboard
    assert "Stillstands-Watchdog" in dashboard
    assert "Maximale Laufzeit" in dashboard


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


def test_legacy_jobs_feedback_and_native_push_contracts_are_wired():
    models = _swift("Core/Models.swift")
    config = _swift("Views/ConfigurationViews.swift")
    dashboard = _swift("Views/DashboardView.swift")
    api = _swift("Core/APIClient.swift")
    app = _swift("RcloneMobileApp.swift")
    push = _swift("Core/PushNotifications.swift")

    assert 'if values.contains(key("jobs"))' in models
    assert 'stableUUID5("rclone-job\\0' in models
    assert 'Picker("Rhythmus"' in config
    assert 'DatePicker("Uhrzeit"' in config
    assert (
        'TextField("Remote oder Zielpfad"' in config
        and 'Image(systemName: "folder")' in config
    )
    assert "ConfiguredCopyListRow" in dashboard
    assert '"/api/push/devices"' in api
    assert "registerForRemoteNotifications" in push
    assert "pushDeviceTokenReady" in app


def test_offline_logout_bounds_and_retries_push_revocation():
    model = _swift("Core/AppModel.swift")
    push = _swift("Core/PushNotifications.swift")
    app = _swift("RcloneMobileApp.swift")

    assert 'pendingPushRevocationsKey = "pendingPushRevocations"' in model
    assert "rememberPendingPushRevocation" in model
    assert "await retryPendingPushRevocations" in model
    assert "unregisterForRemoteNotifications" in push
    assert "pushCoordinator.unregisterLocally()" in app


def test_failed_run_retry_and_push_deep_link_are_revision_safe():
    api = _swift("Core/APIClient.swift")
    model = _swift("Core/AppModel.swift")
    push = _swift("Core/PushNotifications.swift")
    app = _swift("RcloneMobileApp.swift")
    root = _swift("Views/RootTabView.swift")
    runs = _swift("Views/BackupsView.swift")

    assert 'post("/api/jobs/\\(id)/retry?dry_run=\\(dryRun)")' in api
    assert "func retryJob(id: Int) async -> Bool" in model
    assert "requestedRunID" in model
    assert "pushNavigationRequested" in push
    assert 'userInfo["job_id"]' in push
    assert "consumePendingNavigationJobID" in push
    assert "pendingNavigationJobID = jobID" in push
    assert "model.requestRunNavigation(id: jobID)" in app
    assert "selectedTab = 3" in root
    assert "await openRequestedRun()" in runs
    assert "Job erneut starten" in runs
    assert "configRevision" in runs


def test_push_delivery_status_and_real_test_are_visible_in_system():
    api = _swift("Core/APIClient.swift")
    models = _swift("Core/Models.swift")
    system = _swift("Views/SystemView.swift")

    assert 'get("/api/push/status")' in api
    assert 'post("/api/push/test")' in api
    assert "struct PushStatus: Decodable" in models
    assert "PushStatusView()" in system
    assert "Endgültig fehlgeschlagen" in system
    assert "Testmitteilung senden" in system


def test_native_lifecycle_contracts_keep_drafts_sessions_and_proxy_paths_safe():
    api = _swift("Core/APIClient.swift")
    model = _swift("Core/AppModel.swift")
    config = _swift("Views/ConfigurationViews.swift")

    assert "draftBaseRevision" in config
    assert "baseRevision: draftBaseRevision" in config
    assert "draftRevision == currentConfig.revision" in model
    assert "revision: draftRevision" in model
    assert "pushSyncTask" in model
    assert "knownPushTokens" in model
    assert "reconcilePushRegistration" in model
    assert "lifecycleConfiguration.waitsForConnectivity = false" in api
    assert "lifecycleConfiguration.timeoutIntervalForResource = 6" in api
    assert (
        'components.percentEncodedPath = normalizedPath.isEmpty ? "" : "/" + normalizedPath'
        in api
    )


def test_native_batch_storage_and_central_401_contracts_are_explicit():
    models = _swift("Core/Models.swift")
    model = _swift("Core/AppModel.swift")
    dashboard = _swift("Views/DashboardView.swift")
    api = _swift("Core/APIClient.swift")

    assert "startedDefinitions" in models
    assert "queuedDefinitions" in models
    assert "BatchDefinitionState" in models
    assert "beginRunTracking(response)" in model
    assert "refreshVisibleRunData" in model
    assert "catch APIError.unauthenticated" in model
    assert "signOutLocally()" in model
    assert "StorageSizeState" in model
    assert "acceptStorageMeasurement" in model
    assert "markStorageMeasurementFailure" in model
    assert "StorageMeasurementStateView" in dashboard
    assert "timeout: includeSizes ? 85 : nil" in api


def test_native_accessibility_motion_and_recovery_messages_are_explicit():
    components = _swift("Views/Components.swift")
    login = _swift("Views/LoginView.swift")
    app = _swift("RcloneMobileApp.swift")
    api = _swift("Core/APIClient.swift")

    assert "@AccessibilityFocusState" in components
    assert "UIAccessibility.post(notification: .announcement" in components
    assert 'accessibilityLabel("Fehler.' in components
    assert (
        'accessibilityHint("Prüfe die Angaben oder versuche die Aktion erneut.'
        in components
    )
    assert "let accessibilityHint: String" in login
    assert ".accessibilityLabel(title)" in login
    assert ".accessibilityHint(accessibilityHint)" in login
    assert ".textContentType(contentType)" in login
    assert ".submitLabel(.go)" in login
    assert "@Environment(\\.accessibilityReduceMotion)" in app
    assert "reduceMotion ? nil : .smooth" in app
    assert "if reduceMotion" in app
    assert "Die Serverantwort konnte nicht geprüft werden" in api
    assert 'APIError.incompatibleResponse(resource: "Anmeldung")' in api


def test_native_push_permission_is_contextual_deferred_and_retriggerable():
    app = _swift("RcloneMobileApp.swift")
    push = _swift("Core/PushNotifications.swift")
    system = _swift("Views/SystemView.swift")

    assert '@AppStorage("pushPrimerDecision")' in app
    assert '.alert("Bei Sicherungsfehlern informieren?"' in app
    assert 'Button("Später", role: .cancel)' in app
    assert 'Button("Mitteilungen erlauben")' in app
    assert "requestAuthorizationAndRegister()" in app
    assert app.index('Button("Mitteilungen erlauben")') < app.index(
        "requestAuthorizationAndRegister()"
    )
    assert "registerIfAlreadyAuthorized" in push
    assert "notificationSettings()" in push
    assert "pushAuthorizationRequested" in push
    assert "pushAuthorizationRequested" in system
    assert "Mitteilungen aktivieren oder prüfen" in system


def test_native_tappable_rows_use_semantic_controls_with_specific_labels():
    backups = _swift("Views/BackupsView.swift")
    config = _swift("Views/ConfigurationViews.swift")

    assert ".onTapGesture" not in backups
    assert ".onTapGesture" not in config
    assert "Button { selectedPair = pair }" in backups
    assert "NavigationLink { RunDetailView(job: job) }" in backups
    assert "NavigationLink { DataPathDetailView" in backups
    assert 'accessibilityLabel("Job \\(pair.name), Status' in backups
    assert 'accessibilityLabel("Datenweg \\(pair.name)' in backups
    assert 'accessibilityLabel("Datenweg \\(pair.name) bearbeiten")' in config
    assert 'accessibilityLabel("Job \\(definition.name) bearbeiten")' in config
