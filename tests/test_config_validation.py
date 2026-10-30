from pathlib import Path

import pytest
import yaml

from app.config_validation import (
    ConfigValidationError,
    SESSION_SECRET_PLACEHOLDER,
    session_secret_strength_error,
    validate_config,
)


def _base(tmp_path: Path) -> dict:
    return {
        "web": {"username": "admin", "local_browse_roots": [str(tmp_path)]},
        "paths": {
            "data_dir": str(tmp_path),
            "logs_dir": str(tmp_path / "logs"),
            "temp_dir": str(tmp_path / "temp"),
        },
        "backup": {"pairs": [], "default_schedule": "manual"},
        "notifications": {"webhooks": []},
    }


def test_default_and_installer_example_use_consistent_security_policy(tmp_path: Path):
    defaults, _ = validate_config(_base(tmp_path))
    assert defaults["web"]["secure_cookie"] == "auto"
    assert Path(defaults["paths"]["recovery_dir"]) == tmp_path / "recovery"
    assert set(defaults["web"]["allowed_hosts"]) == {"localhost", "127.0.0.1", "::1"}
    example = Path(__file__).resolve().parents[1] / "config" / "config.example.yaml"
    normalized, _ = validate_config(yaml.safe_load(example.read_text(encoding="utf-8")))
    assert normalized["web"]["secure_cookie"] == "auto"
    assert "127.0.0.1" in normalized["web"]["allowed_hosts"]
    assert "*" not in normalized["web"]["allowed_hosts"]
    assert normalized["paths"]["recovery_dir"] == "/opt/rclone-sync/data/recovery"


@pytest.mark.parametrize("value", [None, " auto ", "AUTO"])
def test_auto_cookie_modes_cannot_silently_disable_https_cookies(tmp_path, value):
    config = _base(tmp_path)
    config["web"]["secure_cookie"] = value
    normalized, _ = validate_config(config)
    assert normalized["web"]["secure_cookie"] == "auto"


@pytest.mark.parametrize("value", ["typo", "", [], {}, 2])
def test_invalid_cookie_modes_are_rejected(tmp_path, value):
    config = _base(tmp_path)
    config["web"]["secure_cookie"] = value
    with pytest.raises(ConfigValidationError, match="secure_cookie"):
        validate_config(config)


@pytest.mark.parametrize(
    "secret",
    [
        "short-secret",
        "a" * 64,
        "abcd" * 16,
    ],
)
def test_explicit_weak_session_secrets_are_rejected(tmp_path: Path, secret: str):
    cfg = _base(tmp_path)
    cfg["web"]["secret_key"] = secret

    with pytest.raises(ConfigValidationError, match="web.secret_key"):
        validate_config(cfg)


def test_strong_session_secret_and_migration_values_are_accepted(tmp_path: Path):
    strong = "Qm8!tZ4#pL2@vN7$xR5&cD9*kF3-wY6+uH1=sJ0_eG"
    assert session_secret_strength_error(strong) is None
    assert session_secret_strength_error("") is None
    assert session_secret_strength_error(SESSION_SECRET_PLACEHOLDER) is None

    cfg = _base(tmp_path)
    cfg["web"]["secret_key"] = strong
    normalized, _warnings = validate_config(cfg)
    assert normalized["web"]["secret_key"] == strong


def test_option_like_remote_is_rejected(tmp_path: Path):
    cfg = _base(tmp_path)
    cfg["backup"]["pairs"] = [
        {
            "name": "Bad",
            "remote": "--config:/evil",
            "local": str(tmp_path),
            "direction": "pull",
            "mode": "copy",
        }
    ]
    with pytest.raises(ConfigValidationError):
        validate_config(cfg)


def test_mountpoint_must_be_parent_of_local_path(tmp_path: Path):
    local = tmp_path / "data"
    sibling = tmp_path / "other"
    cfg = _base(tmp_path)
    cfg["backup"]["pairs"] = [
        {
            "name": "Mount",
            "remote": "cloud:/data",
            "local": str(local),
            "direction": "pull",
            "mode": "copy",
            "mountpoint": str(sibling),
        }
    ]
    with pytest.raises(ConfigValidationError, match="mountpoint"):
        validate_config(cfg)


