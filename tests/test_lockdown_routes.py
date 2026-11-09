"""Lockdown sperrt auch die neuen Start- und Schreibrouten (HTTP 423)."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.routes import (
    api_bitrot,
    api_boot_drill,
    api_conflicts,
    api_jobs,
    api_versions,
)


def _locked(monkeypatch, module):
    monkeypatch.setattr(module, "is_locked_down", lambda *_a, **_k: True)


@pytest.mark.parametrize(
    ("module", "call"),
    [
        (api_boot_drill, lambda: api_boot_drill.boot_drill_run()),
        (api_bitrot, lambda: api_bitrot.start_bitrot_scrub(None, user="admin")),
        (
            api_conflicts,
            lambda: api_conflicts.resolve_pair_conflict(
                "Fotos",
                api_conflicts.ResolveBody(base_path="a.txt", choice="keep_both"),
                None,
                user="admin",
            ),
        ),
        (
            api_versions,
            lambda: api_versions.prune_versions(
                api_versions.PruneBody(pair="Fotos", password="x" * 8),
                None,
                user="admin",
            ),
        ),
        (
            api_jobs,
            lambda: api_jobs.pause_scheduler_endpoint(
                api_jobs.SchedulerPausePayload(), user="admin"
            ),
        ),
        (api_jobs, lambda: api_jobs.resume_scheduler_endpoint(user="admin")),
    ],
    ids=["bootdrill", "bitrot", "conflicts", "versions", "pause", "resume"],
)
def test_routes_reject_with_423_during_lockdown(monkeypatch, module, call):
    _locked(monkeypatch, module)
    with pytest.raises(HTTPException) as info:
        call()
    assert info.value.status_code == 423
