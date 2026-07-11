import socket

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
