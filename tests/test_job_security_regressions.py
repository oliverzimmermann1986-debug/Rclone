"""Quarantine, filter and PBS identity invariants across alternate execution paths."""

from pathlib import Path

import pytest
import yaml
from fastapi import HTTPException, Request

from app import protection
from app.config_store import Config
from app.config_validation import ConfigValidationError, validate_config
from app.db import Database
from app.jobs import pbs_backup, rclone_sync
from app.rclone_args import UnsafeRcloneArgument, validate_parsed_rclone_args
from app.routes import api_config


def _pair(tmp_path: Path) -> dict:
    return {
        "id": "a" * 32,
        "name": "Protected",
        "local": str(tmp_path / "source"),
        "remote": "cloud:/Photos",
        "direction": "push",
        "mode": "sync",
        "allow_delete": True,
        "max_delete": 10,
    }


def _quarantine(database: Database, pair: dict) -> None:
    database.runtime_set(
        protection._ANOMALY_STATE_KEY,
        {
            "baselines": {},
            "quarantines": {
                protection._state_key(pair): {
                    "pair": pair["name"],
                    "detected_at": 1,
                    "acknowledged": False,
                }
            },
        },
    )


@pytest.fixture
def guard_config(tmp_path, monkeypatch):
    config, _warnings = validate_config(
        {
            "web": {
                "username": "admin",
                "secret_key": "guard-test-secret-with-varied-characters-7be804a9",
                "local_browse_roots": [str(tmp_path)],
            },
            "paths": {
                "data_dir": str(tmp_path / "data"),
                "logs_dir": str(tmp_path / "logs"),
                "temp_dir": str(tmp_path / "tmp"),
            },
            "backup": {"pairs": [_pair(tmp_path)], "filter_file": ""},
            "pbs": {"enabled": False, "targets": []},
        }
    )
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    store = Config(path)
    database = Database(tmp_path / "guard-config.db")
    monkeypatch.setattr(api_config, "get_config", lambda: store)
    monkeypatch.setattr(api_config, "get_db", lambda: database)
    return store


def _guard_update(store: Config):
    candidate, revision = store.snapshot_with_revision()
    candidate["_revision"] = revision
    request = Request(
        {
            "type": "http",
            "method": "PUT",
            "path": "/api/config",
            "headers": [],
            "client": ("192.0.2.10", 12345),
        }
    )
    return candidate, revision, request


@pytest.mark.parametrize("scope", ("global", "pair"))
@pytest.mark.parametrize(
    "key,value",
    (
        ("enabled", False),
        ("file_drop_percent", 100),
        ("size_drop_percent", 100),
        ("min_baseline_files", 10_000_000),
        ("measurement_timeout_seconds", 10),
    ),
)
def test_all_effective_guard_parameters_require_password(
    guard_config, scope, key, value
):
    candidate, revision, request = _guard_update(guard_config)
    section = candidate["backup"]
    if scope == "pair":
        section = section["pairs"][0]
    section.setdefault("anomaly_guard", {})[key] = value
    with pytest.raises(HTTPException) as denied:
        api_config.update_config(
            request, api_config.ConfigUpdate(config=candidate), user="admin"
        )
    assert denied.value.status_code == 403
    assert guard_config.revision == revision


def test_guard_parameter_change_with_password_proof_is_saved(guard_config, monkeypatch):
    candidate, revision, request = _guard_update(guard_config)
    candidate["backup"]["anomaly_guard"]["min_baseline_files"] = 10_000_000
    proofs = []
    monkeypatch.setattr(
        api_config,
        "require_reauthentication",
        lambda _request, user, password: proofs.append((user, password)),
    )
    api_config.update_config(
        request,
        api_config.ConfigUpdate(config=candidate, current_password="fixture-proof"),
        user="admin",
    )
    assert proofs == [("admin", "fixture-proof")]
    assert guard_config.revision != revision
    assert (
        guard_config.get("backup", "anomaly_guard", "min_baseline_files") == 10_000_000
    )