def test_legacy_webhooks_are_removed_with_migration_warning(tmp_path: Path):
    cfg = _base(tmp_path)
    hook = {
        "id": "duplicate123",
        "type": "generic",
        "url": "https://example.com/hook",
        "events": ["sync_ok"],
    }
    cfg["notifications"]["webhooks"] = [dict(hook), dict(hook)]
    normalized, warnings = validate_config(cfg)

    assert "webhooks" not in normalized["notifications"]
    assert any("Legacy-Webhooks wurden entfernt" in item for item in warnings)


def test_example_config_is_valid_and_examples_are_disabled():
    import yaml

    example = Path("config/config.example.yaml")
    config = yaml.safe_load(example.read_text(encoding="utf-8"))
    normalized, warnings = validate_config(config)
    assert warnings == []
    assert normalized["backup"]["pairs"]
    assert all(pair["enabled"] is False for pair in normalized["backup"]["pairs"])


def test_backup_watchdogs_have_separate_bounded_limits(tmp_path: Path):
    cfg = _base(tmp_path)
    cfg["backup"]["timeout_hours"] = 2.5
    cfg["backup"]["max_runtime_hours"] = 10_000

    normalized, _ = validate_config(cfg)

    assert normalized["backup"]["timeout_hours"] == 2.5
    assert normalized["backup"]["max_runtime_hours"] == 720


def test_pair_success_age_is_bounded(tmp_path: Path):
    cfg = _base(tmp_path)
    cfg["backup"]["pairs"] = [
        {
            "name": "Freshness",
            "remote": "cloud:/data",
            "local": str(tmp_path / "data"),
            "direction": "pull",
            "mode": "copy",
            "max_success_age_hours": 99999,
        }
    ]
    normalized, _ = validate_config(cfg)
    assert normalized["backup"]["pairs"][0]["max_success_age_hours"] == 8760


def test_pair_failure_domain_and_location_alias_are_preserved(tmp_path: Path):
    cfg = _base(tmp_path)
    cfg["backup"]["pairs"] = [
        {
            "name": "Domain",
            "remote": "cloud:/data",
            "local": str(tmp_path / "data"),
            "direction": "push",
            "mode": "copy",
            "failure_domain": "  Anbieter EU-1  ",
            "location_id": "anbieter eu-1",
        }
    ]

    normalized, _ = validate_config(cfg)
    pair = normalized["backup"]["pairs"][0]
    assert pair["failure_domain"] == "Anbieter EU-1"
    assert pair["location_id"] == "anbieter eu-1"


def test_conflicting_pair_failure_domain_aliases_are_rejected(tmp_path: Path):
    cfg = _base(tmp_path)
    cfg["backup"]["pairs"] = [
        {
            "name": "Domain",
            "remote": "cloud:/data",
            "local": str(tmp_path / "data"),
            "direction": "push",
            "mode": "copy",
            "failure_domain": "Anbieter A",
            "location_id": "Anbieter B",
        }
    ]

    with pytest.raises(ConfigValidationError, match="widersprechen"):
        validate_config(cfg)


def test_pair_failure_domain_rejects_control_characters(tmp_path: Path):
    cfg = _base(tmp_path)
    cfg["backup"]["pairs"] = [
        {
            "name": "Domain",
            "remote": "cloud:/data",
            "local": str(tmp_path / "data"),
            "direction": "push",
            "mode": "copy",
            "failure_domain": "Anbieter\ngefälscht",
        }
    ]

    with pytest.raises(ConfigValidationError, match="failure_domain"):
        validate_config(cfg)


def test_local_target_gets_mount_guard_by_default(tmp_path: Path):
    cfg = _base(tmp_path)
    source = tmp_path / "src"
    source.mkdir()
    target = tmp_path / "usb"
    target.mkdir()
    cfg["backup"]["pairs"] = [
        {
            "name": "LokalZuLokal",
            "remote": str(target),
            "local": str(source),
            "direction": "push",
            "mode": "sync",
            "min_remote_files": 0,
        }
    ]
    data, warnings = validate_config(cfg)
    pair = data["backup"]["pairs"][0]
    assert pair["min_remote_files"] == 1
    assert any("min_remote_files auf 1" in warning for warning in warnings)


