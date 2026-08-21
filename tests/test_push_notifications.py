from pathlib import Path

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

    assert database.push_devices() == [
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
    def __init__(self, status_code: int, reason: str = ""):
        self.status_code = status_code
        self._reason = reason

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
    database.push_device_upsert(TOKEN, "production", now=20)
    database.push_device_upsert(rejected, "sandbox", now=10)
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

    assert result == {"sent": 1, "failed": 1, "removed": 1}
    assert calls[0] == ("init", {"http2": True, "timeout": 5.0})
    urls = [call[0] for call in calls[1:]]
    assert any(url.startswith("https://api.push.apple.com/3/device/") for url in urls)
    assert any(
        url.startswith("https://api.sandbox.push.apple.com/3/device/") for url in urls
    )
    assert [item["token"] for item in database.push_devices()] == [TOKEN]
