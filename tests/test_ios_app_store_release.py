import json
import os
import shutil
import struct
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_public_release_build_is_not_restricted_to_internal_testflight():
    workflow = (ROOT / "codemagic.yaml").read_text(encoding="utf-8")

    assert "testFlightInternalTestingOnly" not in workflow
    assert "Capture localized App Store screenshots" in workflow
    assert "build/app-store-screenshots/*.png" in workflow
    assert "--store-preview" in workflow
    assert "dashboard vault recovery protect system" in workflow


def test_signed_bundle_gate_verifies_both_extensions_independent_of_directory_order():
    workflow = (ROOT / "codemagic.yaml").read_text(encoding="utf-8")
    assert 'WIDGET_PATH="$APP_PATH/PlugIns/RcloneProtectionWidget.appex"' in workflow
    assert 'SHARE_PATH="$APP_PATH/PlugIns/RcloneShareExtension.appex"' in workflow
    assert 'test -d "$SHARE_PATH"' in workflow
    assert 'test -d "$WIDGET_PATH"' in workflow
    assert "com.apple.share-services" in workflow
    assert "share-entitlements.plist" in workflow
    assert 'codesign --verify --deep --strict "$APP_PATH"' in workflow


def test_simulator_keychain_tests_keep_ad_hoc_signing_enabled():
    native_ci = (ROOT / ".github" / "workflows" / "ios.yml").read_text(encoding="utf-8")
    release_ci = (ROOT / "codemagic.yaml").read_text(encoding="utf-8")
    for workflow in (native_ci, release_ci):
        assert 'CODE_SIGNING_ALLOWED=YES CODE_SIGN_IDENTITY="-"' in workflow
        assert "CODE_SIGNING_ALLOWED=NO" not in workflow
    assert 'codesign -d --entitlements :- "$SIM_APP"' in native_ci
    project = (ROOT / "ios" / "project.yml").read_text(encoding="utf-8")
    test_target = project.split("  RcloneMobileTests:", 1)[1]
    assert "GENERATE_INFOPLIST_FILE: true" in test_target


def release_test_step():
    workflow = yaml.safe_load((ROOT / "codemagic.yaml").read_text(encoding="utf-8"))
    scripts = workflow["workflows"]["ios-testflight"]["scripts"]
    return next(
        step["script"]
        for step in scripts
        if step["name"] == "Run iOS unit and UI tests"
    )


def test_release_tests_use_available_simulator_serially_and_publish_results():
    script = release_test_step()
    assert (
        'SIMULATOR_ID="$(python3 "$CM_BUILD_DIR/scripts/ios_simulator_destination.py")"'
        in script
    )
    assert '-destination "platform=iOS Simulator,id=$SIMULATOR_ID"' in script
    assert "-parallel-testing-enabled NO" in script
    assert '-resultBundlePath "$RESULT_BUNDLE"' in script
    assert "name=iPhone" not in script
    workflow = yaml.safe_load((ROOT / "codemagic.yaml").read_text(encoding="utf-8"))
    assert (
        "build/ios-tests.xcresult.zip"
        in workflow["workflows"]["ios-testflight"]["artifacts"]
    )


def bash_for_release_gate():
    bash = shutil.which("bash")
    if bash:
        return bash
    git = shutil.which("git")
    if git:
        git_bash = Path(git).parent.parent / "bin" / "bash.exe"
        if git_bash.is_file():
            return str(git_bash)
    pytest.skip("Bash is required to exercise the release test gate")


def shell_path(path):
    resolved = path.resolve()
    if os.name == "nt" and resolved.drive:
        return f"/{resolved.drive[0].lower()}/{resolved.relative_to(resolved.anchor).as_posix()}"
    return str(resolved)


@pytest.mark.parametrize(
    ("test_status", "archive_status", "expected_status"),
    [(0, 0, 0), (65, 0, 65), (65, 23, 65), (0, 23, 23)],
)
def test_release_gate_archives_results_and_preserves_failure_status(
    tmp_path, test_status, archive_status, expected_status
):
    tools = tmp_path / "tools"
    tools.mkdir()
    stubs = {
        "python3": 'printf "%s\\n" simulator-udid\n',
        "xcodebuild": (
            'printf "%s\\n" "$@" > "$CM_BUILD_DIR/xcodebuild-arguments"\n'
            'while test "$#" -gt 0; do\n'
            '  if test "$1" = -resultBundlePath; then\n'
            '    shift; mkdir -p "$1"; printf result > "$1/result.txt"\n'
            "  fi\n"
            "  shift\n"
            "done\n"
            'exit "$FAKE_TEST_STATUS"\n'
        ),
        "ditto": (
            'printf attempted > "$CM_BUILD_DIR/archive-attempted"\n'
            'test "$FAKE_ARCHIVE_STATUS" -eq 0 || exit "$FAKE_ARCHIVE_STATUS"\n'
            'for destination in "$@"; do :; done\n'
            'printf archived > "$destination"\n'
        ),
    }
    for name, body in stubs.items():
        executable = tools / name
        executable.write_text("#!/bin/sh\n" + body, encoding="utf-8", newline="\n")
        executable.chmod(0o755)
    environment = {
        **os.environ,
        "CM_BUILD_DIR": shell_path(tmp_path),
        "XCODE_PROJECT": "ios/RcloneMobile.xcodeproj",
        "XCODE_SCHEME": "RcloneMobile",
        "TEST_TOOLS": shell_path(tools),
        "FAKE_TEST_STATUS": str(test_status),
        "FAKE_ARCHIVE_STATUS": str(archive_status),
    }
    result = subprocess.run(
        [
            bash_for_release_gate(),
            "--noprofile",
            "--norc",
            "-c",
            'export PATH="$TEST_TOOLS:$PATH"\n' + release_test_step(),
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == expected_status, result.stderr
    assert (tmp_path / "archive-attempted").is_file()
    assert (tmp_path / "build" / "ios-tests.xcresult.zip").exists() == (
        archive_status == 0
    )
    arguments = (
        (tmp_path / "xcodebuild-arguments").read_text(encoding="utf-8").splitlines()
    )
    assert (
        arguments[arguments.index("-destination") + 1]
        == "platform=iOS Simulator,id=simulator-udid"
    )
    assert arguments[arguments.index("-parallel-testing-enabled") + 1] == "NO"
    assert "CODE_SIGN_IDENTITY=-" in arguments


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
