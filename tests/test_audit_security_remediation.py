"""Bounded regression fixtures for security changes from the October audit."""

import json
import logging
import multiprocessing
import os
import time
import threading
import stat
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException, Request
from fastapi.responses import Response

from app import auth, main, push_notifications, rclone_args
from app.db import Database
from app import db as database_module
from app.jobs import rclone_sync
from app.routes import api_config, api_webauthn


def _request(peer="127.0.0.1", proto="https", scheme="http"):
    return Request(
        {
            "type": "http",
            "scheme": scheme,
            "server": ("testserver", 80),
            "client": (peer, 12345),
            "method": "GET",
            "path": "/login",
            "headers": [
                (b"host", b"testserver"),
                (b"x-forwarded-proto", proto.encode()),
            ],
        }
    )


class Settings:
    def get(self, *keys, default=None):
        return {
            ("web", "trusted_proxy_ips"): ["127.0.0.1/32"],
            ("web", "secure_cookie"): "auto",
            ("web", "hsts_seconds"): 3600,
        }.get(tuple(keys), default)


@pytest.mark.parametrize(
    "peer,proto,scheme,secure",
    [
        ("127.0.0.1", "https", "http", True),
        ("198.51.100.9", "https", "http", False),
        ("127.0.0.1", "https,http", "http", False),
        ("127.0.0.1", "http", "http", False),
        ("198.51.100.9", "http", "https", True),
    ],
)
def test_cookie_and_hsts_use_only_explicitly_trusted_single_proto(
    monkeypatch, peer, proto, scheme, secure
):
    for module in (auth, main, api_webauthn):
        monkeypatch.setattr(module, "get_config", Settings)
    request = _request(peer, proto, scheme)
    assert auth.request_is_https(request) is secure
    assert main._cookie_secure(request) is secure
    assert api_webauthn._cookie_secure(request) is secure
    response = Response()
    main._apply_security_headers(response, request, "fixture")
    assert ("strict-transport-security" in response.headers) is secure


@pytest.mark.parametrize(
    "key,default,changed",
    [("login_window_seconds", 300, 60), ("session_max_age_seconds", 604800, 2592000)],
)
def test_session_and_login_window_changes_require_stepup(key, default, changed):
    old = {"web": {"username": "admin", key: default}}
    assert api_config._sensitive_config_changed(
        old, {"web": {"username": "admin", key: changed}}
    )
    assert not api_config._sensitive_config_changed(
        old, {"web": {"username": "admin", key: str(default)}}
    )
    assert not api_config._sensitive_config_changed(old, {"web": {"username": "admin"}})


def test_unfinished_quoted_backslashes_are_a_bounded_regex_case():
    # These branches must be disjoint: an escaped backslash must never also
    # match the ordinary character branch. Keep the runtime fixture small.
    pattern = rclone_args._SENSITIVE_FLAG_VALUE_RE.pattern
    assert '[^\\"\\\\]' in pattern
    assert "[^'\\\\]" in pattern
    unfinished = '--password "' + "\\" * 16
    assert isinstance(rclone_args.redact_command_text(unfinished), str)
    redacted = rclone_args.redact_command_text(
        '--password "secret\\\\value" --token=fixture'
    )
    assert "secret" not in redacted and "fixture" not in redacted


def test_invalid_native_exchange_is_rejected_before_generation_guard(monkeypatch):
    checks = []
    database = SimpleNamespace(
        native_auth_exchange_pending=lambda token, verifier: (
            checks.append((token, verifier)) or False
        )
    )
    monkeypatch.setattr(api_webauthn, "get_db", lambda: database)
    monkeypatch.setattr(
        api_webauthn,
        "account_generation_guard",
        lambda: pytest.fail(
            "anonymous invalid token reached the expensive generation guard"
        ),
    )
    body = api_webauthn.ExchangeRequest(token="a" * 32, verifier="b" * 43)
    with pytest.raises(HTTPException) as error:
        api_webauthn.exchange_native_session(body, _request())
    assert error.value.status_code == 401
    assert len(checks) == 1 and len(checks[0][0]) == 64


