import hashlib
import http.client
import io
import json
import logging

import pytest

from app import pbs_api
from app.routes import api_pbs

SECRET = "S3CRET-token-value"
CERT = b"fake-der-certificate"
CERT_FP = ":".join(f"{b:02X}" for b in hashlib.sha256(CERT).digest())


@pytest.fixture(autouse=True)
def _fresh_caches(monkeypatch):
    monkeypatch.setattr(pbs_api, "_maintenance_cache", {})
    monkeypatch.setattr(api_pbs, "_insights_cache", {})


def _settings(**extra):
    return {
        "enabled": True,
        "repository": "sync@pbs!dash@pbs.example:store",
        "password": SECRET,
        "fingerprint": CERT_FP,
        **extra,
    }


class _FakeSock:
    def __init__(self, der, response):
        self.der, self.response, self.sent, self.closed = der, response, b"", False

    def getpeercert(self, binary_form=False):
        return self.der

    def sendall(self, data):
        self.sent += data

    def makefile(self, _mode):
        return io.BytesIO(self.response)

    def close(self):
        self.closed = True


def _fake_server(monkeypatch, *, status=200, body=b'{"data": {}}', der=CERT):
    socks = []

    def connect(conn):
        head = f"HTTP/1.1 {status} X\r\nContent-Length: {len(body)}\r\n\r\n".encode()
        conn.sock = _FakeSock(der, head + body)
        socks.append(conn.sock)

    monkeypatch.setattr(http.client.HTTPSConnection, "connect", connect)
    return socks


@pytest.mark.parametrize(
    ("repository", "expected"),
    [
        ("u@pbs!tok@pbs.example:ds", ("u@pbs!tok", "pbs.example", 8007, "ds")),
        ("root@pam@10.0.0.5:8008:ds", ("root@pam", "10.0.0.5", 8008, "ds")),
        ("pbs.example:ds", (None, "pbs.example", 8007, "ds")),
        ("u@pbs!t@[2001:db8::1]:ds", ("u@pbs!t", "2001:db8::1", 8007, "ds")),
        ("u@pbs!t@[::1]:8443:ds", ("u@pbs!t", "::1", 8443, "ds")),
    ],
)
def test_parse_repository_formats(repository, expected):
    repo = pbs_api.parse_repository(repository)
    assert (repo["auth_id"], repo["host"], repo["port"], repo["datastore"]) == expected


@pytest.mark.parametrize(
    "repository",
    [
        "",
        "ds",
        "host:",
        ":ds",
        "host:0:ds",
        "host:abc:ds",
        "host:70000:ds",
        "2001:db8::1:ds",
        "[::1:ds",
    ],
)
def test_parse_repository_rejects_invalid(repository):
    with pytest.raises(ValueError):
        pbs_api.parse_repository(repository)


def test_pinned_fingerprint_match_sends_token(monkeypatch):
    socks = _fake_server(
        monkeypatch,
        body=json.dumps({"data": {"total": 400, "used": 100, "avail": 300}}).encode(),
    )
    assert pbs_api.datastore_status(_settings()) == {
        "total": 400,
        "used": 100,
        "avail": 300,
    }
    sent = socks[0].sent
    assert sent.startswith(b"GET /api2/json/admin/datastore/store/status ")
    assert f"Authorization: PBSAPIToken=sync@pbs!dash:{SECRET}".encode() in sent


def test_pinned_fingerprint_mismatch_refuses_before_sending(monkeypatch):
    socks = _fake_server(monkeypatch, der=b"other-certificate")
    result = pbs_api.datastore_status(_settings())
    assert result == {"error": "TLS-Fingerprint des PBS stimmt nicht überein"}
    assert socks[0].closed and socks[0].sent == b""


def test_gc_mapping_and_dedup_factor(monkeypatch):
    monkeypatch.setattr(
        pbs_api,
        "_get",
        lambda _s, _p: {
            "index-data-bytes": 1000,
            "disk-bytes": 250,
            "removed-bytes": 7,
            "upid": "UPID:pbs:1",
            "last-run-endtime": 1_700_000_000,
        },
    )
    assert pbs_api.gc_status(_settings()) == {
        "index_data_bytes": 1000,
        "disk_bytes": 250,
        "dedup_factor": 4.0,
        "saved_bytes": 750,
        "removed_bytes": 7,
        "pending_bytes": None,
        "upid": "UPID:pbs:1",
        "last_run": 1_700_000_000,
    }
    monkeypatch.setattr(
        pbs_api, "_get", lambda _s, _p: {"index-data-bytes": 5, "disk-bytes": 0}
    )
    gc = pbs_api.gc_status(_settings())
    assert gc["dedup_factor"] is None and gc["saved_bytes"] == 5


def test_task_filter_ignores_prune_and_backup(monkeypatch):
    tasks = [
        {"worker_type": t, "worker_id": "store", "upid": f"UPID:{t}", "starttime": 1}
        for t in (
            "prune",
            "prunejob",
            "backup",
            "garbage_collection",
            "verificationjob",
        )
    ]
    monkeypatch.setattr(pbs_api, "_get", lambda _s, path: tasks + ["junk"])
    result = pbs_api.active_maintenance(_settings())
    assert result["running"] is True and result["datastore"] == "store"
    assert [t["type"] for t in result["tasks"]] == [
        "garbage_collection",
        "verificationjob",
    ]
    monkeypatch.setattr(pbs_api, "_get", lambda _s, path: tasks[:3])
    assert pbs_api.active_maintenance(_settings()) == {
        "running": False,
        "tasks": [],
        "datastore": "store",
    }