@pytest.mark.parametrize("invalid_guard", (True, 7, ["unexpected"]))
def test_invalid_pair_guard_preserves_validation_response(guard_config, invalid_guard):
    candidate, revision, request = _guard_update(guard_config)
    candidate["backup"]["pairs"][0]["anomaly_guard"] = invalid_guard
    with pytest.raises(HTTPException) as denied:
        api_config.update_config(
            request, api_config.ConfigUpdate(config=candidate), user="admin"
        )
    assert denied.value.status_code == 422
    assert guard_config.revision == revision


@pytest.mark.parametrize("scope", ("global", "pair"))
def test_invalid_numeric_guard_cannot_reset_stricter_setting_without_password(
    guard_config, scope
):
    old = guard_config.snapshot()
    section = old["backup"]
    if scope == "pair":
        section = section["pairs"][0]
    section.setdefault("anomaly_guard", {})["min_baseline_files"] = 10
    candidate, _warnings = validate_config(old)
    section = candidate["backup"]
    if scope == "pair":
        section = section["pairs"][0]
    section["anomaly_guard"]["min_baseline_files"] = {"invalid": "number"}
    assert api_config._sensitive_config_changed(old, candidate) is True


def test_equivalent_guard_defaults_and_numeric_strings_do_not_require_password(
    guard_config,
):
    old = guard_config.snapshot()
    candidate = guard_config.snapshot()
    for key, value in candidate["backup"]["anomaly_guard"].items():
        candidate["backup"]["anomaly_guard"][key] = str(value)
    candidate["backup"]["pairs"][0]["anomaly_guard"] = {"enabled": "true"}
    assert api_config._sensitive_config_changed(old, candidate) is False


def test_repairing_invalid_config_cannot_hide_guard_weakening(guard_config):
    old = guard_config.snapshot()
    old["backup"]["pairs"] = "invalid old structure"
    candidate = guard_config.snapshot()
    candidate["backup"]["anomaly_guard"]["min_baseline_files"] = 10_000_000
    assert api_config._sensitive_config_changed(old, candidate) is True


def test_new_pair_cannot_add_unprotected_guard_override_without_password(guard_config):
    candidate, revision, request = _guard_update(guard_config)
    candidate["backup"]["pairs"].append(
        {
            **candidate["backup"]["pairs"][0],
            "id": "b" * 32,
            "name": "Unprotected alternate",
            "anomaly_guard": {"enabled": False},
        }
    )
    with pytest.raises(HTTPException) as denied:
        api_config.update_config(
            request, api_config.ConfigUpdate(config=candidate), user="admin"
        )
    assert denied.value.status_code == 403
    assert guard_config.revision == revision


@pytest.mark.parametrize("disable_at", ("global", "pair"))
def test_disabling_guard_does_not_clear_existing_quarantine(
    tmp_path, monkeypatch, disable_at
):
    database = Database(tmp_path / "guard.db")
    pair = _pair(tmp_path)
    _quarantine(database, pair)
    backup = {"pairs": [pair]}
    if disable_at == "global":
        backup["anomaly_guard"] = {"enabled": False}
    else:
        pair["anomaly_guard"] = {"enabled": False}

    def unexpected_measurement(*_args, **_kwargs):
        pytest.fail("An existing quarantine must be enforced without measurement")

    monkeypatch.setattr(protection, "measure_path", unexpected_measurement)
    finding = protection.preflight_anomaly_guard(
        database, pair=pair, backup=backup, source=pair["local"]
    )
    assert finding["blocked"] is True
    assert finding["quarantine"]["acknowledged"] is False
    assert protection.acknowledge_quarantine(database, pair)
    assert (
        protection.preflight_anomaly_guard(
            database, pair=pair, backup=backup, source=pair["local"]
        )["blocked"]
        is False
    )