def test_local_target_opt_out_keeps_zero(tmp_path: Path):
    cfg = _base(tmp_path)
    source = tmp_path / "src"
    source.mkdir()
    target = tmp_path / "usb"
    target.mkdir()
    cfg["backup"]["pairs"] = [
        {
            "name": "LeeresZiel",
            "remote": str(target),
            "local": str(source),
            "direction": "push",
            "mode": "sync",
            "min_remote_files": 0,
            "allow_empty_remote_target": True,
        }
    ]
    data, warnings = validate_config(cfg)
    assert data["backup"]["pairs"][0]["min_remote_files"] == 0
    assert any("allow_empty_remote_target" in warning for warning in warnings)


def test_unknown_keys_warn_without_breaking_load(tmp_path: Path):
    cfg = _base(tmp_path)
    cfg["web"]["hidden_remote_path"] = ["pcloud:/Crypto Folder"]
    cfg["bakcup"] = {}
    data, warnings = validate_config(cfg)
    assert data["backup"]["pairs"] == []
    assert any("web.hidden_remote_path" in warning for warning in warnings)
    assert any("'bakcup'" in warning for warning in warnings)


def test_known_example_config_has_no_unknown_key_warnings(tmp_path: Path):
    cfg = _base(tmp_path)
    cfg["web"].update({"secure_cookie": "auto", "hsts_seconds": 31536000})
    _data, warnings = validate_config(cfg)
    assert not [w for w in warnings if "Unbekannt" in w]


def test_webauthn_requires_matching_https_origin_and_secure_cookie(tmp_path: Path):
    cfg = _base(tmp_path)
    cfg["web"].update(
        {
            "secure_cookie": True,
            "webauthn_rp_id": "backup.example.de",
            "webauthn_origin": "https://backup.example.de",
        }
    )
    validated, _warnings = validate_config(cfg)
    assert validated["web"]["webauthn_rp_id"] == "backup.example.de"
    assert validated["web"]["webauthn_origin"] == "https://backup.example.de"

    cfg["web"]["secure_cookie"] = False
    with pytest.raises(ConfigValidationError, match="secure_cookie"):
        validate_config(cfg)


def test_webauthn_rejects_cross_domain_or_partial_configuration(tmp_path: Path):
    cfg = _base(tmp_path)
    cfg["web"].update(
        {
            "secure_cookie": True,
            "webauthn_rp_id": "example.de",
            "webauthn_origin": "https://attacker.invalid",
        }
    )
    with pytest.raises(ConfigValidationError, match="Domain der WebAuthn-Origin"):
        validate_config(cfg)

    cfg["web"]["webauthn_origin"] = ""
    with pytest.raises(ConfigValidationError, match="gemeinsam gesetzt"):
        validate_config(cfg)


def _pair(tmp_path: Path, **overrides) -> dict:
    pair = {
        "name": "Spiegel",
        "remote": "wasabi:media-bk",
        "local": str(tmp_path / "media"),
        "direction": "push",
        "mode": "sync",
        "enabled": True,
        "schedule": "0 2 * * *",
    }
    pair.update(overrides)
    return pair


def test_destructive_pair_without_backup_dir_warns(tmp_path: Path):
    cfg = _base(tmp_path)
    cfg["backup"]["pairs"] = [_pair(tmp_path)]
    _clean, warnings = validate_config(cfg)
    assert any("ohne backup_dir" in warning for warning in warnings)


def test_copy_pair_without_backup_dir_is_silent(tmp_path: Path):
    cfg = _base(tmp_path)
    cfg["backup"]["pairs"] = [_pair(tmp_path, mode="copy")]
    _clean, warnings = validate_config(cfg)
    assert not any("backup_dir" in warning for warning in warnings)


def test_backup_dir_inside_target_is_rejected(tmp_path: Path):
    cfg = _base(tmp_path)
    cfg["backup"]["pairs"] = [
        _pair(tmp_path, backup_dir="wasabi:media-bk/.versions/{date}")
    ]
    with pytest.raises(ConfigValidationError) as excinfo:
        validate_config(cfg)
    assert any("überlappen" in error for error in excinfo.value.errors)


