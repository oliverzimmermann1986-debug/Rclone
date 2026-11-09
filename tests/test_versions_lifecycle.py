import json
import subprocess
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import lockdown
from app import versions_lifecycle as vl
from app.routes import api_versions

STAMPS = [datetime(2026, 6, 1) + timedelta(hours=60 * i) for i in range(60)]


@pytest.fixture(autouse=True)
def _no_lockdown(monkeypatch):
    monkeypatch.setattr(lockdown, "is_locked_down", lambda *_: False)


def test_select_gfs_buckets_over_five_months():
    extra = datetime(2026, 10, 26, 8)  # zweiter Lauf am Tag der neuesten Version
    plan = vl.select_gfs(STAMPS + [extra], keep_daily=7, keep_weekly=4, keep_monthly=2)
    days = [extra] + [
        datetime(2026, 10, d, h)
        for d, h in ((24, 0), (21, 12), (19, 0), (16, 12), (14, 0), (11, 12))
    ]
    # KW41 (ab Mo 05.10.) hat Reste vor dem ältesten Tages-Bucket, dann KW40..38.
    weeks = [
        datetime(2026, 10, 6, 12),
        datetime(2026, 9, 29),
        datetime(2026, 9, 21, 12),
        datetime(2026, 9, 14),
    ]
    months = [datetime(2026, 9, 1, 12), datetime(2026, 8, 2, 12)]
    assert plan["keep"] == set(days + weeks + months)  # je Bucket der älteste
    assert plan["merge"][extra] == [datetime(2026, 10, 26, 12)]
    assert plan["merge"][datetime(2026, 9, 29)] == [
        datetime(2026, 10, 1, 12),
        datetime(2026, 10, 4),
    ]
    assert plan["purge"] == sorted(STAMPS[:25], reverse=True)
    assert max(plan["purge"]) < min(plan["keep"])
    merged = [stamp for younger in plan["merge"].values() for stamp in younger]
    assert len(plan["keep"]) + len(merged) + len(plan["purge"]) == 61
    assert plan["keep"] | set(merged) | set(plan["purge"]) == set(STAMPS + [extra])


def test_select_gfs_zero_and_empty():
    assert vl.select_gfs(STAMPS, keep_daily=0, keep_weekly=0, keep_monthly=0) == {
        "keep": set(),
        "merge": {},
        "purge": sorted(STAMPS, reverse=True),
    }
    assert vl.select_gfs([], keep_daily=7, keep_weekly=4, keep_monthly=12) == {
        "keep": set(),
        "merge": {},
        "purge": [],
    }


PUSH = {"direction": "push", "remote": "cloud:bucket/data", "local": "/srv/data"}


@pytest.mark.parametrize(
    ("backup", "pair", "roots", "warned"),
    [
        (
            {},
            {**PUSH, "backup_dir": ".versions/{date}"},
            ["cloud:bucket/data/.versions"],
            False,
        ),
        (
            {},
            {**PUSH, "direction": "pull", "backup_dir": "/srv/versions/{date}/"},
            ["/srv/versions"],
            False,
        ),
        # Globale absolute Wurzel teilen sich alle Pairs: nie automatisch ausdünnen.
        (
            {"backup_dir": "/srv/versions/{date}/"},
            {**PUSH, "direction": "pull"},
            [],
            True,
        ),
        ({"backup_dir": "b2:versions/{date}"}, PUSH, [], True),
        (
            {"backup_dir": ".v/{date}", "backup_dir1": "b2:v1/{date}"},
            {"remote": "cloud:data", "local": "/srv/data"},
            ["/srv/data/.v"],
            True,
        ),
        ({}, {**PUSH, "backup_dir": "archive:alt/{date}"}, ["archive:alt"], False),
        (
            {},
            {**PUSH, "remote": "cloud:", "backup_dir": "v/{date}"},
            ["cloud:v"],
            False,
        ),
        ({}, {**PUSH, "backup_dir": ".versions"}, [], False),
        ({}, {**PUSH, "backup_dir": "v/{date}-alt"}, [], True),
        ({}, {**PUSH, "backup_dir": "{date}/x"}, [], True),
        ({}, {**PUSH, "backup_dir": "{date}"}, [], True),
        (
            {"backup_dir": ".v/{date}"},
            {"remote": "cloud:data", "local": "/srv/data"},
            ["cloud:data/.v", "/srv/data/.v"],
            False,
        ),
        (
            {"backup_dir": "/abs/{date}"},
            {"remote": "cloud:data", "local": "/srv/data", "backup_dir2": "/l/{date}"},
            ["/l"],
            False,
        ),
    ],
)
def test_version_roots(backup, pair, roots, warned):
    warnings: list[str] = []
    assert vl.version_roots(backup, pair, warnings=warnings) == roots
    assert bool(warnings) is warned