@pytest.mark.parametrize("overlap", ("local_alias", "local_child", "remote_child"))
def test_quarantine_follows_endpoint_aliases_and_overlaps(tmp_path, overlap):
    database = Database(tmp_path / "overlap.db")
    configured = _pair(tmp_path)
    _quarantine(database, configured)
    quick = {
        **configured,
        "id": "b" * 32,
        "name": "quick",
        "local": str(tmp_path / "other"),
        "remote": "other:/Unrelated",
    }
    if overlap == "local_alias":
        quick["local"] = str(tmp_path / "source" / "nested" / "..")
    elif overlap == "local_child":
        quick["local"] = str(tmp_path / "source" / "child")
    else:
        quick["remote"] = "CLOUD:./Photos/old/../child/"
    finding = protection.active_quarantine(
        database, pair=quick, configured_pairs=[configured]
    )
    assert finding is not None
    assert finding["blocked"] is True


def test_quarantine_does_not_block_unrelated_or_non_destructive_copy(tmp_path):
    database = Database(tmp_path / "unrelated.db")
    configured = _pair(tmp_path)
    _quarantine(database, configured)
    unrelated = {
        **configured,
        "id": "b" * 32,
        "local": str(tmp_path / "different"),
        "remote": "other:/Photos",
    }
    assert (
        protection.active_quarantine(
            database, pair=unrelated, configured_pairs=[configured]
        )
        is None
    )
    assert (
        protection.active_quarantine(
            database,
            pair={**configured, "mode": "copy", "allow_delete": False},
            configured_pairs=[configured],
        )
        is None
    )


def _quick_config(tmp_path: Path, pair: dict) -> dict:
    return {
        "web": {"local_browse_roots": [str(tmp_path)]},
        "paths": {"logs_dir": str(tmp_path / "logs")},
        "backup": {"pairs": [pair]},
    }


def test_quick_quarantine_stops_before_precheck_or_destination_creation(
    tmp_path, monkeypatch
):
    database = Database(tmp_path / "quick.db")
    configured = _pair(tmp_path)
    _quarantine(database, configured)
    monkeypatch.setattr(rclone_sync, "get_db", lambda: database)

    def unexpected_precheck(*_args, **_kwargs):
        pytest.fail("Quarantined Quick Sync must not probe or create destinations")

    monkeypatch.setattr(rclone_sync, "_precheck_pair", unexpected_precheck)
    monkeypatch.setattr(rclone_sync, "_prepare_local_destinations", unexpected_precheck)
    result = rclone_sync.run_quick(
        remote_path="cloud:./Photos/",
        local_path=configured["local"],
        direction="push",
        mode="sync",
        allow_delete=True,
        max_delete=10,
        config_snapshot=_quick_config(tmp_path, configured),
    )
    assert result["ok"] is False
    assert result["quarantined"] is True
    assert result["skipped"] is True
    assert not Path(configured["local"]).exists()


@pytest.mark.parametrize("dry_run,mode", ((True, "sync"), (False, "copy")))
def test_quick_preview_and_copy_do_not_query_destructive_quarantine(
    tmp_path, monkeypatch, dry_run, mode
):
    configured = _pair(tmp_path)

    def unexpected_database():
        pytest.fail("Non-destructive Quick Sync must remain available")

    monkeypatch.setattr(rclone_sync, "get_db", unexpected_database)
    monkeypatch.setattr(
        rclone_sync, "_precheck_pair", lambda _pair: (False, "precheck reached")
    )
    result = rclone_sync.run_quick(
        remote_path=configured["remote"],
        local_path=configured["local"],
        direction="push",
        mode=mode,
        dry_run=dry_run,
        config_snapshot=_quick_config(tmp_path, configured),
    )
    assert result["error"] == "precheck reached"
    assert "quarantined" not in result