def test_task_filter_limits_to_own_datastore(monkeypatch):
    def task(worker_type, worker_id):
        return {"worker_type": worker_type, "worker_id": worker_id, "upid": "u"}

    tasks = [
        task("garbage_collection", "other"),
        task("verify", "other:ns/vm/100"),
        task("verificationjob", "store2:v-1"),  # Präfix-Falle: store ≠ store2
        task("garbage_collection", "store"),
        task("verify", "store:vm/100"),
        task("verificationjob", "store:v-1"),
        task("verify_group", "store"),
        {"worker_type": "garbage_collection", "upid": "u"},  # ohne worker_id
        task("garbage_collection", None),
    ]
    monkeypatch.setattr(pbs_api, "_get", lambda _s, path: tasks)
    result = pbs_api.active_maintenance(_settings())
    assert result["datastore"] == "store"
    assert [t["type"] for t in result["tasks"]] == [
        "garbage_collection",
        "verify",
        "verificationjob",
        "verify_group",
    ]
    monkeypatch.setattr(pbs_api, "_get", lambda _s, path: tasks[:3] + tasks[7:])
    assert pbs_api.active_maintenance(_settings())["running"] is False


def test_maintenance_cache_ttl_clamp_and_fail_open(monkeypatch):
    calls = []

    def fake_active(_settings):
        calls.append(1)
        return {"error": "PBS-API nicht erreichbar"}

    monkeypatch.setattr(pbs_api, "active_maintenance", fake_active)
    settings = _settings(gc_throttle={"poll_seconds": 5})  # → 15 s
    first = pbs_api.maintenance_active(settings, now=1000)
    assert first == {
        "active": False,
        "tasks": [],
        "checked_at": 1000,
        "error": "PBS-API nicht erreichbar",
    }
    assert pbs_api.maintenance_active(settings, now=1014)["checked_at"] == 1000
    assert pbs_api.maintenance_active(settings, now=1015)["checked_at"] == 1015
    assert len(calls) == 2
    assert (
        pbs_api.throttle_settings(_settings(gc_throttle={"poll_seconds": 5000}))[
            "poll_seconds"
        ]
        == 900
    )
    off = pbs_api.maintenance_active(
        _settings(gc_throttle={"enabled": False}), now=2000
    )
    assert off["active"] is False and len(calls) == 2


@pytest.mark.parametrize(
    ("settings", "status", "body", "expected"),
    [
        (
            _settings(password=SECRET + "\r\nX: y"),
            200,
            b"{}",
            "PBS-API-Abfrage fehlgeschlagen",
        ),
        (
            _settings(repository="root@pam@pbs.example:store"),
            200,
            b"{}",
            "Nur API-Token werden unterstützt",
        ),
        (_settings(), 401, b"", "PBS-API verweigert den Zugriff (HTTP 401)"),
        (
            _settings(),
            200,
            b"not json " + SECRET.encode(),
            "Antwort der PBS-API ist nicht lesbar",
        ),
        (_settings(), 200, b'{"data": []}', "Antwort der PBS-API ist unvollständig"),
    ],
)
def test_errors_never_leak_secret(
    monkeypatch, caplog, settings, status, body, expected
):
    caplog.set_level(logging.DEBUG)
    _fake_server(monkeypatch, status=status, body=body)
    result = pbs_api.datastore_status(settings)
    assert result == {"error": expected}
    assert SECRET not in repr(result) and SECRET not in caplog.text


def test_network_error_is_reported_without_secret(monkeypatch):
    def refuse(_conn):
        raise ConnectionRefusedError(f"refused {SECRET}")

    monkeypatch.setattr(http.client.HTTPSConnection, "connect", refuse)
    result = pbs_api.maintenance_active(_settings(), now=1)
    assert result["active"] is False and result["error"] == "PBS-API nicht erreichbar"
    assert SECRET not in repr(result)


def test_insights_endpoint_maps_and_caches(monkeypatch):
    settings = _settings()
    calls = {"status": 0, "gc": 0}

    def status(_s):
        calls["status"] += 1
        return {"total": 400, "used": 100, "avail": 300}

    def gc(_s):
        calls["gc"] += 1
        return {"dedup_factor": 4.0}

    monkeypatch.setattr(api_pbs.pbs_backup, "pbs_settings", lambda _cfg=None: settings)
    monkeypatch.setattr(pbs_api, "datastore_status", status)
    monkeypatch.setattr(pbs_api, "gc_status", gc)
    monkeypatch.setattr(
        pbs_api,
        "active_maintenance",
        lambda _s: {"running": True, "tasks": [{"type": "verify"}]},
    )

    body = api_pbs.pbs_insights()
    assert body["enabled"] is True and (body["host"], body["datastore"]) == (
        "pbs.example",
        "store",
    )
    assert body["storage"]["percent_used"] == 25.0 and body["dedup"] == {
        "dedup_factor": 4.0
    }
    assert body["maintenance"]["active"] is True
    assert body["gc_throttle"] == {"enabled": True, "poll_seconds": 60}
    assert SECRET not in repr(body)
    api_pbs.pbs_insights()
    assert calls == {"status": 1, "gc": 1}
    api_pbs.pbs_insights(refresh=True)
    assert calls == {"status": 2, "gc": 2}

    monkeypatch.setattr(
        pbs_api, "datastore_status", lambda _s: {"error": "PBS-API nicht erreichbar"}
    )
    failed = api_pbs.pbs_insights(refresh=True)
    assert failed["dedup"] == {"error": "PBS-API nicht erreichbar"} and calls["gc"] == 2

    settings["enabled"] = False
    assert api_pbs.pbs_insights(refresh=True) == {"enabled": False}
