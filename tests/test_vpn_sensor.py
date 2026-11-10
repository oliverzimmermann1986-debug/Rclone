import json
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import vpn_sensor
from app.auth import require_auth
from app.routes import api_vpn

NOW = 1_800_000_000
PRIV = "PRIVATEKEYPRIVATEKEYPRIVATEKEYPRIVATEKEY00="
PSK = "PRESHAREDKEYPRESHAREDKEYPRESHAREDKEY000="
PEER1 = "AAAAAAAAbbbbbbbbccccccccddddddddeeeeeeee00="
PEER2 = "ZZZZZZZZyyyyyyyyxxxxxxxxwwwwwwwwvvvvvvvv11="
DUMP = "\n".join(
    [
        "\t".join(["wg0", PRIV, "IFACEPUBKEY=", "51820", "off"]),
        "\t".join(
            [
                "wg0",
                PEER1,
                PSK,
                "203.0.113.7:51820",
                "10.0.0.2/32,10.0.1.0/24",
                str(NOW - 42),
                "1000",
                "2000",
                "25",
            ]
        ),
        "\t".join(["wg0", PEER2, "(none)", "(none)", "(none)", "0", "0", "0", "off"]),
    ]
)


def _run(stdout="", returncode=0, stderr=""):
    return MagicMock(
        return_value=SimpleNamespace(
            stdout=stdout, returncode=returncode, stderr=stderr
        )
    )


@pytest.fixture(autouse=True)
def _reset_cache():
    vpn_sensor._cache = None


def test_wireguard_parses_dump_without_leaking_keys():
    run = _run(DUMP)
    res = vpn_sensor.wireguard_status(run=run, now=NOW)

    assert run.call_args.args[0] == ["wg", "show", "all", "dump"]
    assert run.call_args.kwargs["timeout"] == 10
    assert res["available"] is True and res["error"] is None
    (iface,) = res["interfaces"]
    assert iface["name"] == "wg0" and iface["listen_port"] == 51820
    p1, p2 = iface["peers"]
    assert p1 == {
        "public_key_short": "AAAAAAAA…",
        "endpoint": "203.0.113.7:51820",
        "allowed_ips": ["10.0.0.2/32", "10.0.1.0/24"],
        "handshake_age_seconds": 42,
        "rx_bytes": 1000,
        "tx_bytes": 2000,
    }
    assert (
        p2["handshake_age_seconds"] is None
        and p2["endpoint"] is None
        and p2["allowed_ips"] == []
    )
    dumped = json.dumps(res)
    assert PRIV not in dumped and PSK not in dumped and PEER1 not in dumped


def test_wireguard_unavailable():
    missing = vpn_sensor.wireguard_status(
        run=MagicMock(side_effect=FileNotFoundError())
    )
    denied = vpn_sensor.wireguard_status(
        run=_run(
            returncode=1, stderr="Unable to access interface: Operation not permitted\n"
        )
    )
    hung = vpn_sensor.wireguard_status(
        run=MagicMock(side_effect=subprocess.TimeoutExpired("wg", 10))
    )

    assert missing["available"] is False and "nicht installiert" in missing["error"]
    assert denied["available"] is False and "Operation not permitted" in denied["error"]
    assert hung["available"] is False and hung["interfaces"] == []


def test_tailscale_parses_json():
    payload = {
        "Self": {
            "HostName": "nas",
            "Online": True,
            "TailscaleIPs": ["100.64.0.1", "fd7a::1"],
        },
        "Peer": {
            "nodekey:1": {
                "HostName": "backup",
                "Online": False,
                "LastSeen": "2026-10-30T08:00:00Z",
                "RxBytes": 5,
                "TxBytes": 6,
                "OS": "linux",
            },
            "nodekey:2": {
                "HostName": "new",
                "Online": True,
                "LastSeen": "0001-01-01T00:00:00Z",
            },
        },
    }
    res = vpn_sensor.tailscale_status(run=_run(json.dumps(payload)))

    assert res["available"] is True
    assert res["self"] == {
        "hostname": "nas",
        "online": True,
        "tailscale_ips": ["100.64.0.1", "fd7a::1"],
    }
    assert res["peers"][0] == {
        "hostname": "backup",
        "online": False,
        "last_seen": "2026-10-30T08:00:00Z",
        "rx_bytes": 5,
        "tx_bytes": 6,
        "os": "linux",
    }
    assert res["peers"][1]["last_seen"] is None


