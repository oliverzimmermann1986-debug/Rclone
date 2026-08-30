from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
IOS = ROOT / "ios"


def _read(path: str) -> str:
    return (IOS / path).read_text(encoding="utf-8")


def test_native_recovery_center_uses_real_server_endpoints_and_safe_staging():
    api = _read("RcloneMobile/Core/APIClient.swift")
    view = _read("RcloneMobile/Views/RecoveryCenterView.swift")
    system = _read("RcloneMobile/Views/SystemView.swift")

    for endpoint in (
        "/api/recovery/pass",
        "/api/recovery/calendar",
        "/api/recovery/policies",
        "/api/recovery/quarantine/",
        "/api/recovery/browse",
        "/api/recovery/restore",
        "/api/recovery/handover",
        "/api/recovery/points",
        "/api/recovery/diff",
    ):
        assert endpoint in api
    assert "RecoveryCenterView()" in system
    assert "Getrennt wiederherstellen" in view
    assert "Produktive Quell- und Zielpfade werden nicht verändert" in view
    assert "Notfallübung starten" in view
    assert "RPO" in view and "RTO-Stichprobe" in view
    assert "func loadDemo()" in view
    assert "model.isDemoMode" in view
    assert "Recovery-Zeitreise" in view
    assert "getRecoveryDiff" in api


def test_device_vault_is_native_resumable_and_visible_in_demo():
    api = _read("RcloneMobile/Core/APIClient.swift")
    vault = _read("RcloneMobile/Views/DeviceVaultView.swift")
    transfer = _read("RcloneMobile/Core/VaultTransfer.swift")
    dashboard = _read("RcloneMobile/Views/DashboardView.swift")

    for endpoint in ("/api/vault/uploads", "/api/vault/library"):
        assert endpoint in api
    assert "PhotosPicker" in vault
    assert ".fileImporter" in vault
    assert "Demo-Sicherung abspielen" in vault
    assert "Geräte-Vault" in dashboard
    assert "1024 * 1024" in transfer
    assert "SHA256" in transfer


def test_offline_card_multi_server_and_encrypted_handover_are_explicit():
    view = _read("RcloneMobile/Views/RecoveryCenterView.swift")
    model = _read("RcloneMobile/Core/AppModel.swift")
    login = _read("RcloneMobile/Views/LoginView.swift")

    assert 'forKey: "offlineRecoveryPass"' in view
    assert "keine Serverpfade, Passwörter oder Cloud-Schlüssel" in view
    assert "AES-256-GCM" in view
    assert "SavedServerProfile" in model
    assert 'forKey: "savedServerProfiles"' in model
    assert "model.savedServerProfiles" in login
    assert "Das Passwort wird nicht gespeichert" in login


def test_widget_live_activity_shortcuts_and_notification_actions_are_wired():
    project = yaml.safe_load(_read("project.yml"))
    app_sources = project["targets"]["RcloneMobile"]["sources"]
    widget = project["targets"]["RcloneProtectionWidget"]
    shared = _read("Shared/ProtectionShared.swift")
    widget_source = _read("RcloneProtectionWidget/RcloneProtectionWidget.swift")
    shortcuts = _read("RcloneMobile/Core/ProtectionShortcuts.swift")
    push = _read("RcloneMobile/Core/PushNotifications.swift")

    assert {item["path"] for item in app_sources} >= {"RcloneMobile", "Shared"}
    assert widget["type"] == "app-extension"
    assert "ProtectionWidgetSnapshot" in shared
    assert "ActivityConfiguration" in widget_source
    assert "DynamicIsland" in widget_source
    assert "AppShortcutsProvider" in shortcuts
    assert 'identifier: "RCLONE_INCIDENT"' in push
    assert 'identifier: "PAUSE_SCHEDULES"' in push
    assert ".authenticationRequired" in push


def test_app_group_is_identical_for_app_widget_and_shared_store():
    group = "group.de.oliverzimmermann.rclonesync"
    assert group in _read("Shared/ProtectionShared.swift")
    assert group in _read("RcloneMobile/RcloneMobile.entitlements")
    assert group in _read("RcloneProtectionWidget/RcloneProtectionWidget.entitlements")
    assert "NSPrivacyAccessedAPICategoryUserDefaults" in _read(
        "RcloneProtectionWidget/PrivacyInfo.xcprivacy"
    )
