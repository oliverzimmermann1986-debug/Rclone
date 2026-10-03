"""Behavioral checks for authenticated previews and narrow revision-bound saves."""

import copy
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import auth
from app.config_store import Config
from app.config_validation import validate_config
from app.db import Database
from app.restore_evidence import MAX_EVIDENCE_AGE_SEC, pair_binding, restore_history_key
from app.routes import api_restore_plan
from app.security import CSRF_COOKIE


URL = "/api/recovery/restore-plan"
SECRET = "Qm8!tZ4#pL2@vN7$xR5&cD9*kF3-wY6+uH1=sJ0_eG"


def stamp(month, day, hour=12, minute=0):
    return datetime(
        2026, month, day, hour, minute, tzinfo=ZoneInfo("Europe/Berlin")
    ).timestamp()


def settings(schedule="0 5 * * 0,3", *, enabled=True):
    return {
        "enabled": enabled,
        "schedule": schedule,
        "sample_files": 20,
        "max_total_mb": 256,
        "max_scan_files": 20000,
    }


@pytest.fixture
def environment(tmp_path, monkeypatch):
    config = {
        "web": {
            "username": "admin",
            "secret_key": SECRET,
            "local_browse_roots": [str(tmp_path)],
        },
        "paths": {
            "data_dir": str(tmp_path),
            "logs_dir": str(tmp_path / "logs"),
            "temp_dir": str(tmp_path / "temp"),
        },
        "backup": {
            "enabled": True,
            "timezone": "Europe/Berlin",
            "default_schedule": "manual",
            "restore_test": settings("0 5 1 * *"),
            "jobs": [],
            "pairs": [
                {
                    "id": "a" * 32,
                    "name": "Fotos",
                    "local": str(tmp_path / "Fotos"),
                    "remote": "cloud:Foto",
                    "direction": "push",
                    "mode": "copy",
                    "enabled": True,
                },
                {
                    "id": "b" * 32,
                    "name": "Rezepte",
                    "local": str(tmp_path / "Rezepte"),
                    "remote": "cloud:Rezepte",
                    "direction": "push",
                    "mode": "copy",
                    "enabled": True,
                },
            ],
        },
        "notifications": {"webhooks": []},
        "custom_operator_note": "Keep this unrelated setting",
    }
    normalized, _ = validate_config(config)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(normalized), encoding="utf-8")
    store = Config(path)
    database = Database(tmp_path / "restore-plan.db")
    clock = {"now": stamp(10, 3)}
    monkeypatch.setattr(api_restore_plan.time, "time", lambda: clock["now"])
    monkeypatch.setattr(api_restore_plan, "get_config", lambda: store)
    monkeypatch.setattr(api_restore_plan, "get_db", lambda: database)
    monkeypatch.setattr(auth, "get_config", lambda: store)
    monkeypatch.setattr("app.routes.api_config.get_db", lambda: database)
    app = FastAPI()
    app.include_router(api_restore_plan.router)
    with TestClient(app) as client:
        client.cookies.set(auth.SESSION_COOKIE, auth.create_session("admin"))
        client.cookies.set(CSRF_COOKIE, "restore-plan-csrf")
        client.headers["X-CSRF-Token"] = "restore-plan-csrf"
        yield SimpleNamespace(
            client=client, store=store, db=database, clock=clock, path=path
        )


def body(env, draft=None):
    return {"revision": env.store.revision, "settings": draft or settings()}


def proof(env, name, checked_at, *, ok=True):
    pair = next(
        item for item in env.store.get("backup", "pairs") if item["name"] == name
    )
    previous = env.clock["now"]
    env.clock["now"] = checked_at
    job_id = env.db.job_start("restoretest")
    result = {
        "name": name,
        "ok": ok,
        "history_key": restore_history_key(pair),
        "evidence_binding": pair_binding(pair),
        "evidence_checked_at": checked_at,
        "verified": 2 if ok else 1,
        "sample_size": 2,
        "sample_status": "complete",
    }
    if not ok:
        result["error"] = "checksum mismatch"
    env.db.job_finish(job_id, "ok" if ok else "error", {"pairs": [result]})
    env.clock["now"] = previous
    return job_id


