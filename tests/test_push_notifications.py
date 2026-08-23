from pathlib import Path
import json
import time

import pytest

from app import push_notifications
from app.config_validation import ConfigValidationError, validate_config
from app.db import Database
from app.routes import api_push


TOKEN = "ab" * 32


def _base_config(tmp_path: Path) -> dict:
    return {
        "web": {"username": "admin", "local_browse_roots": [str(tmp_path)]},
        "paths": {
            "data_dir": str(tmp_path),
            "logs_dir": str(tmp_path / "logs"),
            "temp_dir": str(tmp_path / "temp"),
        },
        "backup": {"pairs": [], "jobs": [], "default_schedule": "manual"},
        "notifications": {"webhooks": []},
    }


def test_push_device_registry_is_idempotent_and_removable(tmp_path: Path):
    database = Database(tmp_path / "push.db")

    database.push_device_upsert(TOKEN, "sandbox", app_version="1.0.9", now=10)
    database.push_device_upsert(TOKEN, "production", app_version="1.0.10", now=20)

    assert database.push_devices(now=20) == [
        {
            "token": TOKEN,
            "environment": "production",
            "app_version": "1.0.10",
            "created_at": 10.0,
            "updated_at": 20.0,
        }
    ]
    assert database.push_device_delete(TOKEN) is True
    assert database.push_devices() == []


def test_apns_config_is_fail_closed_and_restricted_to_data_dir(tmp_path: Path):
    invalid = _base_config(tmp_path)
    invalid["notifications"]["apns"] = {
        "enabled": True,
        "team_id": "bad",
        "key_id": "bad",
        "key_file": str(tmp_path.parent / "AuthKey.p8"),
        "topic": "bad topic",
        "events": [],
    }

    with pytest.raises(ConfigValidationError):
        validate_config(invalid)

    valid = _base_config(tmp_path)
    valid["notifications"]["apns"] = {
        "enabled": True,
        "team_id": "ABCDEFGHIJ",
        "key_id": "KLMNOPQRST",
        "key_file": str(tmp_path / "AuthKey.p8"),
        "topic": "de.oliverzimmermann.rclonesync",
        "events": ["sync_error"],
    }
    normalized, _warnings = validate_config(valid)
    assert normalized["notifications"]["apns"]["enabled"] is True
    assert normalized["notifications"]["apns"]["events"] == ["sync_error"]
    assert normalized["notifications"]["apns"]["retention_hours"] == 24
    assert normalized["notifications"]["apns"]["max_attempts"] == 8
    assert normalized["notifications"]["apns"]["device_lease_days"] == 7


def test_push_route_validates_and_registers_device(monkeypatch, tmp_path: Path):
    database = Database(tmp_path / "route.db")
    monkeypatch.setattr(api_push, "get_db", lambda: database)

    result = api_push.register_push_device(
        api_push.PushDeviceRegistration(
            token=TOKEN,
            environment="production",
            app_version="1.0.10",
        )
    )

    assert result == {"ok": True, "registered": True}
    assert database.push_devices()[0]["token"] == TOKEN
    with pytest.raises(Exception) as raised:
        api_push.unregister_push_device(api_push.PushDeviceRemoval(token="g" * 64))
    assert getattr(raised.value, "status_code", None) == 422


class _Response:
    def __init__(self, status_code: int, reason: str = "", headers=None):
        self.status_code = status_code
        self._reason = reason
        self.headers = headers or {}

    def json(self):
        return {"reason": self._reason}


class _Client:
    def __init__(self, responses, calls, **kwargs):
        self.responses = iter(responses)
        self.calls = calls
        self.calls.append(("init", kwargs))

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def post(self, url, *, content, headers):
        self.calls.append((url, content, headers))
        return next(self.responses)


def test_apns_delivery_uses_http2_and_prunes_rejected_tokens(
    monkeypatch, tmp_path: Path
):
    database = Database(tmp_path / "delivery.db")
    rejected = "cd" * 32
    now = time.time()
    database.push_device_upsert(TOKEN, "production", now=now)
    database.push_device_upsert(rejected, "sandbox", now=now - 1)
    monkeypatch.setattr(
        push_notifications,
        "_settings",
        lambda _event: {
            "team_id": "ABCDEFGHIJ",
            "key_id": "KLMNOPQRST",
            "key_file": str(tmp_path / "AuthKey.p8"),
            "topic": "de.oliverzimmermann.rclonesync",
            "timeout": 5,
        },
    )
    monkeypatch.setattr(push_notifications, "_provider_token", lambda _settings: "jwt")
    calls = []
    responses = [_Response(200), _Response(410, "Unregistered")]

    result = push_notifications.send_push_notifications(
        "sync_error",
        "Fehler",
        "Ein Job ist fehlgeschlagen",
        db=database,
        client_factory=lambda **kwargs: _Client(responses, calls, **kwargs),
    )

    assert result == {
        "queued": 2,
        "sent": 1,
        "failed": 1,
        "removed": 1,
        "retrying": 0,
    }
    assert calls[0] == ("init", {"http2": True, "timeout": 5.0})
    urls = [call[0] for call in calls[1:]]
    assert any(url.startswith("https://api.push.apple.com/3/device/") for url in urls)
    assert any(
        url.startswith("https://api.sandbox.push.apple.com/3/device/") for url in urls
    )
    assert [item["token"] for item in database.push_devices()] == [TOKEN]
    request_headers = calls[1][2]
    assert int(request_headers["apns-expiration"]) > int(now)
    assert len(request_headers["apns-collapse-id"]) == 64
    assert request_headers["apns-id"]