def _listing(*names):
    return json.dumps([{"Name": name, "IsDir": True} for name in names])


def test_list_versions_filters_foreign_and_invalid_names():
    calls = []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(
            cmd,
            0,
            _listing(
                "2026-10-01T10-00-00",
                "Fotos",
                "2026-10-01T10-00-00-alt",
                "2026-02-30T10-00-00",
                "２０２６-10-01T10-00-00",
            ),
            "",
        )

    assert vl.list_versions("cloud:v", run=run) == [
        {"name": "2026-10-01T10-00-00", "stamp": datetime(2026, 10, 1, 10)}
    ]
    cmd, kwargs = calls[0]
    assert cmd == [
        "rclone",
        "lsjson",
        "--dirs-only",
        "--no-mimetype",
        "--no-modtime",
        "--",
        "cloud:v",
    ]
    assert kwargs["timeout"] == 120 and kwargs["stdin"] is subprocess.DEVNULL


@pytest.mark.parametrize(
    ("rc", "stderr"), [(3, ""), (1, "error listing: directory not found")]
)
def test_list_versions_missing_root_is_empty(rc, stderr):
    assert (
        vl.list_versions(
            "x:", run=lambda cmd, **_: subprocess.CompletedProcess(cmd, rc, "", stderr)
        )
        == []
    )
    with pytest.raises(RuntimeError):
        vl.list_versions(
            "x:",
            run=lambda cmd, **_: subprocess.CompletedProcess(cmd, 1, "", "auth failed"),
        )


NAMES = [f"2026-10-{day:02d}T03-00-00" for day in range(1, 11)]
ENABLED = {"enabled": True, "keep_daily": 3, "keep_weekly": 0, "keep_monthly": 0}
OLDER, NEWER = "2026-10-26T08-00-00", "2026-10-26T12-00-00"
BASE = "cloud:bucket/data/.versions/"


def _fake(tree, fail=(), sticky=()):
    """Mini-rclone über {Ordner: {Dateien}}; move --ignore-existing lässt Dubletten liegen.

    ``sticky``: Dateien, die delete nicht entfernt (Rückfallprüfung).
    """
    calls = []

    def run(cmd, **_):
        calls.append(cmd)
        if cmd[1] == "lsjson":
            return subprocess.CompletedProcess(cmd, 0, _listing(*tree, "Fotos"), "")
        src, *dst = (arg.rsplit("/", 1)[-1] for arg in cmd[cmd.index("--") + 1 :])
        if src in fail:
            return subprocess.CompletedProcess(cmd, 1, "", "kaputt")
        if cmd[1] == "move":
            tree[src], tree[dst[0]] = tree[src] & tree[dst[0]], tree[src] | tree[dst[0]]
        elif cmd[1] == "delete":
            tree[src] &= set(sticky)
        elif cmd[1] == "purge" or not tree[src]:
            del tree[src]
        return subprocess.CompletedProcess(cmd, 0, "", "")

    return run, calls


def _cfg(lifecycle):
    return {
        "backup": {"backup_dir": ".versions/{date}", "versions_lifecycle": lifecycle}
    }


def _ops(calls):
    return [cmd[1:] for cmd in calls if cmd[1] != "lsjson"]


