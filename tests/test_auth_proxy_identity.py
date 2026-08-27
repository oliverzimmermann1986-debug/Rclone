from __future__ import annotations

from pathlib import Path

from fastapi import Request

from app import auth

ROOT = Path(__file__).resolve().parents[1]


class _Config:
    def __init__(self, trusted_proxy_ips: list[str]):
        self.trusted_proxy_ips = trusted_proxy_ips

    def get(self, *keys, default=None):
        if keys == ("web", "trusted_proxy_ips"):
            return self.trusted_proxy_ips
        return default


def _request(peer: str, forwarded_for: str = "") -> Request:
    headers = []
    if forwarded_for:
        headers.append((b"x-forwarded-for", forwarded_for.encode("ascii")))
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/login",
            "headers": headers,
            "client": (peer, 12345),
        }
    )


def test_untrusted_socket_peer_cannot_spoof_or_rotate_forwarded_identity(monkeypatch):
    monkeypatch.setattr(auth, "get_config", lambda: _Config(["127.0.0.1/32"]))

    first = auth.login_key(_request("198.51.100.8", "203.0.113.10"), "admin")
    rotated = auth.login_key(_request("198.51.100.8", "203.0.113.99"), "admin")

    assert first == "198.51.100.8"
    assert rotated == first


def test_trusted_proxy_chain_is_validated_from_right_and_ignores_left_spoofing(monkeypatch):
    monkeypatch.setattr(
        auth,
        "get_config",
        lambda: _Config(["127.0.0.0/8", "10.0.0.0/8"]),
    )

    first = auth.login_key(
        _request("127.0.0.1", "192.0.2.10, 203.0.113.44, 10.2.3.4"),
        "admin",
    )
    rotated = auth.login_key(
        _request("127.0.0.1", "192.0.2.99, 203.0.113.44, 10.2.3.4"),
        "admin",
    )

    assert first == "203.0.113.44"
    assert rotated == first


def test_invalid_forwarded_chain_falls_back_to_socket_peer(monkeypatch):
    monkeypatch.setattr(auth, "get_config", lambda: _Config(["127.0.0.1/32"]))

    assert auth.login_key(_request("127.0.0.1", "not-an-ip"), "admin") == "127.0.0.1"


def test_service_keeps_raw_socket_peer_for_application_proxy_validation():
    service = (ROOT / "systemd" / "rclone-sync-web.service").read_text(encoding="utf-8")

    assert "--no-proxy-headers" in service
    assert "--forwarded-allow-ips" not in service