def test_challenge_quota_rejects_new_without_evicting_active_ceremonies(tmp_path):
    database = Database(tmp_path / "challenges.db")
    database.webauthn_challenge_create(
        challenge_id="registration",
        challenge=b"fixture",
        purpose="register",
        method="passkey",
    )
    future = time.time() + 300
    with database.conn() as connection:
        connection.executemany(
            "INSERT INTO webauthn_challenges(id,challenge,purpose,method,label,native,app_binding,account_generation,expires_at,created_at) VALUES(?,?,?,'passkey','',0,'','',?,?)",
            [
                (f"login-{index}", b"fixture", "authenticate", future, time.time())
                for index in range(512)
            ],
        )
    with pytest.raises(ValueError, match="offene Passkey"):
        database.webauthn_challenge_create(
            challenge_id="overflow",
            challenge=b"fixture",
            purpose="authenticate",
            method="passkey",
        )
    with database.conn() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM webauthn_challenges").fetchone()[0]
            == 513
        )
        assert connection.execute(
            "SELECT id FROM webauthn_challenges WHERE id='registration'"
        ).fetchone()
        assert connection.execute(
            "SELECT id FROM webauthn_challenges WHERE id='login-0'"
        ).fetchone()


@pytest.mark.parametrize("ready", [True, False])
def test_anonymous_readiness_contains_no_operator_warnings(
    monkeypatch, tmp_path, ready
):
    class Config:
        def get(self, *keys, default=None):
            return {"data_dir": str(tmp_path)} if keys == ("paths",) else default

    monkeypatch.setattr(main, "get_config", Config)
    monkeypatch.setattr(main, "database_path", lambda: tmp_path / "fixture.db")
    monkeypatch.setattr(main, "check_database_readonly", lambda _: ready)
    response = main.readyz()
    assert response.status_code == (200 if ready else 503)
    assert set(json.loads(response.body)) == {"ok", "version"}
    assert json.loads(response.body)["ok"] is ready


def test_readiness_checks_schema_without_running_database_integrity_scan(
    monkeypatch, tmp_path
):
    database = Database(tmp_path / "ready.db")
    connect = database_module.sqlite3.connect
    statements = []

    class Connection:
        def __init__(self, *args, **kwargs):
            self.actual = connect(*args, **kwargs)

        def execute(self, sql, *args):
            statements.append(sql)
            return self.actual.execute(sql, *args)

        def close(self):
            self.actual.close()

    monkeypatch.setattr(database_module.sqlite3, "connect", Connection)
    assert database_module.check_database_readonly(database.path)
    assert not any(
        "quick_check" in sql.lower() or "integrity_check" in sql.lower()
        for sql in statements
    )
    assert any("sqlite_master" in sql for sql in statements)


def test_resync_trigger_accepts_only_diagnostic_lines_not_hostile_filenames():
    positive = (
        "2026/10/29 13:14:15 ERROR : Bisync aborted. Must run --resync to recover."
    )
    assert rclone_sync._RESYNC_RE.search(positive)
    for line in (
        "x must run --resync",
        "2026/10/29 13:14:15 ERROR : copied x must run --resync",
        "2026/10/29 13:14:15 ERROR : Bisync aborted. Must run --resync to recover.: Failed to copy",
        "2026/10/29 13:14:15 NOTICE: photo.jpg: Bisync aborted. Must run --resync to recover.",
    ):
        assert not rclone_sync._RESYNC_RE.search(line)


def test_check_args_cannot_override_reserved_vault_filters(monkeypatch, tmp_path):
    captured = []
    monkeypatch.setattr(rclone_sync, "_precheck_pair", lambda _: (True, "ok"))
    monkeypatch.setattr(rclone_sync, "_capture_local_endpoint_guards", lambda _: [])
    monkeypatch.setattr(
        rclone_sync,
        "_run_rclone_command",
        lambda command, *args, **kwargs: captured.append(command) or 0,
    )
    monkeypatch.setattr(rclone_sync, "read_log_tail", lambda _: "")
    snapshot = {
        "paths": {"logs_dir": str(tmp_path)},
        "backup": {
            "check_args": "--filter '+ **'",
            "allow_unsafe_rclone_args": True,
            "pairs": [
                {
                    "name": "fixture",
                    "local": str(tmp_path / "source"),
                    "remote": "cloud:target",
                    "direction": "push",
                }
            ],
        },
    }
    assert rclone_sync.run_pair_check("fixture", config_snapshot=snapshot)["ok"]
    command = captured[0]
    assert command.index("- /Sicherpfad/**") < command.index("+ **")