def test_same_day_versions_are_merged_never_purged():
    """F01: Der ältere Ordner des Tages hält die Originale vor der Verschlüsselung."""
    tree = {OLDER: {"vertrag.docx"}, NEWER: {"bilanz.xlsx"}}
    run, calls = _fake(tree)
    root = vl.apply_lifecycle(_cfg(ENABLED), PUSH, dry_run=False, run=run)["roots"][0]
    assert _ops(calls) == [
        ["move", "--ignore-existing", "--", BASE + NEWER, BASE + OLDER],
        ["delete", "--", BASE + NEWER],
        ["rmdirs", "--", BASE + NEWER],
    ]
    assert tree == {OLDER: {"vertrag.docx", "bilanz.xlsx"}}
    assert root["merged"] == [{"into": OLDER, "from": NEWER, "ok": True, "error": None}]
    assert root["keep"] == [OLDER] and root["purge"] == root["failed"] == []


def test_duplicate_path_keeps_older_version_and_drops_newer_copy():
    tree = {OLDER: {"a"}, NEWER: {"a", "b"}}
    run, calls = _fake(tree)
    root = vl.apply_lifecycle(_cfg(ENABLED), PUSH, dry_run=False, run=run)["roots"][0]
    assert _ops(calls) == [
        ["move", "--ignore-existing", "--", BASE + NEWER, BASE + OLDER],
        ["delete", "--", BASE + NEWER],
        ["rmdirs", "--", BASE + NEWER],
    ]
    assert tree == {OLDER: {"a", "b"}} and root["failed"] == []
    assert root["merged"] == [{"into": OLDER, "from": NEWER, "ok": True, "error": None}]


@pytest.mark.parametrize(
    ("fail", "sticky", "ops", "error"),
    [
        ({NEWER}, (), ["move"], "rclone move exit=1: kaputt"),
        ((), {"a"}, ["move", "delete", "rmdirs"], "Ordner nach move nicht leer"),
    ],
)
def test_unfinished_merge_keeps_folder_and_reports_failure(fail, sticky, ops, error):
    tree = {OLDER: {"a"}, NEWER: {"a", "b"}}
    run, calls = _fake(tree, fail=fail, sticky=sticky)
    root = vl.apply_lifecycle(_cfg(ENABLED), PUSH, dry_run=False, run=run)["roots"][0]
    assert NEWER in tree and [op[0] for op in _ops(calls)] == ops
    assert root["merged"] == [
        {"into": OLDER, "from": NEWER, "ok": False, "error": error}
    ]
    assert root["failed"] == [{"name": NEWER, "error": error}]


def test_apply_lifecycle_disabled_and_dry_run():
    run, calls = _fake({name: set() for name in NAMES} | {OLDER: {"a"}, NEWER: {"b"}})
    assert vl.apply_lifecycle(_cfg({}), PUSH, run=run) == {
        "skipped": True,
        "reason": "disabled",
    }
    # Pair-Override schlägt die globale Einstellung.
    result = vl.apply_lifecycle(
        _cfg({}), {**PUSH, "versions_lifecycle": ENABLED}, run=run
    )
    assert [cmd[1] for cmd in calls] == ["lsjson"]
    root = result["roots"][0]
    assert result["dry_run"] is True and root["root"] == "cloud:bucket/data/.versions"
    assert root["total"] == 12 and root["keep"] == [*NAMES[-2:], OLDER]
    assert root["merge"] == [{"into": OLDER, "from": NEWER}] and root["merged"] == []
    assert root["purge"] == NAMES[-3::-1] and root["purged"] == []


def test_apply_lifecycle_collects_partial_failures():
    run, calls = _fake({name: set() for name in NAMES}, fail={NAMES[0]})
    root = vl.apply_lifecycle(_cfg(ENABLED), PUSH, dry_run=False, run=run)["roots"][0]
    purges = [cmd for cmd in calls if cmd[1] == "purge"]
    assert len(purges) == 7
    assert purges[0][:3] == ["rclone", "purge", "--"]
    assert all(cmd[3].startswith(BASE + "2026-10-") for cmd in purges)
    assert sorted(root["purged"]) == NAMES[1:7]
    assert root["failed"] == [
        {"name": NAMES[0], "error": "rclone purge exit=1: kaputt"}
    ]


def test_keep_zero_refuses_to_delete_everything():
    run, calls = _fake({name: set() for name in NAMES})
    zero = {"enabled": True, "keep_daily": 0, "keep_weekly": 0, "keep_monthly": 0}
    root = vl.apply_lifecycle(_cfg(zero), PUSH, dry_run=False, run=run)["roots"][0]
    assert root["error"] == "Schutz: Lifecycle würde alle Versionen löschen"
    assert root["purged"] == [] and [cmd[1] for cmd in calls] == ["lsjson"]