def test_preview_has_no_config_or_evidence_mutations(environment):
    env = environment
    proof(env, "Fotos", env.clock["now"] - 86400)
    config_bytes = env.path.read_bytes()
    before = env.store.snapshot_with_revision()
    history = env.db.job_list()

    response = env.client.post(URL + "/preview", json=body(env))

    assert response.status_code == 200
    assert response.json()["settings"]["schedule"] == "0 5 * * 0,3"
    assert env.path.read_bytes() == config_bytes
    assert env.store.snapshot_with_revision() == before
    assert env.db.job_list() == history
    assert not env.path.with_suffix(".yaml.bak").exists()


@pytest.mark.parametrize("preview", [False, True])
def test_plan_read_does_not_delete_an_expired_scheduler_pause(environment, preview):
    env = environment
    raw_pause = {
        "paused": True,
        "until": env.clock["now"] - 60,
        "reason": "finished maintenance",
        "actor": "admin",
    }
    env.db.runtime_set("scheduler_pause", raw_pause)
    response = (
        env.client.post(URL + "/preview", json=body(env))
        if preview
        else env.client.get(URL)
    )
    assert response.status_code == 200
    assert response.json()["scheduler_paused"] is False
    assert env.db.runtime_get("scheduler_pause") == raw_pause


@pytest.mark.parametrize("schedule", ["manual", "off", "disabled", "none", " MANUAL "])
@pytest.mark.parametrize("method", ["post", "put"])
def test_enabled_automatic_plan_rejects_manual_schedules(environment, schedule, method):
    env = environment
    original = env.store.snapshot_with_revision()
    response = getattr(env.client, method)(
        URL + ("/preview" if method == "post" else ""),
        json=body(env, settings(schedule)),
    )
    assert response.status_code == 422
    assert env.store.snapshot_with_revision() == original


def test_disabled_manual_plan_can_be_previewed(environment):
    response = environment.client.post(
        URL + "/preview", json=body(environment, settings("manual", enabled=False))
    )
    assert response.status_code == 200
    result = response.json()
    assert result["next_runs"] == []
    assert result["due_now"] is False
    assert any("deaktiviert" in warning for warning in result["warnings"])


@pytest.mark.parametrize("schedule", ["invalid cron", "0 0 5 * * *", "99 5 * * *"])
def test_invalid_or_extended_cron_is_rejected_without_save(environment, schedule):
    before = environment.path.read_bytes()
    response = environment.client.put(URL, json=body(environment, settings(schedule)))
    assert response.status_code == 422
    assert environment.path.read_bytes() == before


@pytest.mark.parametrize("method,suffix", [("post", "/preview"), ("put", "")])
def test_impossible_calendar_cron_is_rejected_before_mutating_config(
    environment, method, suffix
):
    env = environment
    before = env.path.read_bytes()
    revision = env.store.revision
    response = getattr(env.client, method)(
        URL + suffix, json=body(env, settings("0 5 31 2 *"))
    )
    assert response.status_code == 422
    assert env.path.read_bytes() == before
    assert env.store.revision == revision


@pytest.mark.parametrize("schedule", ["0 5 31 2 *", "0 0 5 * * *", "not a cron"])
def test_legacy_invalid_schedule_stays_readable_and_editable_without_writes(
    environment, schedule
):
    env = environment
    env.store.update(
        lambda config: config["backup"].update({"restore_test": settings(schedule)})
    )
    proof(env, "Fotos", env.clock["now"] - 60)
    config_bytes = env.path.read_bytes()
    snapshot = env.store.snapshot_with_revision()
    jobs = env.db.job_list()

    response = env.client.get(URL)

    assert response.status_code == 200
    result = response.json()
    assert result["settings"]["schedule"] == schedule
    assert result["revision"] == env.store.revision
    assert result["next_runs"] == []
    assert result["max_interval_seconds"] is None
    assert result["due_now"] is False
    assert all(path["coverage_gap"] for path in result["data_paths"])
    assert any("gespeicherte Prüfzeitplan" in warning for warning in result["warnings"])
    assert env.path.read_bytes() == config_bytes
    assert env.store.snapshot_with_revision() == snapshot
    assert env.db.job_list() == jobs

    repair = env.client.put(URL, json=body(env))
    assert repair.status_code == 200
    assert repair.json()["settings"]["schedule"] == "0 5 * * 0,3"


