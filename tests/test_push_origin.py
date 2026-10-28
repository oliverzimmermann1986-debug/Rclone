from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from app import config_store, db, push_notifications
from app.config_store import Config
from app.config_validation import SESSION_SECRET_PLACEHOLDER
from app.db import Database
from app.routes import api_push


TOKEN = "ab" * 32
SECRET_A = "push-origin-secret-a-0123456789-ABCDEFGHIJKLMN"
SECRET_B = "push-origin-secret-b-ZYXWVUTSRQPONMLK-9876543210"


@pytest.fixture
def origin_server(tmp_path: Path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump({"web": {"secret_key": SECRET_A, "session_version": 1}}),
        encoding="utf-8",
    )
    store = Config(path)
    database = Database(tmp_path / "push.db")
    monkeypatch.setattr(config_store, "_config", store)
    monkeypatch.setattr(db, "_db", database)
    return store, database


def test_registration_and_queued_incident_use_same_server_owned_identity(
    origin_server, monkeypatch
):
    _store, database = origin_server
    registered = api_push.register_push_device(
        api_push.PushDeviceRegistration(token=TOKEN), user="admin"
    )
    server_id = registered["server_id"]
    assert registered["ok"] is True
    assert isinstance(server_id, str)
    assert len(server_id) == 64
    assert server_id != SECRET_A
    monkeypatch.setattr(
        push_notifications, "_settings", lambda _event: {"retention_seconds": 60}
    )
    monkeypatch.setattr(
        push_notifications,
        "dispatch_pending_pushes",
        lambda **_kwargs: {"sent": 0, "failed": 0, "removed": 0, "retrying": 0},
    )

    result = push_notifications.send_push_notifications(
        "sync_error",
        "Dummy incident",
        "Dummy message",
        extra={"server_id": "attacker-selected", "summary": {"job_id": 7}},
        dedupe_key="origin-fixture",
        db=database,
    )

    assert result["queued"] == 1
    rows = database.push_outbox_claim_due(claim_owner="origin-test", limit=1)
    assert len(rows) == 1
    assert rows[0]["payload"]["server_id"] == server_id
    payload = json.loads(push_notifications._payload(rows[0]))
    assert payload["server_id"] == server_id
    assert payload["job_id"] == 7


def test_payload_keeps_queue_time_identity_after_rotation_and_size_truncation(
    origin_server,
):
    store, _database = origin_server
    context = push_notifications._push_context({"pairs": ["😀" * 500] * 20})
    queued_server_id = context["server_id"]
    store.update(lambda data: data["web"].update({"secret_key": SECRET_B}))
    assert push_notifications.push_server_id() != queued_server_id

    body = push_notifications._payload(
        {
            "event": "sync_error",
            "title": "😀" * 120,
            "message": "😀" * 900,
            "payload": context,
        }
    )

    assert len(body) <= 4_096
    payload = json.loads(body)
    assert payload["server_id"] == queued_server_id
    assert payload["aps"]["category"] == "RCLONE_INCIDENT"
    assert "pairs" not in payload


@pytest.mark.parametrize("identity", [None, "", "not-a-server", "a" * 65, "G" * 64])
def test_old_or_malformed_notification_identity_does_not_gain_a_new_binding(identity):
    payload = json.loads(
        push_notifications._payload(
            {
                "event": "sync_error",
                "title": "Dummy incident",
                "message": "Still displayed",
                "payload": {"server_id": identity},
            }
        )
    )
    assert payload["server_id"] is None
    assert payload["aps"]["alert"]["body"] == "Still displayed"


def test_caller_and_summary_cannot_spoof_notification_origin(origin_server):
    context = push_notifications._push_context(
        {
            "server_id": "a" * 64,
            "summary": {"server_id": "b" * 64, "job_id": 42},
        }
    )
    assert context["server_id"] == push_notifications.push_server_id()
    assert context["server_id"] not in {"a" * 64, "b" * 64}
    assert context["job_id"] == 42


def test_installation_identity_is_stable_but_changes_with_secret_rotation(
    origin_server,
):
    store, _database = origin_server
    first = push_notifications.push_server_id()
    store.update(lambda data: data["web"].update({"session_version": 2}))
    assert push_notifications.push_server_id() == first
    store.update(lambda data: data["web"].update({"secret_key": SECRET_B}))
    assert push_notifications.push_server_id() != first


@pytest.mark.parametrize("secret", ["", SESSION_SECRET_PLACEHOLDER, "too-short"])
def test_uninitialized_secrets_cannot_create_a_predictable_origin(
    origin_server, secret
):
    store, _database = origin_server
    store.update(lambda data: data["web"].update({"secret_key": secret}))
    assert push_notifications.push_server_id() is None