@pytest.mark.parametrize(("states", "ops"), [([True], []), ([False, True], ["move"])])
def test_lockdown_aborts_before_next_step(monkeypatch, states, ops):
    answers = iter(states)
    monkeypatch.setattr(lockdown, "is_locked_down", lambda *_: next(answers))
    tree = {name: set() for name in NAMES} | {"2026-10-10T09-00-00": {"b"}}
    run, calls = _fake(tree)
    result = vl.apply_lifecycle(_cfg(ENABLED), PUSH, dry_run=False, run=run)
    assert [op[0] for op in _ops(calls)] == ops
    assert result["aborted"] == "lockdown" and result["warnings"]
    assert result["roots"][0]["purged"] == [] and len(tree) == 11


class _Db:
    def __init__(self, running):
        self.running, self.audits = running, []

    def job_running(self, kind):
        return self.running if kind == "backup" else None

    def audit_add(self, event, *, actor, details):
        self.audits.append((event, actor, details))


class _Config:
    def snapshot(self):
        return {
            "backup": {**_cfg(ENABLED)["backup"], "pairs": [{**PUSH, "name": "Fotos"}]}
        }


def test_shared_global_root_is_never_touched():
    calls = []
    cfg = {
        "backup": {"backup_dir": "b2:versions/{date}", "versions_lifecycle": ENABLED}
    }

    result = vl.apply_lifecycle(cfg, PUSH, dry_run=False, run=calls.append)

    assert result["roots"] == [] and calls == []
    assert "geteilte Versionswurzel" in result["warnings"][0]


def test_prune_route_409_while_backup_lock_held(monkeypatch):
    db = _Db(None)
    scopes = []
    monkeypatch.setattr(api_versions, "get_db", lambda: db)
    monkeypatch.setattr(api_versions, "get_config", _Config)
    monkeypatch.setattr(api_versions, "require_reauthentication", lambda *_: "gen")
    monkeypatch.setattr(api_versions, "is_locked_down", lambda: False)
    monkeypatch.setattr(api_versions, "try_file_lock", scopes.append)  # -> None
    monkeypatch.setattr(
        vl, "apply_lifecycle", lambda *_a, **_k: pytest.fail("ohne Lock")
    )
    body = api_versions.PruneBody(pair="Fotos", password="pw")

    with pytest.raises(HTTPException) as exc:
        api_versions.prune_versions(body, request=None, user="admin")

    assert exc.value.status_code == 409 and db.audits == []
    assert scopes == ["backup"]


def test_prune_route_reauth_conflict_and_audit(monkeypatch):
    db = _Db({"id": 1})
    releases = []
    monkeypatch.setattr(
        api_versions,
        "try_file_lock",
        lambda _scope: SimpleNamespace(release=lambda: releases.append(1)),
    )
    monkeypatch.setattr(api_versions, "get_db", lambda: db)
    monkeypatch.setattr(api_versions, "get_config", _Config)
    monkeypatch.setattr(api_versions, "require_reauthentication", lambda *_: "gen")
    monkeypatch.setattr(api_versions, "is_locked_down", lambda: False)
    run, _ = _fake({name: set() for name in NAMES}, fail={NAMES[0]})
    real_apply = vl.apply_lifecycle
    monkeypatch.setattr(
        vl, "apply_lifecycle", lambda *a, **k: real_apply(*a, run=run, **k)
    )
    body = api_versions.PruneBody(pair="Fotos", password="pw")

    with pytest.raises(HTTPException) as exc:
        api_versions.prune_versions(body, request=None, user="admin")
    assert exc.value.status_code == 409 and db.audits == []
    with pytest.raises(HTTPException) as exc:
        api_versions.preview_versions(pair="fehlt")
    assert exc.value.status_code == 404

    db.running = None
    api_versions.prune_versions(body, request=None, user="admin")
    assert db.audits == [
        ("versions_pruned", "admin", {"pair": "Fotos", "purged": 6, "failed": 1})
    ]
    assert len(releases) == 2
