"""Tests für den Bulk-Enable/Disable-Endpoint POST /api/config/pairs/bulk."""

import pytest
import yaml
from fastapi import HTTPException

from app.config_store import Config
from app.routes import api_config


def _store(tmp_path, monkeypatch):
    cfg = {
        "web": {"username": "admin"},
        "backup": {
            "pairs": [
                {"name": "Serien", "enabled": True},
                {"name": "Filme", "enabled": True},
                {"name": "Fotos", "enabled": False},
            ]
        },
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    store = Config(path)
    monkeypatch.setattr(api_config, "get_config", lambda: store)
    monkeypatch.setattr(api_config, "validate_config", lambda data: (data, []))
    monkeypatch.setattr(api_config, "_audit_best_effort", lambda *a, **k: None)
    return store, path


def _pairs_by_name(path):
    snap, _ = Config(path).snapshot_with_revision()
    return {p["name"]: p for p in snap["backup"]["pairs"]}


def test_bulk_disable_only_changes_matching_pairs(tmp_path, monkeypatch):
    store, path = _store(tmp_path, monkeypatch)
    _, revision = store.snapshot_with_revision()
    result = api_config.pairs_bulk(
        api_config.PairBulkAction(
            names=["Serien", "Filme"], action="disable", revision=revision
        ),
        user="admin",
    )
    assert result["ok"] is True
    assert result["matched"] == 2
    assert result["changed"] == 2
    pairs = _pairs_by_name(path)
    assert pairs["Serien"]["enabled"] is False
    assert pairs["Filme"]["enabled"] is False
    assert pairs["Fotos"]["enabled"] is False  # unverändert


def test_bulk_enable_counts_only_effective_changes(tmp_path, monkeypatch):
    store, path = _store(tmp_path, monkeypatch)
    _, revision = store.snapshot_with_revision()
    # Serien ist bereits enabled -> matched=2, changed=1 (nur Fotos wechselt)
    result = api_config.pairs_bulk(
        api_config.PairBulkAction(
            names=["Serien", "Fotos"], action="enable", revision=revision
        ),
        user="admin",
    )
    assert result["matched"] == 2
    assert result["changed"] == 1
    assert _pairs_by_name(path)["Fotos"]["enabled"] is True


def test_bulk_rejects_stale_revision(tmp_path, monkeypatch):
    store, _ = _store(tmp_path, monkeypatch)
    with pytest.raises(HTTPException) as exc:
        api_config.pairs_bulk(
            api_config.PairBulkAction(
                names=["Serien"], action="disable", revision="0" * 64
            ),
            user="admin",
        )
    assert exc.value.status_code == 409


def test_bulk_unknown_name_is_404(tmp_path, monkeypatch):
    store, _ = _store(tmp_path, monkeypatch)
    _, revision = store.snapshot_with_revision()
    with pytest.raises(HTTPException) as exc:
        api_config.pairs_bulk(
            api_config.PairBulkAction(
                names=["DoesNotExist"], action="disable", revision=revision
            ),
            user="admin",
        )
    assert exc.value.status_code == 404
