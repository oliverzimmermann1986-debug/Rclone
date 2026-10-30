"""Capacity predictions require real, bounded and sufficiently old observations."""

import copy
import sys
import time
from types import SimpleNamespace

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import storage_forecast as forecast
from app.auth import require_auth
from app.db import Database
from app.config_store import Config
from app.routes import api_storage_forecast
from app.security import require_csrf

DAY = 86400
NOW = 1_800_000_000.0
OPTIONS = {"sample_interval_hours": 24, "warning_days": 30, "history_days": 180}


def config(pairs=1, **settings):
    return {
        "backup": {
            "storage_forecast": settings,
            "pairs": [
                {
                    "id": f"{index:032x}",
                    "name": f"Datenweg {index}",
                    "local": f"/data/{index}",
                    "remote": f"cloud{index}:/backup",
                }
                for index in range(1, pairs + 1)
            ],
        }
    }


def observations(values, *, total=10000):
    return [
        {
            "observed_at": NOW - (len(values) - index - 1) * DAY,
            "total_bytes": total,
            "used_bytes": value,
            "free_bytes": max(0, total - value),
            "state": "ok",
            "reason": "Belegung des Dateisystems",
        }
        for index, value in enumerate(values)
    ]


def test_sparse_observations_do_not_invent_a_full_date():
    for rows in ([], observations([100]), observations([100, 200])):
        item = forecast.estimate(rows, now=NOW, options=OPTIONS)
        assert item["state"] == "insufficient_samples"
        assert item["estimated_full_at"] is None
        assert item["growth_bytes_per_day"] is None


def test_robust_linear_growth_reports_capacity_date_and_confidence():
    rows = observations([1000 + day * 100 for day in range(15)])
    item = forecast.estimate(rows, now=NOW, options=OPTIONS)
    assert item["growth_bytes_per_day"] == 100
    assert item["days_to_full"] == 76
    assert item["estimated_full_at"] == NOW + 76 * DAY
    assert item["state"] == "ok" and item["confidence"] == "high"
    warning = forecast.estimate(rows, now=NOW, options={**OPTIONS, "warning_days": 90})
    assert warning["state"] == "warning"


def test_isolated_outlier_does_not_create_a_spurious_capacity_alarm():
    values = [1000 + day * 100 for day in range(11)]
    values[5] = 9900
    item = forecast.estimate(observations(values), now=NOW, options=OPTIONS)
    assert item["growth_bytes_per_day"] == 100
    assert item["days_to_full"] == 80


@pytest.mark.parametrize(
    "values",
    [[1000] * 8, [1000, 1002, 998, 1001, 999, 1000, 998], [9000, 8000, 7000, 6000]],
)
def test_flat_noise_and_decreasing_usage_do_not_predict_filling(values):
    item = forecast.estimate(observations(values), now=NOW, options=OPTIONS)
    assert item["state"] in {"not_growing", "uncertain"}
    assert item["estimated_full_at"] is None and item["days_to_full"] is None


def test_stale_evidence_and_unknown_quotas_remain_explicit():
    rows = observations([1000, 2000, 3000])
    stale = forecast.estimate(rows, now=NOW + 3 * DAY, options=OPTIONS)
    assert stale["state"] == "stale" and stale["days_to_full"] is None
    unknown = forecast.estimate(
        [
            {
                "observed_at": NOW,
                "state": "unknown",
                "used_bytes": 123,
                "reason": "Keine Quote",
            }
        ],
        now=NOW,
        options=OPTIONS,
    )
    assert unknown["state"] == "unknown"
    assert unknown["total_bytes"] is None and unknown["estimated_full_at"] is None


def test_full_volume_is_visible_without_waiting_for_trend_samples():
    item = forecast.estimate(observations([10000]), now=NOW, options=OPTIONS)
    assert item["state"] == "full" and item["days_to_full"] == 0


def measurement(_target):
    return {
        "total_bytes": 10000,
        "used_bytes": 1000,
        "free_bytes": 9000,
        "state": "ok",
        "reason": "Testmessung",
    }


def test_daily_samples_deduplicate_and_due_sampling_uses_persisted_cache(
    tmp_path, monkeypatch
):
    database = Database(tmp_path / "observations.db")
    cfg = config()
    calls = []

    def probe(target):
        calls.append(target["identity"])
        return measurement(target)

    monkeypatch.setattr(forecast, "_probe_target", probe)
    assert forecast.sample_if_due(cfg, database, NOW)["sampled"] == 2
    assert forecast.sample_if_due(cfg, database, NOW + 60)["sampled"] == 0
    target = calls[0]
    forecast.sample_targets(cfg, database, identity=target, now=NOW + 3600, force=True)
    assert forecast.history(cfg, database, target, NOW + 3600)["total"] == 1
    assert forecast.sample_if_due(cfg, database, NOW + 2 * DAY)["sampled"] == 2
    assert forecast.history(cfg, database, target, NOW + 2 * DAY)["total"] == 2
    assert len(calls) == 5


def test_rotating_tick_budget_does_not_starve_later_targets(tmp_path, monkeypatch):
    database = Database(tmp_path / "rotation.db")
    cfg = config(pairs=10)
    calls = []

    def probe(target):
        calls.append(target["identity"])
        return measurement(target)

    monkeypatch.setattr(forecast, "_probe_target", probe)
    first = forecast.sample_if_due(cfg, database, NOW)
    second = forecast.sample_if_due(cfg, database, NOW + 60)
    third = forecast.sample_if_due(cfg, database, NOW + 120)
    assert [first["sampled"], second["sampled"], third["sampled"]] == [8, 8, 4]
    assert len(set(calls)) == 20


