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