def test_apns_never_sends_names_or_raw_errors_even_from_legacy_rows(monkeypatch):
    monkeypatch.setattr(push_notifications, "push_server_id", lambda: "a" * 64)
    source = {
        "job_id": 7,
        "run_id": "opaque-run",
        "definition_id": "opaque-definition",
        "pair": "private-path-canary",
        "pairs": ["private-path-canary"],
        "definition_name": "private-name-canary",
    }
    context = push_notifications._push_context(source)
    assert set(context) == {"job_id", "run_id", "definition_id", "server_id"}
    payload = push_notifications._payload(
        {
            "event": "sync_error",
            "title": "private-name-canary",
            "message": "raw-error-canary",
            "payload": {**source, "server_id": "a" * 64},
        }
    )
    assert b"canary" not in payload
    body = json.loads(payload)
    assert body["job_id"] == 7 and body["aps"]["alert"]["title"] == "Sicherpfad"


def test_httpx_info_log_does_not_reveal_device_token(caplog):
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    caplog.set_level(logging.INFO)
    token = "device-token-canary"
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200))
    ) as client:
        client.post("https://api.push.apple.com/3/device/" + token)
    assert token not in caplog.text


def test_lost_push_lease_closes_transport_and_never_reports_delivery(monkeypatch):
    closed = threading.Event()

    class Client:
        def post(self, *args, **kwargs):
            assert closed.wait(timeout=2), "transport was not closed after lease loss"
            raise httpx.TransportError("fixture closed")

        def close(self):
            closed.set()

    database = SimpleNamespace(push_outbox_renew_claims=lambda *args, **kwargs: [])
    monkeypatch.setattr(push_notifications, "_CLAIM_HEARTBEAT_INTERVAL_SECONDS", 0.01)
    with pytest.raises(httpx.TransportError, match="Lease"):
        push_notifications._post_with_claim_heartbeat(
            Client(),
            "https://api.push.apple.com/fixture",
            content=b"{}",
            headers={},
            database=database,
            row_id=1,
            claim_owner="fixture",
            lease_seconds=10,
        )
    assert closed.is_set()


def _try_existing_delivery_lock(path, result):
    from app.file_lock import acquire, release

    fd = os.open(path, os.O_RDWR)
    try:
        try:
            acquire(fd, blocking=False)
        except (BlockingIOError, OSError):
            result.put(False)
        else:
            release(fd)
            result.put(True)
    finally:
        os.close(fd)


def test_delivery_fence_is_exclusive_across_processes(tmp_path):
    database = Database(tmp_path / "push.db")
    context = multiprocessing.get_context("spawn")
    result = context.Queue()
    child = context.Process(
        target=_try_existing_delivery_lock,
        args=(str(database.path) + ".push-delivery.lock", result),
    )
    try:
        with push_notifications.delivery_guard(database):
            child.start()
            assert result.get(timeout=10) is False
        child.join(timeout=10)
        assert child.exitcode == 0
    finally:
        if child.is_alive():
            child.terminate()
            child.join(timeout=5)
        result.close()


class _MetadataPath:
    """A fixed path tree; these tests never inspect host directory permissions."""

    def __init__(self, name, metadata):
        self.name = name
        self.metadata = metadata

    def absolute(self):
        return self

    @property
    def parents(self):
        parts = self.name.strip("/").split("/")
        return [
            _MetadataPath("/" + "/".join(parts[:index]), self.metadata)
            for index in range(len(parts) - 1, -1, -1)
        ]

    def lstat(self):
        item = self.metadata.get(self.name)
        if item is None:
            raise FileNotFoundError(self.name)
        if isinstance(item, OSError):
            raise item
        return SimpleNamespace(st_mode=item[0], st_uid=item[1])

    def is_symlink(self):
        return self.name in self.metadata and stat.S_ISLNK(self.lstat().st_mode)

    def __str__(self):
        return self.name