def test_backup_dir_beside_target_is_accepted(tmp_path: Path):
    cfg = _base(tmp_path)
    cfg["backup"]["pairs"] = [
        _pair(tmp_path, backup_dir="wasabi:media-bk-versions/{date}")
    ]
    clean, warnings = validate_config(cfg)
    assert (
        clean["backup"]["pairs"][0]["backup_dir"] == "wasabi:media-bk-versions/{date}"
    )
    assert not any("ohne backup_dir" in warning for warning in warnings)


def test_backup_dir_rejects_traversal(tmp_path: Path):
    cfg = _base(tmp_path)
    cfg["backup"]["pairs"] = [_pair(tmp_path, backup_dir="../../etc/{date}")]
    with pytest.raises(ConfigValidationError) as excinfo:
        validate_config(cfg)
    assert any("'..'" in error for error in excinfo.value.errors)


def test_relative_backup_dir_on_destructive_pair_warns(tmp_path: Path):
    cfg = _base(tmp_path)
    cfg["backup"]["pairs"] = [_pair(tmp_path, backup_dir=".versions/{date}")]
    _clean, warnings = validate_config(cfg)
    assert any("relativ" in warning for warning in warnings)


def test_bisync_backup_dirs_are_checked_per_side(tmp_path: Path):
    local = tmp_path / "projekte"
    cfg = _base(tmp_path)
    cfg["backup"]["pairs"] = [
        _pair(
            tmp_path,
            direction="bisync",
            mode="bisync",
            local=str(local),
            backup_dir1=str(local / "versions"),
            backup_dir2="gdrive:projekte-versions/{date}",
        )
    ]
    with pytest.raises(ConfigValidationError) as excinfo:
        validate_config(cfg)
    assert any("backup_dir1" in error for error in excinfo.value.errors)


def test_anomaly_guard_is_bounded_globally_and_per_pair(tmp_path: Path):
    cfg = _base(tmp_path)
    cfg["backup"]["anomaly_guard"] = {
        "file_drop_percent": 1,
        "size_drop_percent": 500,
        "min_baseline_files": 0,
        "measurement_timeout_seconds": 5000,
    }
    cfg["backup"]["pairs"] = [
        _pair(
            tmp_path,
            anomaly_guard={
                "enabled": False,
                "file_drop_percent": 40,
                "measurement_timeout_seconds": 45,
            },
        )
    ]
    clean, _warnings = validate_config(cfg)
    assert clean["backup"]["anomaly_guard"] == {
        "enabled": True,
        "file_drop_percent": 5.0,
        "size_drop_percent": 100.0,
        "min_baseline_files": 1,
        "measurement_timeout_seconds": 900,
    }
    assert clean["backup"]["pairs"][0]["anomaly_guard"] == {
        "enabled": False,
        "file_drop_percent": 40.0,
        "size_drop_percent": 100.0,
        "min_baseline_files": 1,
        "measurement_timeout_seconds": 45,
    }


def test_pair_anomaly_guard_must_be_mapping(tmp_path: Path):
    cfg = _base(tmp_path)
    cfg["backup"]["pairs"] = [_pair(tmp_path, anomaly_guard="unsafe")]
    with pytest.raises(ConfigValidationError, match="anomaly_guard"):
        validate_config(cfg)


def _errors(cfg: dict) -> list[str]:
    with pytest.raises(ConfigValidationError) as excinfo:
        validate_config(cfg)
    return excinfo.value.errors


