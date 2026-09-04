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


def test_storage_cards_drill_into_native_privacy_preserving_charts():
    api = _swift("Core/APIClient.swift")
    models = _swift("Core/Models.swift")
    dashboard = _swift("Views/DashboardView.swift")
    detail = _swift("Views/ProtectionPathView.swift")

    assert '"/api/storage/composition?pair=' in api
    assert "struct StorageCompositionResponse: Decodable, Equatable" in models
    assert "Datenweg antippen, um Größenvergleich und Dateitypen zu sehen." in dashboard
    assert "import Charts" in detail
    assert "SectorMark(" in detail
    assert "BarMark(" in detail
    assert 'Picker("Speicherort"' in detail
    assert "Dateinamen bleiben auf dem Server" in detail


def test_dashboard_exposes_distinct_evidence_based_protection_score():
    models = _swift("Core/Models.swift")
    model = _swift("Core/AppModel.swift")
    dashboard = _swift("Views/DashboardView.swift")
    protection_path = _swift("Views/ProtectionPathView.swift")

    assert 'Text("SCHUTZSTATUS")' in dashboard
    assert 'case .ok: "Bereit"' in dashboard
    assert 'case .warning: "Prüfen"' in dashboard
    assert 'case .error: "Handeln"' in dashboard
    assert "DataPathSignatureMark" in dashboard
    assert "RestoreNodeMark" not in dashboard
    assert "nextProtectionAction" in dashboard
    assessment = _swift("Views/ProtectionAssessmentView.swift")
    incidents = _swift("Views/IncidentCenterView.swift")
    assert "ProtectionAssessment" in dashboard
    assert 'Text("VERTRAUENSSCORE")' in assessment
    assert "Restore-Nachweise" in assessment
    assert "Frische erfolgreiche Läufe" in assessment
    assert "Schutzschild" in assessment
    assert "Incident Center" in incidents
    assert "Empfohlener nächster Schritt" in incidents
    assert 'return (\n                "Restore-Test"' in incidents
    assert "activeRestoreTestPairs" in model
    assert "isRestoreTestRunning(for pair: String)" in model
    assert "trackingRestorePairs" in model
    assert "isRestoreTesting: model.isRestoreTestRunning(for: pair.name)" in dashboard
    assert "current.isEmpty" in _swift("Views/RecoveryCenterView.swift")
    assert "struct RestoreEvidence: Decodable, Equatable" in models
    assert 'case restoreEvidence = "restore_evidence"' in models
    assert "ProtectionPathDetailView" in dashboard
    assert 'Text("RESTORE-NACHWEIS")' in protection_path
    assert 'Section("Schutzpfad")' in protection_path
    assert 'Section("Schutzschild")' in protection_path
    assert "await model.runRestoreTest(pair: pair.name)" in protection_path
    assert 'case "passed": return "Wiederherstellbar"' in protection_path
    assert 'case "passed": return .green' in protection_path
    assert 'case "passed": return "checkmark.seal.fill"' in protection_path
    assert 'case "never": return "Restore offen"' in protection_path
    assert 'case "never": return .orange' in protection_path
    assert "default: return .secondary" in protection_path


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


def test_session_restore_revokes_pending_push_before_loading_credentials():
    model = _swift("Core/AppModel.swift")

    restore = model[
        model.index("func restoreSession() async") : model.index("func login(")
    ]
    assert "try await revokePendingPushRegistrationsBeforeRestore" in restore
    assert restore.index(
        "try await revokePendingPushRegistrationsBeforeRestore"
    ) < restore.index("try await newClient.getConfig()")
    assert restore.index(
        "try await revokePendingPushRegistrationsBeforeRestore"
    ) < restore.index("client = newClient")
    assert "_ = try await client.unregisterPushDevice" in model


def test_stored_http_session_restore_is_fail_closed_and_manual_login_reconfirms():
    api = _swift("Core/APIClient.swift")
    model = _swift("Core/AppModel.swift")
    login = _swift("Views/LoginView.swift")

    assert "requiresExplicitInsecureTransportConfirmation" in api
    restore = model[
        model.index("func restoreSession() async") : model.index("func login(")
    ]
    assert "if APIClient.requiresExplicitInsecureTransportConfirmation(url)" in restore
    assert "newClient.clearLocalSession()" in restore
    assert restore.index(
        "requiresExplicitInsecureTransportConfirmation"
    ) < restore.index("try await newClient.getConfig()")
    assert "if APIClient.requiresExplicitInsecureTransportConfirmation(url)" in login
    assert "showHTTPWarning = true" in login


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


def test_native_target_browser_can_switch_storage_and_create_folders():
    api = _swift("Core/APIClient.swift")
    models = _swift("Core/Models.swift")
    operations = _swift("Views/OperationalViews.swift")
    config = _swift("Views/ConfigurationViews.swift")

    assert "func createDirectory(kind: String, parent: String, name: String)" in api
    assert '"/api/browse/directory"' in api
    assert "struct CreateDirectoryRequest: Encodable" in models
    assert 'Picker("Zieltyp", selection: $selectedKind)' in operations
    assert 'Label("Cloud", systemImage: "icloud")' in operations
    assert 'Label("Lokal", systemImage: "externaldrive")' in operations
    assert (
        'Label("Neuen Ordner anlegen", systemImage: "folder.badge.plus")' in operations
    )
    assert "allowsKindSwitch: target == .remote" in operations
    assert "allowsKindSwitch: target == .remote" in config
    assert "createDirectory(" in operations
