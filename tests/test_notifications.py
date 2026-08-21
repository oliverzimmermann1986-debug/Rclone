import socket
import threading
import time

import pytest

from app import notifications


def test_private_webhook_destination_is_blocked(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
        ],
    )
    with pytest.raises(ValueError, match="blockiert"):
        notifications._resolved_addresses("example.test", 443, allow_private=False)


def test_public_webhook_destination_can_be_resolved(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )
    assert notifications._resolved_addresses(
        "example.test", 443, allow_private=False
    ) == ["93.184.216.34"]


def test_notification_policy_clamps_invalid_manual_config(monkeypatch):
    class BrokenConfig:
        def get(self, *keys, default=None):
            values = {
                ("notifications", "allow_http"): False,
                ("notifications", "allow_private_targets"): False,
                ("notifications", "timeout_seconds"): "not-a-number",
                ("notifications", "max_parallel"): 9999,
            }
            return values.get(tuple(keys), default)

    monkeypatch.setattr(notifications, "get_config", lambda: BrokenConfig())
    allow_http, allow_private, timeout, workers = notifications._notification_policy()
    assert allow_http is False
    assert allow_private is False
    assert timeout == 10.0
    assert workers == 16


def test_notify_returns_after_total_deadline_when_hook_stalls(monkeypatch):
    class Config:
        def get(self, *keys, default=None):
            if tuple(keys) == ("notifications", "webhooks"):
                return [
                    {
                        "enabled": True,
                        "type": "generic",
                        "url": "https://example.test/hook",
                        "events": ["sync_ok"],
                    }
                ]
            return default

    release = threading.Event()
    monkeypatch.setattr(notifications, "get_config", lambda: Config())
    monkeypatch.setattr(
        notifications,
        "_notification_policy",
        lambda: (False, False, 0.05, 1),
    )
    monkeypatch.setattr(
        notifications,
        "notify_one",
        lambda *_args, **_kwargs: release.wait(2),
    )

    started = time.monotonic()
    notifications.notify("sync_ok", "Fertig", "Test")
    elapsed = time.monotonic() - started
    release.set()

    assert elapsed < 0.5
