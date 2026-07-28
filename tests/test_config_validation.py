from pathlib import Path

import pytest

from app.config_validation import ConfigValidationError, validate_config


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


def test_duplicate_webhook_ids_are_replaced(tmp_path: Path):
    cfg = _base(tmp_path)
    hook = {
        "id": "duplicate123",
        "type": "generic",
        "url": "https://example.com/hook",
        "events": ["sync_ok"],
    }
    cfg["notifications"]["webhooks"] = [dict(hook), dict(hook)]
    normalized, _ = validate_config(cfg)
    ids = [item["id"] for item in normalized["notifications"]["webhooks"]]
    assert len(ids) == len(set(ids)) == 2


def test_example_config_is_valid_and_examples_are_disabled():
    import yaml

    example = Path("config/config.example.yaml")
    config = yaml.safe_load(example.read_text(encoding="utf-8"))
    normalized, warnings = validate_config(config)
    assert warnings == []
    assert normalized["backup"]["pairs"]
    assert all(pair["enabled"] is False for pair in normalized["backup"]["pairs"])


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
