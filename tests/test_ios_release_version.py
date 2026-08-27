from pathlib import Path

import pytest
import yaml

from scripts.ios_release_version import (
    apply_marketing_version,
    configured_marketing_version,
    version_from_tag,
)


@pytest.mark.parametrize(
    ("tag", "version"),
    [("ios-v1.0.0", "1.0.0"), ("ios-v12.34.56", "12.34.56")],
)
def test_release_tag_maps_to_marketing_version(tag: str, version: str):
    assert version_from_tag(tag) == version


@pytest.mark.parametrize(
    "tag",
    ["v1.2.3", "ios-v1.2", "ios-v1.2.3-beta", "ios-v01.2.3", "ios-v*"],
)
def test_invalid_release_tags_are_rejected(tag: str):
    with pytest.raises(ValueError):
        version_from_tag(tag)


def test_marketing_version_is_updated_exactly_once(tmp_path: Path):
    project_file = tmp_path / "project.yml"
    project_file.write_text(
        'settings:\n  base:\n    MARKETING_VERSION: "1.0.0"\n', encoding="utf-8"
    )
    apply_marketing_version(project_file, "2.4.6")
    assert configured_marketing_version(project_file) == "2.4.6"


def test_duplicate_marketing_version_is_rejected(tmp_path: Path):
    project_file = tmp_path / "project.yml"
    project_file.write_text(
        'MARKETING_VERSION: "1.0.0"\nMARKETING_VERSION: "2.0.0"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        apply_marketing_version(project_file, "3.0.0")


def test_codemagic_applies_tag_before_generation_and_keeps_monotonic_build_number():
    root = Path(__file__).parents[1]
    config = (root / "codemagic.yaml").read_text(encoding="utf-8")
    generate_command = '"$CM_BUILD_DIR/.tools/xcodegen/bin/xcodegen" generate'
    assert config.index("ios_release_version.py") < config.index(generate_command)
    assert 'agvtool new-version -all "$BUILD_NUMBER"' in config
    assert "CFBundleShortVersionString" in config
    assert "CFBundleVersion" in config


def test_codemagic_verifies_fetched_main_and_exact_signed_tag_before_release_changes():
    root = Path(__file__).parents[1]
    config = (root / "codemagic.yaml").read_text(encoding="utf-8")
    provenance = config.index("ios_release_preflight.py")
    version_write = config.index("ios_release_version.py")

    assert "refs/heads/main:refs/remotes/origin/main" in config
    assert "refs/tags/$CM_TAG:refs/tags/$CM_TAG" in config
    assert "--unshallow --force --no-tags" in config
    assert "IOS_RELEASE_GPG_PUBLIC_KEY_B64" in config
    assert config.index("gpg --batch --quiet --import") < provenance
    assert provenance < version_write
    assert "tests/test_ios_release_preflight.py" in config


def test_codemagic_validates_live_backend_contracts_before_xcode_build():
    root = Path(__file__).parents[1]
    config = (root / "codemagic.yaml").read_text(encoding="utf-8")
    normalized = " ".join(config.split())
    dependency_install = (
        'python3 -m pip install -r "$CM_BUILD_DIR/requirements-dev.txt"'
    )
    contract_test_run = (
        "python3 -m pytest tests/test_native_login_contract.py "
        "tests/test_native_read_contract.py tests/test_install_pinned_xcodegen.py "
        "tests/test_ios_release_preflight.py tests/test_ios_release_version.py"
    )

    assert dependency_install in normalized
    assert contract_test_run in normalized
    assert normalized.index(dependency_install) < normalized.index(contract_test_run)
    generate_command = '"$CM_BUILD_DIR/.tools/xcodegen/bin/xcodegen" generate'
    assert normalized.index(contract_test_run) < normalized.index(generate_command)
    assert normalized.index(contract_test_run) < normalized.index(
        "xcode-project build-ipa"
    )


def test_codemagic_uploads_public_app_store_eligible_build_without_auto_submission():
    root = Path(__file__).parents[1]
    config = (root / "codemagic.yaml").read_text(encoding="utf-8")

    assert "testFlightInternalTestingOnly" not in config
    assert "submit_to_testflight: false" in config
    assert "submit_to_app_store: false" in config


def test_codemagic_pins_the_verified_simulator_image():
    root = Path(__file__).parents[1]
    config = (root / "codemagic.yaml").read_text(encoding="utf-8")

    assert "xcode: 26.4" in config
    assert '-destination "platform=iOS Simulator,name=iPhone 17,OS=latest"' in config


def test_codemagic_uses_checksum_verified_exact_xcodegen_before_generation():
    root = Path(__file__).parents[1]
    config = (root / "codemagic.yaml").read_text(encoding="utf-8")
    project = yaml.safe_load((root / "ios" / "project.yml").read_text(encoding="utf-8"))

    install = config.index("install_pinned_xcodegen.py")
    generate = config.index('"$CM_BUILD_DIR/.tools/xcodegen/bin/xcodegen" generate')
    assert "brew install xcodegen" not in config
    assert install < generate
    assert "tests/test_install_pinned_xcodegen.py" in config
    assert project["options"]["minimumXcodeGenVersion"] == "2.46.0"


def test_ios_ci_tracks_native_contract_sources():
    root = Path(__file__).parents[1]
    workflow = (root / ".github" / "workflows" / "ios.yml").read_text(encoding="utf-8")
    for expected_path in (
        '"app/main.py"',
        '"app/auth_contract.py"',
        '"app/config_store.py"',
        '"app/config_validation.py"',
        '"app/db.py"',
        '"app/job_definitions.py"',
        '"app/jobs/**"',
        '"app/routes/**"',
        '"contracts/**"',
    ):
        assert workflow.count(expected_path) == 2


def test_shared_contracts_are_copied_into_ios_test_bundle():
    root = Path(__file__).parents[1]
    project = yaml.safe_load((root / "ios" / "project.yml").read_text(encoding="utf-8"))
    test_target = project["targets"]["RcloneMobileTests"]
    resource_sources = {
        source["path"]
        for source in test_target["sources"]
        if isinstance(source, dict) and source.get("buildPhase") == "resources"
    }

    assert resource_sources == {
        "../contracts/native_login_contract.json",
        "../contracts/native_read_contract_v1.json",
        "RcloneMobile/StorePreviewData.json",
    }