def _guard_metadata(monkeypatch, tree, leaf="/srv/owned/data"):
    # Replacing only the module binding avoids changing pathlib/os globally on
    # Windows while exercising the production POSIX policy.
    monkeypatch.setattr(
        rclone_sync, "os", SimpleNamespace(name="posix", geteuid=lambda: 1000)
    )
    return rclone_sync._local_endpoint_parent_guard(_MetadataPath(leaf, tree))


def test_endpoint_ancestry_accepts_root_and_service_owned_directories(monkeypatch):
    tree = {
        "/": (stat.S_IFDIR | 0o755, 0),
        "/srv": (stat.S_IFDIR | 0o755, 0),
        "/srv/owned": (stat.S_IFDIR | 0o750, 1000),
        "/srv/owned/data": (stat.S_IFDIR | 0o750, 1000),
    }
    assert _guard_metadata(monkeypatch, tree)[0]


@pytest.mark.parametrize(
    "mode,owner",
    [
        (stat.S_IFDIR | 0o755, 2000),
        (stat.S_IFDIR | 0o775, 1000),
        (stat.S_IFDIR | 0o777, 0),
        (stat.S_IFLNK | 0o777, 0),
        (stat.S_IFREG | 0o644, 0),
    ],
)
def test_endpoint_ancestry_rejects_foreign_writable_or_non_directory_parent(
    monkeypatch, mode, owner
):
    tree = {
        "/": (stat.S_IFDIR | 0o755, 0),
        "/srv": (stat.S_IFDIR | 0o755, 0),
        "/srv/owned": (mode, owner),
        "/srv/owned/data": (stat.S_IFDIR | 0o750, 1000),
    }
    trusted, reason = _guard_metadata(monkeypatch, tree)
    assert not trusted and "/srv/owned" in reason


@pytest.mark.parametrize("owner", [0, 1000])
def test_sticky_root_temporary_parent_protects_existing_owned_child(monkeypatch, owner):
    tree = {
        "/": (stat.S_IFDIR | 0o755, 0),
        "/tmp": (stat.S_IFDIR | 0o1777, 0),
        "/tmp/owned": (stat.S_IFDIR | 0o700, owner),
        "/tmp/owned/data": (stat.S_IFDIR | 0o700, 1000),
    }
    assert _guard_metadata(monkeypatch, tree, "/tmp/owned/data")[0]


def test_sticky_parent_rejects_missing_child_before_any_creation(monkeypatch):
    tree = {"/": (stat.S_IFDIR | 0o755, 0), "/tmp": (stat.S_IFDIR | 0o1777, 0)}
    trusted, reason = _guard_metadata(monkeypatch, tree, "/tmp/missing/data")
    assert not trusted and "/tmp" in reason


def test_sticky_service_owned_parent_does_not_substitute_for_root_sticky_policy(
    monkeypatch,
):
    tree = {
        "/": (stat.S_IFDIR | 0o755, 0),
        "/tmp": (stat.S_IFDIR | 0o1777, 1000),
        "/tmp/owned": (stat.S_IFDIR | 0o700, 1000),
    }
    assert not _guard_metadata(monkeypatch, tree, "/tmp/owned")[0]


def test_endpoint_leaf_symlink_is_rejected_even_under_trusted_parent(monkeypatch):
    tree = {
        "/": (stat.S_IFDIR | 0o755, 0),
        "/srv": (stat.S_IFDIR | 0o755, 0),
        "/srv/owned": (stat.S_IFDIR | 0o700, 1000),
        "/srv/owned/data": (stat.S_IFLNK | 0o777, 1000),
    }
    trusted, reason = _guard_metadata(monkeypatch, tree)
    assert not trusted and "symbolischen" in reason
