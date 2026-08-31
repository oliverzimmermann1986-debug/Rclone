import json
import struct
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_public_release_build_is_not_restricted_to_internal_testflight():
    workflow = (ROOT / "codemagic.yaml").read_text(encoding="utf-8")

    assert "testFlightInternalTestingOnly" not in workflow
    assert "Capture localized App Store screenshots" in workflow
    assert "build/app-store-screenshots/*.png" in workflow
    assert "--store-preview" in workflow
    assert "dashboard vault paths jobs system" in workflow


def test_store_preview_fixture_covers_all_primary_tabs():
    fixture = json.loads(
        (ROOT / "ios" / "RcloneMobile" / "StorePreviewData.json").read_text(
            encoding="utf-8"
        )
    )

    assert fixture["overview"]["alerts"] == []
    assert len(fixture["storage"]["pairs"]) >= 2
    assert len(fixture["config"]["backup"]["pairs"]) >= 2
    assert len(fixture["config"]["backup"]["jobs"]) >= 2
    assert len(fixture["jobs"]) >= 3
    assert fixture["doctor"]["ok"] is True


def test_vault_store_preview_renders_directly_without_a_dimming_sheet():
    app = (ROOT / "ios" / "RcloneMobile" / "RcloneMobileApp.swift").read_text(
        encoding="utf-8"
    )

    assert "if StorePreviewMode.opensDeviceVault" in app
    assert "NavigationStack { DeviceVaultView() }" in app


def test_support_and_privacy_pages_are_publishable_without_tracking():
    support = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")
    privacy = (ROOT / "docs" / "datenschutz.html").read_text(encoding="utf-8")

    assert "Sicherpfad" in support
    assert "Datenschutzerklärung" in privacy
    assert "keine personenbezogenen Daten" in privacy
    assert "analytics" not in (support + privacy).lower()
    assert "<script" not in (support + privacy).lower()


def test_siri_intent_descriptions_avoid_reserved_device_names():
    shortcuts = (
        ROOT / "ios" / "RcloneMobile" / "Core" / "ProtectionShortcuts.swift"
    ).read_text(encoding="utf-8")

    descriptions = [
        line.lower() for line in shortcuts.splitlines() if "IntentDescription(" in line
    ]
    assert descriptions
    assert all("iphone" not in description for description in descriptions)


def test_sicherpfad_brand_and_app_icon_are_release_ready():
    info = (ROOT / "ios" / "RcloneMobile" / "Info.plist").read_text(encoding="utf-8")
    icon = (
        ROOT
        / "ios"
        / "RcloneMobile"
        / "Assets.xcassets"
        / "AppIcon.appiconset"
        / "AppIcon.png"
    ).read_bytes()

    assert "<string>Sicherpfad</string>" in info
    assert icon[:8] == b"\x89PNG\r\n\x1a\n"
    assert struct.unpack(">II", icon[16:24]) == (1024, 1024)


def test_native_user_facing_brand_no_longer_uses_old_app_name():
    native_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "ios" / "RcloneMobile").rglob("*")
        if path.suffix in {".swift", ".plist", ".json"}
    )

    assert "Rclone Sync" not in native_sources
    assert "Rclone-Sync" not in native_sources
