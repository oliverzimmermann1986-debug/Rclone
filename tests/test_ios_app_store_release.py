import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_public_release_build_is_not_restricted_to_internal_testflight():
    workflow = (ROOT / "codemagic.yaml").read_text(encoding="utf-8")

    assert "testFlightInternalTestingOnly" not in workflow
    assert "Capture localized App Store screenshots" in workflow
    assert "build/app-store-screenshots/*.png" in workflow
    assert "--store-preview" in workflow


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


def test_support_and_privacy_pages_are_publishable_without_tracking():
    support = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")
    privacy = (ROOT / "docs" / "datenschutz.html").read_text(encoding="utf-8")

    assert "Rclone Sync" in support
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