def test_quick_rechecks_quarantine_immediately_before_spawn(tmp_path, monkeypatch):
    database = Database(tmp_path / "late.db")
    configured = _pair(tmp_path)
    monkeypatch.setattr(rclone_sync, "get_db", lambda: database)
    monkeypatch.setattr(rclone_sync, "_precheck_pair", lambda _pair: (True, "ok"))
    monkeypatch.setattr(rclone_sync, "_prepare_local_destinations", lambda _pair: None)
    monkeypatch.setattr(rclone_sync, "_capture_local_endpoint_guards", lambda _pair: {})
    monkeypatch.setattr(
        rclone_sync,
        "_build_pair_command",
        lambda *_args, **_kwargs: (["rclone", "sync"], "sync", "push", "sync"),
    )

    def attempt_spawn(_command, _log_file, *, pre_spawn_check, **_kwargs):
        _quarantine(database, configured)
        allowed, message = pre_spawn_check()
        assert allowed is False
        raise RuntimeError(message)

    monkeypatch.setattr(rclone_sync, "_run_rclone_command", attempt_spawn)
    result = rclone_sync.run_quick(
        remote_path=configured["remote"],
        local_path=configured["local"],
        direction="push",
        mode="sync",
        allow_delete=True,
        max_delete=10,
        reset_cancel_state=False,
        config_snapshot=_quick_config(tmp_path, configured),
    )
    assert result["ok"] is False
    assert result["quarantined"] is True
    assert "Prozessstart quarantänisiert" in result["error"]


@pytest.mark.parametrize("args", (["-f", "+ **"], ["-f=+ **"], ["-f+ **"]))
def test_short_filter_alias_uses_reserved_first_rule_stream(args):
    with pytest.raises(UnsafeRcloneArgument):
        validate_parsed_rclone_args(args)
    permitted = validate_parsed_rclone_args(args, allow_unsafe=True)
    compiled = rclone_sync._protected_filter_args(permitted)
    assert compiled[:2] == ["--filter", "- /Sicherpfad/**"]
    assert compiled.index("- /Sicherpfad/**") < compiled.index("+ **")
    assert all(not token.startswith("-f") for token in compiled)


@pytest.mark.parametrize("args", (["-f", "!"], ["-f=!"], ["-f!"]))
def test_short_filter_reset_is_rejected_even_in_expert_mode(args):
    with pytest.raises(ValueError, match="Reset"):
        rclone_sync._protected_filter_args(
            validate_parsed_rclone_args(args, allow_unsafe=True)
        )


def test_ambiguous_short_filter_cluster_is_rejected_in_expert_mode():
    with pytest.raises(UnsafeRcloneArgument, match="kombinierte|Kombinierte"):
        validate_parsed_rclone_args(["-vf", "+ **"], allow_unsafe=True)


def test_short_filter_normalization_does_not_reinterpret_filter_values():
    args = validate_parsed_rclone_args(["--exclude", "-foo/**"], allow_unsafe=True)
    compiled = rclone_sync._protected_filter_args(args)
    assert "- -foo/**" in compiled
    assert "--filter=oo/**" not in compiled


@pytest.mark.parametrize("second_id", ("docs.", "docs_", "DOCS__"))
def test_pbs_validation_compares_actual_snapshot_group_ids(tmp_path, second_id):
    config = {
        "paths": {"data_dir": str(tmp_path)},
        "pbs": {
            "enabled": False,
            "targets": [
                {
                    "name": "First",
                    "paths": [str(tmp_path / "first")],
                    "backup_id": "docs",
                },
                {
                    "name": "Second",
                    "paths": [str(tmp_path / "second")],
                    "backup_id": second_id,
                },
            ],
        },
    }
    with pytest.raises(ConfigValidationError, match="backup_id.*nicht eindeutig"):
        validate_config(config)


def test_pbs_canonical_identity_preserves_existing_group_for_backup_and_prune(
    monkeypatch,
):
    monkeypatch.setattr(pbs_backup, "client_path", lambda: "/fixture/pbc")
    settings = {"repository": "backup@pbs!token@host:store", "keep": {"keep_last": 3}}
    target = {"name": "Documents", "paths": ["/fixture/docs"], "backup_id": "docs.__"}
    backup = pbs_backup.build_backup_command(settings, target)
    prune = pbs_backup.build_prune_command(settings, target)
    assert backup[backup.index("--backup-id") + 1] == "docs"
    assert prune[1:3] == ["prune", "host/docs"]