def test_new_backup_blocks_have_defaults_and_are_clamped(tmp_path: Path):
    clean, _ = validate_config(_base(tmp_path))
    assert clean["backup"]["versions_lifecycle"] == {
        "enabled": False,
        "keep_daily": 7,
        "keep_weekly": 4,
        "keep_monthly": 12,
    }
    assert clean["backup"]["bitrot_scrub"] == {
        "enabled": False,
        "schedule": "0 4 * * *",
        "sample_percent": 2,
        "max_files": 200,
        "max_total_mb": 256,
        "max_scan_files": 20_000,
        "rotation_days": 30,
    }
    assert clean["backup"]["vpn_probes"] == []

    cfg = _base(tmp_path)
    cfg["backup"]["versions_lifecycle"] = {
        "enabled": True,
        "keep_daily": 9999,
        "keep_weekly": -1,
        "keep_monthly": "x",
    }
    cfg["backup"]["bitrot_scrub"] = {
        "schedule": "manual",
        "sample_percent": 0,
        "max_files": 99_999,
        "max_total_mb": 0,
        "max_scan_files": 1,
        "rotation_days": 999,
    }
    clean, _ = validate_config(cfg)
    assert clean["backup"]["versions_lifecycle"] == {
        "enabled": True,
        "keep_daily": 365,
        "keep_weekly": 0,
        "keep_monthly": 12,
    }
    assert clean["backup"]["bitrot_scrub"] == {
        "enabled": False,
        "schedule": "manual",
        "sample_percent": 1,
        "max_files": 5000,
        "max_total_mb": 1,
        "max_scan_files": 100,
        "rotation_days": 365,
    }


def test_bitrot_scrub_schedule_must_be_cron_or_manual(tmp_path: Path):
    cfg = _base(tmp_path)
    cfg["backup"]["bitrot_scrub"] = {"schedule": "täglich"}
    assert any("bitrot_scrub.schedule" in e for e in _errors(cfg))


def test_vpn_probes_format_and_limit(tmp_path: Path):
    cfg = _base(tmp_path)
    cfg["backup"]["vpn_probes"] = [
        "10.8.0.1:443",
        "gw.example.de:51820",
        "[fd00::1]:443",
    ]
    clean, _ = validate_config(cfg)
    assert clean["backup"]["vpn_probes"] == cfg["backup"]["vpn_probes"]

    for bad in (
        "kein-port",
        "host:0",
        "host:65536",
        "fd00::1:443",
        "[nope]:1",
        "a b:1",
    ):
        cfg["backup"]["vpn_probes"] = ["10.8.0.1:443", bad]
        assert "backup.vpn_probes[1] ist ungültig (host:port erwartet)" in _errors(
            cfg
        ), bad

    cfg["backup"]["vpn_probes"] = [f"h{i}:1" for i in range(11)]
    assert any("höchstens 10" in e for e in _errors(cfg))


def test_pair_new_fields_have_defaults_and_normalize(tmp_path: Path):
    cfg = _base(tmp_path)
    cfg["backup"]["pairs"] = [_pair(tmp_path)]
    pair = validate_config(cfg)[0]["backup"]["pairs"][0]
    assert pair["airgap_device"] == ""
    assert pair["zfs_snapshot"] is False
    assert pair["tags"] == []
    assert "versions_lifecycle" not in pair

    cfg["backup"]["versions_lifecycle"] = {"keep_weekly": 8}
    cfg["backup"]["pairs"] = [
        _pair(
            tmp_path,
            airgap_device="/dev/disk/by-uuid/abc-123",
            zfs_snapshot=True,
            tags=["NAS", "nas", "privat_1"],
            versions_lifecycle={"enabled": True, "keep_daily": 5000},
        )
    ]
    pair = validate_config(cfg)[0]["backup"]["pairs"][0]
    assert pair["airgap_device"] == "/dev/disk/by-uuid/abc-123"
    assert pair["zfs_snapshot"] is True
    assert pair["tags"] == ["nas", "privat_1"]
    assert pair["versions_lifecycle"] == {
        "enabled": True,
        "keep_daily": 365,
        "keep_weekly": 8,
        "keep_monthly": 12,
    }


def test_pair_zfs_snapshot_requires_push(tmp_path: Path):
    cfg = _base(tmp_path)
    cfg["backup"]["pairs"] = [
        _pair(tmp_path, direction="bisync", mode="bisync", zfs_snapshot=True)
    ]
    assert (
        "backup.pairs[0].zfs_snapshot ist nur bei direction: push möglich"
        in _errors(cfg)
    )


@pytest.mark.parametrize(
    "device",
    [
        "/tmp/x",
        "dev/sda",
        "/dev/../etc/passwd",
        "/dev/sd a",
        "/dev/sda\nrm",
        "/dev/" + "a" * 252,
    ],
)
def test_pair_airgap_device_must_be_safe_dev_path(tmp_path: Path, device):
    cfg = _base(tmp_path)
    cfg["backup"]["pairs"] = [_pair(tmp_path, airgap_device=device)]
    assert "backup.pairs[0].airgap_device ist ungültig" in _errors(cfg)