@pytest.mark.parametrize(
    "field,value",
    [
        ("sample_files", 0),
        ("max_total_mb", 51201),
        ("max_scan_files", 99),
        ("enabled", "yes"),
    ],
)
def test_invalid_settings_are_rejected(environment, field, value):
    draft = settings()
    draft[field] = value
    response = environment.client.put(URL, json=body(environment, draft))
    assert response.status_code == 422


def test_monthly_plan_has_gap_but_twice_weekly_fits_current_proof(environment):
    env = environment
    for name in ("Fotos", "Rezepte"):
        proof(env, name, stamp(10, 1))
    monthly = env.client.get(URL).json()
    frequent = env.client.post(URL + "/preview", json=body(env)).json()

    assert monthly["max_interval_seconds"] > MAX_EVIDENCE_AGE_SEC
    assert all(path["evidence_state"] == "passed" for path in monthly["data_paths"])
    assert all(path["coverage_gap"] for path in monthly["data_paths"])
    assert 0 < frequent["max_interval_seconds"] < MAX_EVIDENCE_AGE_SEC
    assert not any(path["coverage_gap"] for path in frequent["data_paths"])
    assert monthly["warnings"]
    assert frequent["warnings"] == []


def test_weekly_plan_detects_autumn_dst_interval(environment):
    env = environment
    env.clock["now"] = stamp(10, 1)
    env.client.cookies.set(auth.SESSION_COOKIE, auth.create_session("admin"))
    response = env.client.post(URL + "/preview", json=body(env, settings("0 5 * * 0")))
    result = response.json()
    assert response.status_code == 200
    assert result["max_interval_seconds"] == MAX_EVIDENCE_AGE_SEC + 3600
    assert any("Zeitumstellung" in warning for warning in result["warnings"])


def test_frequent_monthly_burst_does_not_hide_the_long_gap_after_the_burst(environment):
    env = environment
    env.clock["now"] = stamp(10, 1, 0, 1)
    env.client.cookies.set(auth.SESSION_COOKIE, auth.create_session("admin"))
    for name in ("Fotos", "Rezepte"):
        proof(env, name, env.clock["now"] - 60)
    response = env.client.post(URL + "/preview", json=body(env, settings("* * 1 * *")))
    assert response.status_code == 200
    result = response.json()
    assert result["max_interval_seconds"] > MAX_EVIDENCE_AGE_SEC
    assert any("Abstand" in warning for warning in result["warnings"])


@pytest.mark.parametrize("state", ["expired", "failed", "never"])
def test_invalid_persisted_evidence_never_reports_continuous_coverage(
    environment, state
):
    env = environment
    now = env.clock["now"]
    if state == "expired":
        proof(env, "Fotos", now - MAX_EVIDENCE_AGE_SEC)
    elif state == "failed":
        proof(env, "Fotos", now - 2 * 86400)
        proof(env, "Fotos", now - 86400, ok=False)
    response = env.client.post(URL + "/preview", json=body(env))
    path = next(
        item for item in response.json()["data_paths"] if item["name"] == "Fotos"
    )
    assert (
        path["evidence_state"]
        == {"expired": "stale", "failed": "failed", "never": "never"}[state]
    )
    assert path["coverage_gap"] is True
    assert response.json()["warnings"]


def test_changed_data_path_binding_invalidates_an_existing_proof(environment):
    env = environment
    proof(env, "Fotos", env.clock["now"] - 86400)
    env.store.update(
        lambda config: config["backup"]["pairs"][0].update({"remote": "cloud:NewFoto"})
    )
    response = env.client.post(URL + "/preview", json=body(env))
    path = next(
        item for item in response.json()["data_paths"] if item["name"] == "Fotos"
    )
    assert path["evidence_state"] == "stale"
    assert path["coverage_gap"] is True


