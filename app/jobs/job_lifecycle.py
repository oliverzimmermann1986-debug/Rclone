"""Scope-gebundene Job-Reservation und Orphan-Recovery.

Alle Funktionen in diesem Modul müssen aufgerufen werden, während der passende
prozessübergreifende Lock gehalten wird. Der Lock ist die Autorität dafür, dass
kein neuer Worker parallel zur Recovery starten kann.
"""

from __future__ import annotations

from typing import Any, Iterable

from . import runtime_state

BACKUP_KINDS = ("backup", "check", "quicksync", "restoretest")
PBS_KINDS = ("pbs",)


def reconcile_locked_scope(
    db,
    *,
    scope: str,
    kinds: Iterable[str],
    reason: str = "Prozess-Lock frei; kein aktiver Worker vorhanden",
) -> dict[str, Any]:
    """Bereinigt verwaiste Zustände fail-closed unter gehaltenem Scope-Lock."""

    active = runtime_state.active_processes(scope)
    if active:
        return {
            "safe": False,
            "recovered_jobs": 0,
            "active_processes": len(active),
        }

    recovered_runtime = False
    if scope == runtime_state.DEFAULT_CANCEL_SCOPE:
        recovered_runtime = (
            runtime_state.recover_stale_run_details(reason=reason, force=True)
            is not None
        )
    recovered_jobs = db.jobs_mark_all_running_stale(
        kinds=tuple(kinds),
        reason=reason,
    )
    return {
        "safe": True,
        "recovered_jobs": recovered_jobs,
        "recovered_runtime": recovered_runtime,
        "active_processes": 0,
    }