def test_apns_retry_is_persisted_and_duplicate_event_is_not_requeued(
    monkeypatch, tmp_path: Path
):
    database = Database(tmp_path / "retry.db")
    database.push_device_upsert(TOKEN, "production")
    monkeypatch.setattr(
        push_notifications,
        "_settings",
        lambda _event: {
            "team_id": "ABCDEFGHIJ",
            "key_id": "KLMNOPQRST",
            "key_file": str(tmp_path / "AuthKey.p8"),
            "topic": "de.oliverzimmermann.rclonesync",
            "timeout": 5,
            "retention_seconds": 86400,
            "max_attempts": 8,
        },
    )
    monkeypatch.setattr(push_notifications, "_provider_token", lambda _settings: "jwt")
    calls = []

    first = push_notifications.send_push_notifications(
        "sync_error",
        "Fehler",
        "Fotos fehlgeschlagen",
        extra={"summary": {"job_id": 42, "run_id": "run-42"}},
        dedupe_key="sync_error:job-42",
        db=database,
        client_factory=lambda **kwargs: _Client(
            [_Response(503, "ServiceUnavailable")], calls, **kwargs
        ),
    )
    duplicate = push_notifications.send_push_notifications(
        "sync_error",
        "Fehler",
        "Fotos fehlgeschlagen",
        extra={"summary": {"job_id": 42, "run_id": "run-42"}},
        dedupe_key="sync_error:job-42",
        db=database,
        client_factory=lambda **kwargs: _Client([], calls, **kwargs),
    )

    assert first["queued"] == 1
    assert first["retrying"] == 1
    assert duplicate["queued"] == 0
    assert database.push_outbox_status()["pending"] == 1
    payload = json.loads(calls[1][1])
    assert payload["job_id"] == 42
    assert payload["run_id"] == "run-42"


def test_slow_apns_batch_renews_remaining_owner_claims(monkeypatch, tmp_path: Path):
    database = Database(tmp_path / "slow-batch.db")
    database.push_device_upsert(TOKEN, "production", now=100)
    database.push_device_upsert("cd" * 32, "production", now=99)
    for sequence in (1, 2):
        assert (
            database.push_outbox_enqueue(
                event="sync_error",
                title="Fehler",
                message=f"Fehler {sequence}",
                payload={"sequence": sequence},
                dedupe_key=f"slow:{sequence}",
                retention_seconds=86400,
                device_limit=1,
                now=100,
            )
            == 1
        )

    clock = {"now": 100.0}
    monkeypatch.setattr("app.db.time.time", lambda: clock["now"])
    monkeypatch.setattr(
        push_notifications,
        "_settings",
        lambda _event: {
            "team_id": "ABCDEFGHIJ",
            "key_id": "KLMNOPQRST",
            "key_file": str(tmp_path / "AuthKey.p8"),
            "topic": "de.oliverzimmermann.rclonesync",
            "timeout": 30,
            "max_attempts": 8,
        },
    )
    monkeypatch.setattr(push_notifications, "_provider_token", lambda _settings: "jwt")
    competing_results = []

    class SlowClient(_Client):
        def post(self, url, *, content, headers):
            self.calls.append((url, content, headers))
            if len(self.calls) == 2:
                clock["now"] = 165.0
            else:
                clock["now"] = 175.0
                competing_results.append(
                    push_notifications.dispatch_pending_pushes(
                        db=database,
                        client_factory=lambda **_kwargs: pytest.fail(
                            "Dispatcher B darf keine fremde APNs-Zeile senden"
                        ),
                        claim_owner="dispatcher-b",
                    )
                )
            return _Response(200)

    result = push_notifications.dispatch_pending_pushes(
        db=database,
        client_factory=lambda **kwargs: SlowClient([], [], **kwargs),
        claim_owner="dispatcher-a",
    )

    assert result["sent"] == 2
    assert competing_results == [{"sent": 0, "failed": 0, "removed": 0, "retrying": 0}]
    assert database.push_outbox_status()["sent"] == 2