def test_paused_or_disabled_scheduler_never_claims_a_due_automatic_run(environment):
    env = environment
    env.clock["now"] = stamp(10, 4, 5, 1)
    env.store.update(
        lambda config: config["backup"].update({"restore_test": settings("0 5 * * 0")})
    )
    assert env.client.get(URL).json()["due_now"] is True
    env.db.runtime_set(
        "scheduler_pause", {"paused": True, "until": env.clock["now"] + 3600}
    )
    paused = env.client.get(URL).json()
    assert paused["due_now"] is False
    assert paused["scheduler_paused"] is True
    assert any("pausiert" in warning for warning in paused["warnings"])
    env.db.runtime_delete("scheduler_pause")
    env.store.update(lambda config: config["backup"].update({"enabled": False}))
    disabled = env.client.get(URL).json()
    assert disabled["due_now"] is False
    assert disabled["scheduler_paused"] is False
    assert any("deaktiviert" in warning for warning in disabled["warnings"])


def test_disabled_data_paths_are_not_planned(environment):
    env = environment
    env.store.update(
        lambda config: config["backup"]["pairs"][1].update({"enabled": False})
    )
    result = env.client.get(URL).json()
    assert [path["name"] for path in result["data_paths"]] == ["Fotos"]
    assert result["data_paths"][0]["id"] == "a" * 32


def test_revision_conflict_keeps_parallel_unrelated_change(environment):
    env = environment
    stale = body(env)
    second_store = Config(env.path)
    second_store.update(
        lambda config: config["web"].update({"username": "other-admin"})
    )
    # Keep the request authenticated while using the original plan revision.
    env.client.cookies.set(auth.SESSION_COOKIE, auth.create_session("other-admin"))
    current = env.store.snapshot_with_revision()

    response = env.client.put(URL, json=stale)

    assert response.status_code == 409
    assert env.store.snapshot_with_revision() == current
    assert env.store.get("web", "username") == "other-admin"
    assert env.store.get("backup", "restore_test", "schedule") == "0 5 1 * *"


def test_conflict_between_preview_and_atomic_write_keeps_parallel_change(
    environment, monkeypatch
):
    env = environment
    original_replace = env.store.replace

    def raced_replace(draft, **kwargs):
        Config(env.path).update(
            lambda config: config.update({"custom_operator_note": "parallel edit"})
        )
        return original_replace(draft, **kwargs)

    monkeypatch.setattr(env.store, "replace", raced_replace)
    response = env.client.put(URL, json=body(env))
    assert response.status_code == 409
    assert env.store.get("custom_operator_note") == "parallel edit"
    assert env.store.get("backup", "restore_test", "schedule") == "0 5 1 * *"


def test_save_updates_only_restore_settings_and_revision(environment):
    env = environment
    before, revision = env.store.snapshot_with_revision()
    expected = copy.deepcopy(before)
    expected["backup"]["restore_test"] = settings()
    response = env.client.put(URL, json=body(env))
    assert response.status_code == 200
    assert env.store.snapshot() == expected
    assert response.json()["revision"] != revision
    assert response.json()["revision"] == env.store.revision


def test_save_preserves_unnormalized_unrelated_config(environment):
    env = environment
    original = env.store.snapshot()
    original.pop("schema_version")
    original["web"].pop("secure_cookie")
    original["backup"].pop("scheduler_retry_minutes")
    env.store.replace(original)
    expected = copy.deepcopy(original)
    expected["backup"]["restore_test"] = settings()
    response = env.client.put(URL, json=body(env))
    assert response.status_code == 200
    assert env.store.snapshot() == expected


@pytest.mark.parametrize(
    "method,suffix", [("get", ""), ("post", "/preview"), ("put", "")]
)
def test_restore_plan_requires_authentication(environment, method, suffix):
    env = environment
    env.client.cookies.delete(auth.SESSION_COOKIE)
    kwargs = {} if method == "get" else {"json": body(env)}
    assert getattr(env.client, method)(URL + suffix, **kwargs).status_code == 401


@pytest.mark.parametrize("method,suffix", [("post", "/preview"), ("put", "")])
def test_preview_and_save_require_csrf(environment, method, suffix):
    env = environment
    del env.client.headers["X-CSRF-Token"]
    before = env.path.read_bytes()
    assert getattr(env.client, method)(URL + suffix, json=body(env)).status_code == 403
    assert env.path.read_bytes() == before
