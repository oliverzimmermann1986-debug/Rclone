from pathlib import Path

import pytest
import yaml

from app.config_store import Config, ConfigConflictError


def test_config_store_returns_copies_and_saves_atomically(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text("backup:\n  pairs: []\n", encoding="utf-8")
    store = Config(path)

    snapshot = store.snapshot()
    snapshot["backup"]["pairs"].append({"name": "mutated-only-locally"})
    assert store.get("backup", "pairs") == []

    store.update(lambda data: data["backup"].update({"enabled": True}))
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert loaded["backup"]["enabled"] is True
    assert path.stat().st_mode & 0o077 == 0


def test_config_store_reloads_external_changes(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text("value: 1\n", encoding="utf-8")
    store = Config(path)
    path.write_text("value: 2\n", encoding="utf-8")
    assert store.get("value") == 2


def test_config_revision_prevents_lost_updates(tmp_path: Path):
    from app.config_store import ConfigConflictError

    path = tmp_path / "config.yaml"
    path.write_text("value: 1\n", encoding="utf-8")
    first = Config(path)
    second = Config(path)
    _, revision = first.snapshot_with_revision()
    second.replace({"value": 2})

    try:
        first.replace({"value": 3}, expected_revision=revision)
    except ConfigConflictError:
        pass
    else:
        raise AssertionError("veraltete Revision wurde nicht abgewiesen")
    assert Config(path).get("value") == 2


def test_config_store_keeps_one_secure_previous_version(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text("value: 1\n", encoding="utf-8")
    store = Config(path)
    store.replace({"value": 2})

    backup = tmp_path / "config.yaml.bak"
    assert yaml.safe_load(backup.read_text(encoding="utf-8")) == {"value": 1}
    assert backup.stat().st_mode & 0o077 == 0


def test_set_save_detects_external_change(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text("value: 1\n", encoding="utf-8")
    first = Config(path)
    second = Config(path)

    first.set("value", 2)
    second.update(lambda data: data.update({"value": 3}))

    with pytest.raises(ConfigConflictError):
        first.save()
    assert Config(path).get("value") == 3


def test_set_save_succeeds_without_conflict(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text("nested:\n  value: 1\n", encoding="utf-8")
    store = Config(path)
    store.set("nested", "value", 2)
    store.save()
    assert Config(path).get("nested", "value") == 2