def test_changed_target_binding_never_reuses_old_growth_evidence(tmp_path, monkeypatch):
    database = Database(tmp_path / "bindings.db")
    cfg = config()
    monkeypatch.setattr(forecast, "_probe_target", measurement)
    forecast.sample_if_due(cfg, database, NOW)
    changed = copy.deepcopy(cfg)
    changed["backup"]["pairs"][0]["remote"] = "replacement:/backup"
    identity = forecast.targets(changed)[1]["identity"]
    assert forecast.history(changed, database, identity, NOW)["total"] == 0


def test_history_prunes_old_rows_and_does_not_use_future_measurements(
    tmp_path, monkeypatch
):
    database = Database(tmp_path / "history.db")
    cfg = config(history_days=7)
    monkeypatch.setattr(forecast, "_probe_target", measurement)
    for day in range(10):
        forecast.sample_if_due(cfg, database, NOW + day * DAY)
    identity = forecast.targets(cfg)[0]["identity"]
    rows = forecast.history(cfg, database, identity, NOW + 9 * DAY)["items"]
    assert rows[0]["observed_at"] >= NOW + 2 * DAY
    assert all(
        row["observed_at"] <= NOW + 7 * DAY
        for row in forecast.history(cfg, database, identity, NOW + 7 * DAY)["items"]
    )


def test_remote_quota_is_never_fabricated_and_configured_capacity_is_supported(
    monkeypatch,
):
    target = forecast.targets(config())[1]

    def unsupported(command, _deadline):
        if command[1] == "about":
            raise ValueError("unsupported")
        return {"bytes": 500}

    monkeypatch.setattr(forecast, "_bounded_json_command", unsupported)
    unknown = forecast._probe_target(target)
    assert unknown["state"] == "unknown" and "total_bytes" not in unknown
    configured = forecast._probe_target({**target, "capacity": 1000})
    assert configured["total_bytes"] == 1000
    assert configured["used_bytes"] == configured["free_bytes"] == 500


def test_local_probe_reads_actual_filesystem_without_a_network_request(tmp_path):
    value = forecast._probe_target({"kind": "local", "endpoint": str(tmp_path)})
    assert value["state"] == "ok"
    assert value["total_bytes"] > 0 and value["free_bytes"] >= 0


def test_probe_rejects_unbounded_output_and_terminates_stalled_child():
    with pytest.raises(ValueError, match="groß"):
        forecast._bounded_json_command(
            [sys.executable, "-I", "-c", "print('x'*100000)"], time.monotonic() + 3
        )
    before = time.monotonic()
    with pytest.raises(TimeoutError):
        forecast._bounded_json_command(
            [sys.executable, "-I", "-c", "import time; time.sleep(10)"], before + 0.1
        )
    assert time.monotonic() - before < 3


def test_api_uses_existing_auth_csrf_and_unknown_target_checks(tmp_path, monkeypatch):
    application = FastAPI()
    application.include_router(api_storage_forecast.router)
    database = Database(tmp_path / "routes.db")
    store = SimpleNamespace(snapshot_with_revision=lambda: (config(), "revision-1"))
    monkeypatch.setattr(api_storage_forecast, "get_config", lambda: store)
    monkeypatch.setattr(api_storage_forecast, "get_db", lambda: database)
    monkeypatch.setattr(forecast, "_probe_target", measurement)
    client = TestClient(application)
    assert client.get("/api/storage/forecast").status_code == 401
    application.dependency_overrides[require_auth] = lambda: "admin"
    assert client.post("/api/storage/forecast/sample", json={}).status_code == 403
    application.dependency_overrides[require_csrf] = lambda: None
    assert client.post("/api/storage/forecast/sample", json={}).json()["sampled"] == 2
    assert client.get("/api/storage/forecast?identity=unknown").status_code == 404
    assert (
        client.get("/api/storage/forecast/history?identity=unknown").status_code == 404
    )


def test_capacity_override_uses_atomic_config_revision_and_preserves_other_settings(
    tmp_path, monkeypatch
):
    cfg = config(warning_days=45)
    cfg["paths"] = {
        "data_dir": str(tmp_path / "server"),
        "logs_dir": str(tmp_path / "logs"),
        "temp_dir": str(tmp_path / "temp"),
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    store = Config(path)
    database = Database(tmp_path / "capacity.db")
    application = FastAPI()
    application.include_router(api_storage_forecast.router)
    application.dependency_overrides[require_auth] = lambda: "admin"
    application.dependency_overrides[require_csrf] = lambda: None
    monkeypatch.setattr(api_storage_forecast, "get_config", lambda: store)
    monkeypatch.setattr(api_storage_forecast, "get_db", lambda: database)
    client = TestClient(application)
    identity = forecast.targets(cfg)[1]["identity"]
    revision = client.get("/api/storage/forecast").json()["_revision"]
    payload = {
        "identity": identity,
        "capacity_bytes": 123456789,
        "expected_revision": revision,
    }
    saved = client.put("/api/storage/forecast/capacity", json=payload)
    assert saved.status_code == 200, saved.text
    assert saved.json()["_revision"] != revision
    assert store.get("backup", "storage_forecast", "capacities")[identity] == 123456789
    assert store.get("backup", "storage_forecast", "warning_days") == 45
    assert client.put("/api/storage/forecast/capacity", json=payload).status_code == 409
    payload.update(expected_revision=saved.json()["_revision"], capacity_bytes=None)
    assert client.put("/api/storage/forecast/capacity", json=payload).status_code == 200
    assert identity not in store.get("backup", "storage_forecast", "capacities")
    payload.update(expected_revision=store.revision, capacity_bytes=True)
    assert client.put("/api/storage/forecast/capacity", json=payload).status_code == 422
