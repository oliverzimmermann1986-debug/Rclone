import json
import ssl
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import heartbeat
from app.db import Database


@pytest.fixture
def database(tmp_path):
    return Database(tmp_path / "heartbeat.db")


def config(**changes):
    return {
        "notifications": {
            "heartbeat": {
                "enabled": True,
                "secret_url": "https://receiver.example/ping/private-token",
                **changes,
            }
        }
    }


def test_disabled_unconfigured_and_lockdown_never_send(database, monkeypatch):
    monkeypatch.setattr(heartbeat, "_send", lambda *_: pytest.fail("network disabled"))
    assert not heartbeat.tick(config(enabled=False), database, now=100)["attempted"]
    assert not heartbeat.tick(config(secret_url=""), database, now=100)["attempted"]
    database.runtime_set("lockdown", {"active": True})
    assert heartbeat.tick(config(), database, now=100)["blocked_by_lockdown"]


def test_persistent_dedup_across_database_instances_and_retry(database, monkeypatch):
    codes = iter([503, 204])
    calls = []
    monkeypatch.setattr(
        heartbeat, "_send", lambda raw: calls.append(True) or next(codes)
    )
    first = heartbeat.tick(config(), database, now=100)
    assert first["status"]["consecutive_failures"] == 1
    another = Database(database.path)
    assert not heartbeat.tick(config(), another, now=159)["attempted"]
    assert heartbeat.tick(config(), another, now=160)["ok"]
    assert len(calls) == 2
    assert heartbeat.status(config(), database)["last_success_at"] == 160
    assert heartbeat.status(config(), database)["consecutive_failures"] == 0


def test_concurrent_ticks_have_one_network_owner(database, monkeypatch):
    calls = []
    monkeypatch.setattr(heartbeat, "_send", lambda raw: calls.append(True) or 200)
    with ThreadPoolExecutor(max_workers=4) as pool:
        result = list(
            pool.map(
                lambda _: heartbeat.tick(config(), Database(database.path), now=500),
                range(4),
            )
        )
    assert len(calls) == 1
    assert sum(item["attempted"] for item in result) == 1


def test_manual_test_uses_stored_receiver_but_stays_rate_limited(database, monkeypatch):
    monkeypatch.setattr(heartbeat, "_send", lambda raw: 200)
    assert heartbeat.tick(config(enabled=False), database, now=100, test=True)["ok"]
    with pytest.raises(heartbeat.HeartbeatRateLimitError):
        heartbeat.tick(config(enabled=False), database, now=101, test=True)


def test_tls_errors_never_persist_secret_url_or_exception(database, monkeypatch):
    def fail(_raw):
        raise ssl.SSLCertVerificationError(
            "https://receiver.example/ping/private-token secret-password"
        )

    monkeypatch.setattr(heartbeat, "_send", fail)
    result = heartbeat.tick(config(), database, now=100)
    assert "TLS" in result["status"]["last_error"]
    serialized = json.dumps([result, database.runtime_get(heartbeat._STATE_KEY)])
    assert "private-token" not in serialized and "secret-password" not in serialized
    assert (
        heartbeat.status(config(secret_url="https://other.example/new"), database)[
            "last_success_at"
        ]
        is None
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://receiver.example/ping",
        "https://user:pass@receiver.example/ping",
        "https://receiver.example/#fragment",
        "https://127.0.0.1/ping",
        "https://localhost/ping",
        "https://receiver.example/ping\r\nHeader:value",
    ],
)
def test_receiver_validation_rejects_unsafe_urls_without_dns(url, monkeypatch):
    monkeypatch.setattr(
        heartbeat.socket,
        "getaddrinfo",
        lambda *_a, **_k: pytest.fail("no DNS during validation"),
    )
    with pytest.raises(ValueError):
        heartbeat.parse_receiver(url)


def test_private_receiver_requires_optin_and_connection_does_not_follow_redirect(
    database, monkeypatch
):
    heartbeat.parse_receiver("https://127.0.0.1/ping", allow_private_hosts=True)
    monkeypatch.setattr(heartbeat, "resolve_host", lambda *_a, **_k: "203.0.113.20")
    calls = []

    class Connection:
        sock = None

        def __init__(self, *args):
            calls.append(args)

        def request(self, *args, **kwargs):
            calls.append((args, kwargs))

        def getresponse(self):
            return type("R", (), {"status": 302})()

        def close(self):
            pass

    monkeypatch.setattr(heartbeat, "_PinnedHTTPSConnection", Connection)
    result = heartbeat.tick(config(), database, now=100)
    assert result["status"]["status_code"] == 302
    assert result["ok"] is False
    assert len(calls) == 2
    assert calls[1][0] == ("GET", "/ping/private-token")


def test_dns_private_result_is_rejected_even_for_public_looking_name(monkeypatch):
    monkeypatch.setattr(
        heartbeat.socket,
        "getaddrinfo",
        lambda *_a, **_k: [(2, 1, 6, "", ("127.0.0.1", 443))],
    )
    with pytest.raises(ValueError):
        heartbeat.resolve_host("receiver.example", 443, 1, public_only=True)


def test_overdue_or_crashed_heartbeat_does_not_appear_healthy(database, monkeypatch):
    monkeypatch.setattr(heartbeat, "_send", lambda *_: 200)
    heartbeat.tick(config(), database, now=100)
    assert heartbeat.status(config(), database, now=120)["state"] == "ok"
    assert heartbeat.status(config(), database, now=281)["state"] == "error"
    database.runtime_update(
        heartbeat._STATE_KEY,
        lambda state: dict(state, claim="crashed", claim_until=200),
    )
    assert (
        "nicht abgeschlossen"
        in heartbeat.status(config(), database, now=300)["last_error"]
    )
