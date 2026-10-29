from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import require_auth
from app.routes import api_jobs
from app.security import require_csrf


def test_http_cleanup_audit_uses_authenticated_account(monkeypatch):
    events = []
    database = SimpleNamespace(
        jobs_delete_failed=lambda: 3,
        audit_add=lambda event, **fields: events.append((event, fields)),
    )
    monkeypatch.setattr(api_jobs, "get_db", lambda: database)
    app = FastAPI()
    app.include_router(api_jobs.router)
    app.dependency_overrides[require_auth] = lambda: "signed-in-admin"
    app.dependency_overrides[require_csrf] = lambda: None
    response = TestClient(app).post("/api/jobs/cleanup-failed")
    assert response.status_code == 200
    assert events == [
        ("failed_jobs_cleaned", {"actor": "signed-in-admin", "details": {"deleted": 3}})
    ]


def test_request_user_reaches_both_queue_entrypoints(monkeypatch):
    calls = []
    monkeypatch.setattr(api_jobs, "_known_pair_names", lambda: {"Fotos"})
    monkeypatch.setattr(
        api_jobs,
        "_queue_enabled_job_definitions",
        lambda **fields: calls.append(fields) or {"ok": True},
    )
    monkeypatch.setattr(
        api_jobs, "_queue_backup", lambda **fields: calls.append(fields) or {"ok": True}
    )
    api_jobs.run_backup(dry_run=True, pairs=None, user="admin-a")
    api_jobs.run_backup(dry_run=True, pairs="Fotos", user="admin-b")
    assert calls[0] == {"dry_run": True, "actor": "admin-a"}
    assert calls[1] == {"dry_run": True, "pairs_filter": ["Fotos"], "actor": "admin-b"}


@pytest.mark.parametrize(
    "original_actor,expected", [("original-admin", "original-admin"), (None, "system")]
)
def test_resumed_batch_uses_persisted_initiator_not_current_account(
    monkeypatch, original_actor, expected
):
    events = []
    spec = {"definition": {"id": "backup", "name": "Fotos"}}
    if original_actor is not None:
        spec["actor"] = original_actor
    batch = {
        "dry_run": True,
        "snapshot": {},
        "config_revision": "revision",
        "cancel_requested": True,
        "items": [{"position": 0, "state": "queued", "job_id": None, "spec": spec}],
    }
    database = SimpleNamespace(
        job_batch_get=lambda identifier: batch,
        job_batch_item_finish=lambda *args, **kwargs: None,
        audit_add=lambda event, **fields: events.append((event, fields)),
    )
    lock = SimpleNamespace(release=lambda: None)
    monkeypatch.setattr(api_jobs, "get_db", lambda: database)
    monkeypatch.setattr(api_jobs, "_locks", {"backup": lock})
    api_jobs._run_definition_batch_thread("persisted-batch", lock)
    assert len(events) == 1
    assert events[0][1]["actor"] == expected
    assert events[0][1]["details"]["error_code"] == "batch_cancelled"