@pytest.mark.parametrize(
    "tags",
    [["Bad Tag"], ["-x"], ["a" * 25], [f"t{i}" for i in range(11)], {"a": 1}],
)
def test_pair_tags_regex_and_limit(tmp_path: Path, tags):
    cfg = _base(tmp_path)
    cfg["backup"]["pairs"] = [_pair(tmp_path, tags=tags)]
    assert any(e.startswith("backup.pairs[0].tags:") for e in _errors(cfg))


def test_pair_versions_lifecycle_must_be_mapping(tmp_path: Path):
    cfg = _base(tmp_path)
    cfg["backup"]["pairs"] = [_pair(tmp_path, versions_lifecycle="an")]
    assert any("versions_lifecycle muss ein Mapping sein" in e for e in _errors(cfg))


def test_pbs_new_blocks_have_defaults_and_are_clamped(tmp_path: Path):
    pbs = validate_config(_base(tmp_path))[0]["pbs"]
    assert pbs["gc_throttle"] == {"enabled": True, "poll_seconds": 60}
    assert pbs["boot_drill"] == {
        "enabled": False,
        "schedule": "manual",
        "group": "",
        "archive": "",
        "memory_mb": 1024,
        "timeout_seconds": 60,
        "scratch_dir": "",
    }

    cfg = _base(tmp_path)
    cfg["pbs"] = {
        "gc_throttle": {"enabled": False, "poll_seconds": 1},
        "boot_drill": {
            "enabled": True,
            "schedule": "0 5 * * 0",
            "group": "vm/100",
            "archive": "drive-scsi0.img.fidx",
            "memory_mb": 10,
            "timeout_seconds": 99_999,
            "scratch_dir": "/var/tmp/drill",
        },
    }
    pbs = validate_config(cfg)[0]["pbs"]
    assert pbs["gc_throttle"] == {"enabled": False, "poll_seconds": 15}
    assert pbs["boot_drill"]["memory_mb"] == 256
    assert pbs["boot_drill"]["timeout_seconds"] == 900
    assert pbs["boot_drill"]["group"] == "vm/100"
    cfg["pbs"]["gc_throttle"]["poll_seconds"] = 5000
    cfg["pbs"]["boot_drill"]["memory_mb"] = 10**9
    pbs = validate_config(cfg)[0]["pbs"]
    assert pbs["gc_throttle"]["poll_seconds"] == 900
    assert pbs["boot_drill"]["memory_mb"] == 65_536


def test_pbs_boot_drill_validation_errors(tmp_path: Path):
    cfg = _base(tmp_path)
    cfg["pbs"] = {"boot_drill": {"enabled": True}}
    assert any("boot_drill.group ist erforderlich" in e for e in _errors(cfg))
    for key, value in (
        ("group", "vmx/100"),
        ("archive", "a/b"),
        ("scratch_dir", "relativ/pfad"),
        ("schedule", "irgendwann"),
    ):
        cfg["pbs"] = {"boot_drill": {"group": "ct/101", key: value}}
        assert any(f"boot_drill.{key}" in e for e in _errors(cfg)), key


def test_new_notification_events_are_known_everywhere(tmp_path: Path):
    from app.notifications import EVENTS

    new = ["bitrot_detected", "boot_drill_ok", "boot_drill_error", "lockdown"]
    assert set(new) <= set(EVENTS)
    cfg = _base(tmp_path)
    cfg["notifications"] = {"apns": {"events": new}}
    assert validate_config(cfg)[0]["notifications"]["apns"]["events"] == new


def test_example_config_documents_new_keys():
    example = Path(__file__).resolve().parents[1] / "config" / "config.example.yaml"
    clean, _ = validate_config(yaml.safe_load(example.read_text(encoding="utf-8")))
    assert clean["pbs"]["boot_drill"]["enabled"] is False
    assert {"bitrot_detected", "boot_drill_error", "lockdown"} <= set(
        clean["notifications"]["apns"]["events"]
    )