def test_tailscale_unavailable():
    assert (
        vpn_sensor.tailscale_status(run=MagicMock(side_effect=FileNotFoundError()))[
            "available"
        ]
        is False
    )
    assert vpn_sensor.tailscale_status(run=_run(returncode=1))["available"] is False
    bad = vpn_sensor.tailscale_status(run=_run("kein json"))
    assert bad["available"] is False and "nicht lesbar" in bad["error"]


def test_probe_reachable_and_unreachable():
    def connect(addr, timeout):
        if addr[1] == 22:
            return MagicMock()
        raise TimeoutError()

    ok, down = vpn_sensor.probe_targets(
        ["backup.example:22", "[::1]:2222"], connect=connect
    )

    assert (
        ok["reachable"] is True and ok["host"] == "backup.example" and ok["port"] == 22
    )
    assert isinstance(ok["latency_ms"], float) and ok["error"] is None
    assert down["reachable"] is False and down["host"] == "::1" and down["port"] == 2222
    assert down["latency_ms"] is None and "Zeitüberschreitung" in down["error"]


@pytest.mark.parametrize(
    "target",
    [
        "nohost",
        "host:0",
        "host:65536",
        "host:abc",
        "ho st:22",
        "ho\tst:22",
        "a" * 254 + ":22",
        "::1:22",
        ":22",
        5,
    ],
)
def test_probe_invalid_target_never_connects(target):
    connect = MagicMock()
    (res,) = vpn_sensor.probe_targets([target], connect=connect)

    assert res["reachable"] is False and res["error"]
    connect.assert_not_called()


def test_probe_limit_is_ten():
    connect = MagicMock()
    res = vpn_sensor.probe_targets([f"h{i}:22" for i in range(12)], connect=connect)

    assert len(res) == 10 and connect.call_count == 10


def test_snapshot_cache_and_refresh(monkeypatch):
    calls = []
    monkeypatch.setattr(
        vpn_sensor,
        "wireguard_status",
        lambda **kw: calls.append("wg") or {"available": False},
    )
    monkeypatch.setattr(
        vpn_sensor, "tailscale_status", lambda **kw: {"available": False}
    )
    monkeypatch.setattr(
        vpn_sensor,
        "probe_targets",
        lambda targets, **kw: [{"target": t} for t in targets],
    )
    cfg = SimpleNamespace(get=lambda *k, default=None: ["h:22", 7])

    first = vpn_sensor.vpn_snapshot(cfg, now=NOW)
    assert first["probes"] == [{"target": "h:22"}] and first["checked_at"] == NOW
    assert vpn_sensor.vpn_snapshot(cfg, now=NOW + 29) is first
    assert len(calls) == 1
    vpn_sensor.vpn_snapshot(cfg, now=NOW + 29, refresh=True)
    assert len(calls) == 2
    vpn_sensor.vpn_snapshot(cfg, now=NOW + 29 + 30)
    assert len(calls) == 3


def test_snapshot_without_config_value(monkeypatch):
    monkeypatch.setattr(
        vpn_sensor, "wireguard_status", lambda **kw: {"available": False}
    )
    monkeypatch.setattr(
        vpn_sensor, "tailscale_status", lambda **kw: {"available": False}
    )
    cfg = SimpleNamespace(get=lambda *k, default=None: "kein-list")

    assert vpn_sensor.vpn_snapshot(cfg, now=NOW)["probes"] == []


def test_route_requires_auth_and_returns_snapshot(monkeypatch):
    monkeypatch.setattr(
        api_vpn, "vpn_snapshot", lambda refresh=False: {"refresh": refresh}
    )
    app = FastAPI()
    app.include_router(api_vpn.router)
    client = TestClient(app)

    assert client.get("/api/diagnostics/vpn").status_code == 401
    app.dependency_overrides[require_auth] = lambda: "admin"
    assert client.get("/api/diagnostics/vpn?refresh=1").json() == {"refresh": True}
    assert client.get("/api/diagnostics/vpn").json() == {"refresh": False}
