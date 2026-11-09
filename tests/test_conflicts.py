import json
import subprocess

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import conflicts
from app.auth import require_auth
from app.routes import api_conflicts
from app.security import CSRF_COOKIE

PAIR = {"name": "docs", "remote": "gd:", "local": "/data", "direction": "bisync"}
LIST1 = ["rclone", "lsjson", "-R", "--files-only", "--no-mimetype"]
LIST1 += ["--include", "*.conflict*", "--", "gd:"]


def item(path, modtime="2026-10-01T10:00:00Z", size=1):
    return {
        "Path": path,
        "Name": path.rpartition("/")[2],
        "Size": size,
        "ModTime": modtime,
    }


class FakeRclone:
    """Antwortet auf exakte Argumentlisten; unbekanntes lsjson liefert []."""

    def __init__(self, listings=None, fail=None):
        self.listings = listings or {}
        self.fail = fail or {}
        self.calls = []

    def __call__(self, cmd, **kwargs):
        assert kwargs["stdin"] is subprocess.DEVNULL and kwargs["timeout"]
        self.calls.append(cmd)
        key = tuple(cmd)
        if key in self.fail:
            return subprocess.CompletedProcess(cmd, 1, "", self.fail[key])
        stdout = json.dumps(self.listings.get(key, [])) if cmd[1] == "lsjson" else ""
        return subprocess.CompletedProcess(cmd, 0, stdout, "")

    def mutations(self):
        return [cmd for cmd in self.calls if cmd[1] != "lsjson"]


def ls_dir(path):
    return ("rclone", "lsjson", "--files-only", "--no-mimetype", "--", path)


def test_list_groups_by_regex_with_live_files_and_skips_invalid_paths():
    fake = FakeRclone(
        {
            tuple(LIST1): [
                item("a.conflict2.txt"),
                item("a.conflict1.txt"),
                item("docs/b.conflict1"),
                item("docs/sub/c.tar.conflict3.gz"),
                item("x.txt.conflict1"),
                item("notes.conflicted.md"),
                item("../evil.conflict1.txt"),
                item("bad\tname.conflict1.txt"),
            ],
            ("rclone", "lsjson", *LIST1[2:-1], "/data"): [item("a.conflict2.txt")],
            ls_dir("gd:"): [item("a.txt", size=7)],
            ls_dir("/data/docs"): [item("b", size=3)],
        }
    )
    result = conflicts.list_conflicts(PAIR, run=fake)

    assert fake.calls[0] == LIST1
    assert [g["base_path"] for g in result["groups"]] == [
        "a.txt",
        "docs/b",
        "docs/sub/c.tar.gz",
        "x.txt",
    ]
    a, b, c, x = result["groups"]
    assert [e["conflict_n"] for e in a["side1"]] == [1, 2]
    assert [e["name"] for e in a["side2"]] == ["a.conflict2.txt"]
    assert a["live"]["side1"] == {"size": 7, "modtime": "2026-10-01T10:00:00Z"}
    assert a["live"]["side2"] is None
    assert b["live"] == {
        "side1": None,
        "side2": {"size": 3, "modtime": "2026-10-01T10:00:00Z"},
    }
    assert c["side1"][0]["conflict_n"] == 3
    assert result["skipped"] == 2 and result["truncated"] is False
    # Separator: Remote "gd:" ohne Slash, lokal mit; je Verzeichnis nur ein Aufruf.
    assert list(ls_dir("gd:docs")) in fake.calls
    assert list(ls_dir("/data/docs/sub")) in fake.calls
    assert len(fake.calls) == 2 + 2 * 3


def test_list_truncates_and_skips_live_lookup_for_many_groups():
    fake = FakeRclone({tuple(LIST1): [item(f"f{i}.conflict1.txt") for i in range(501)]})
    result = conflicts.list_conflicts(PAIR, run=fake)
    assert result["truncated"] is True and len(result["groups"]) == 500
    assert all(group["live"] is None for group in result["groups"])
    assert len(fake.calls) == 2


@pytest.mark.parametrize(
    ("path", "ok"),
    [
        ("a/b.txt", True),
        ("a..b/c", True),
        ("", False),
        ("/etc/passwd", False),
        ("../x", False),
        ("a/../b", False),
        ("a\x00b", False),
        ("a\nb", False),
        ("a\x7fb", False),
    ],
)
def test_valid_rel_path(path, ok):
    assert conflicts.valid_rel_path(path) is ok


def test_resolve_rejects_invalid_path_without_calling_rclone():
    fake = FakeRclone()
    with pytest.raises(ValueError):
        conflicts.resolve_conflict(PAIR, "../a.txt", "keep_both", run=fake)
    assert fake.calls == []


def copies_both_sides(*names):
    """Wie bisync: jede Konfliktkopie liegt auf beiden Seiten, das Original fehlt."""
    return {
        ls_dir("gd:docs"): [item(name) for name in names]
        + [item("other.conflict1.txt")],
        ls_dir("/data/docs"): [item(name) for name in names],
    }


NEW = ("a.txt.conflict-path1-1", "a.txt.conflict-path2-1")
LEGACY = ("a.conflict1.txt", "a.conflict2.txt")


def test_keep_side1_moves_path1_copy_and_deletes_the_rest():
    listings = copies_both_sides(*NEW)
    # Die path2-Kopie ist jünger: die Seite entscheidet, nicht die ModTime.
    listings[ls_dir("gd:docs")][1]["ModTime"] = "2026-10-02T10:00:00Z"
    fake = FakeRclone(listings)
    result = conflicts.resolve_conflict(PAIR, "docs/a.txt", "keep_side1", run=fake)
    assert result["completed"] is True
    assert fake.mutations() == [
        ["rclone", "moveto", "--", "gd:docs/a.txt.conflict-path1-1", "gd:docs/a.txt"],
        ["rclone", "deletefile", "--", "gd:docs/a.txt.conflict-path2-1"],
        ["rclone", "deletefile", "--", "/data/docs/a.txt.conflict-path1-1"],
        ["rclone", "deletefile", "--", "/data/docs/a.txt.conflict-path2-1"],
    ]
    assert [step["cmd"] for step in result["steps"]] == fake.mutations()


def test_keep_side2_with_legacy_numbers_moves_conflict2():
    fake = FakeRclone(copies_both_sides(*LEGACY))
    conflicts.resolve_conflict(PAIR, "docs/a.txt", "keep_side2", run=fake)
    assert fake.mutations() == [
        ["rclone", "moveto", "--", "/data/docs/a.conflict2.txt", "/data/docs/a.txt"],
        ["rclone", "deletefile", "--", "gd:docs/a.conflict1.txt"],
        ["rclone", "deletefile", "--", "gd:docs/a.conflict2.txt"],
        ["rclone", "deletefile", "--", "/data/docs/a.conflict1.txt"],
    ]


def test_keep_side_uses_copy_from_other_side_when_missing_on_chosen_side():
    listings = copies_both_sides(*NEW)
    listings[ls_dir("/data/docs")] = [item(NEW[0])]
    fake = FakeRclone(listings)
    conflicts.resolve_conflict(PAIR, "docs/a.txt", "keep_side2", run=fake)
    assert fake.mutations() == [
        [
            "rclone",
            "moveto",
            "--",
            "gd:docs/a.txt.conflict-path2-1",
            "/data/docs/a.txt",
        ],
        ["rclone", "deletefile", "--", "gd:docs/a.txt.conflict-path1-1"],
        ["rclone", "deletefile", "--", "/data/docs/a.txt.conflict-path1-1"],
    ]


@pytest.mark.parametrize(
    ("names", "extra"),
    [
        (LEGACY + ("a.conflict3.txt",), []),  # Folgekonflikt: Nummer ohne Seite
        (LEGACY, [item("a.txt")]),  # Original vorhanden: es gab einen Gewinner
        (NEW + ("a.txt.conflict-path1-2",), []),  # zwei Fassungen von Seite 1
    ],
)
def test_side_choice_without_unique_origin_is_refused(names, extra):
    listings = copies_both_sides(*names)
    listings[ls_dir("gd:docs")] += extra
    fake = FakeRclone(listings)
    with pytest.raises(conflicts.OriginUnclear, match="Beide behalten"):
        conflicts.resolve_conflict(PAIR, "docs/a.txt", "keep_side1", run=fake)
    assert fake.mutations() == []


def test_list_reports_origin_and_side_resolvable():
    names = ["n.txt.conflict-path1-1", "n.txt.conflict-path2-1"]
    names += ["old.conflict1.txt", "old.conflict2.txt"]
    fake = FakeRclone(
        {
            tuple(LIST1): [item(name) for name in names],
            ("rclone", "lsjson", *LIST1[2:-1], "/data"): [item(n) for n in names],
            ls_dir("gd:"): [item("old.txt")],
        }
    )
    n, old = conflicts.list_conflicts(PAIR, run=fake)["groups"]
    assert [entry["origin"] for entry in n["side2"]] == ["side1", "side2"]
    assert n["side_resolvable"] is True
    assert [entry["origin"] for entry in old["side1"]] == [None, None]
    assert old["side_resolvable"] is False


def test_keep_both_renames_on_both_sides_without_overwriting():
    listings = copies_both_sides(*LEGACY)
    listings[ls_dir("gd:docs")].append(item("a (Konflikt Seite1 2026-10-01).txt"))
    fake = FakeRclone(listings)
    result = conflicts.resolve_conflict(PAIR, "docs/a.txt", "keep_both", run=fake)
    assert result["completed"] is True
    gd, data = "gd:docs/a", "/data/docs/a"
    assert fake.mutations() == [
        [
            "rclone",
            "moveto",
            "--",
            f"{gd}.conflict1.txt",
            f"{gd} (Konflikt Seite1 2026-10-01 2).txt",
        ],
        [
            "rclone",
            "moveto",
            "--",
            f"{gd}.conflict2.txt",
            f"{gd} (Konflikt Seite1 2026-10-01 3).txt",
        ],
        [
            "rclone",
            "moveto",
            "--",
            f"{data}.conflict1.txt",
            f"{data} (Konflikt Seite2 2026-10-01).txt",
        ],
        [
            "rclone",
            "moveto",
            "--",
            f"{data}.conflict2.txt",
            f"{data} (Konflikt Seite2 2026-10-01 2).txt",
        ],
    ]


def test_resolve_root_level_path_on_local_root_with_trailing_slash():
    pair = {**PAIR, "remote": "gd:Ordner", "local": "/data/"}
    fake = FakeRclone(
        {
            ls_dir("gd:Ordner"): [item("a.conflict1")],
            ls_dir("/data/"): [item("a.conflict2")],
        }
    )
    conflicts.resolve_conflict(pair, "a", "keep_side1", run=fake)
    assert fake.mutations() == [
        ["rclone", "moveto", "--", "gd:Ordner/a.conflict1", "gd:Ordner/a"],
        ["rclone", "deletefile", "--", "/data/a.conflict2"],
    ]


def test_resolve_stops_at_first_error_and_reports_done_steps():
    failing = ("rclone", "deletefile", "--", "gd:docs/a.txt.conflict-path2-1")
    fake = FakeRclone(
        copies_both_sides(*NEW), fail={failing: "ERROR : boom --password=geheim"}
    )
    result = conflicts.resolve_conflict(PAIR, "docs/a.txt", "keep_side1", run=fake)
    assert result["completed"] is False
    assert [step["ok"] for step in result["steps"]] == [True, False]
    assert "boom" in result["steps"][1]["error"]
    assert "geheim" not in result["steps"][1]["error"]
    assert len(fake.mutations()) == 2


class FakeDb:
    def __init__(self, running=None):
        self.running = running
        self.audits = []

    def job_running(self, kind):
        assert kind == "backup"
        return self.running

    def audit_add(self, event, actor=None, details=None):
        self.audits.append((event, details))


class FakeLease:
    released = False

    def release(self):
        self.released = True


@pytest.fixture
def api(monkeypatch):
    pairs = [PAIR, {**PAIR, "name": "push", "direction": "push"}]
    snapshot = {"backup": {"pairs": pairs}}
    monkeypatch.setattr(
        api_conflicts,
        "get_config",
        lambda: type("C", (), {"snapshot": lambda self: snapshot})(),
    )
    db = FakeDb()
    monkeypatch.setattr(api_conflicts, "get_db", lambda: db)
    lease = FakeLease()
    monkeypatch.setattr(api_conflicts, "try_file_lock", lambda name: lease)
    reauth = []
    monkeypatch.setattr(
        api_conflicts,
        "require_reauthentication",
        lambda request, user, password: reauth.append((user, password)),
    )
    resolved = []

    def fake_resolve(pair, base_path, choice):
        resolved.append((pair["name"], base_path, choice))
        return {
            "steps": [{"cmd": ["rclone"], "ok": True, "error": None}],
            "completed": True,
        }

    monkeypatch.setattr(conflicts, "resolve_conflict", fake_resolve)
    app = FastAPI()
    app.include_router(api_conflicts.router)
    app.dependency_overrides[require_auth] = lambda: "admin"
    client = TestClient(app)
    client.cookies.set(CSRF_COOKIE, "csrf")
    client.headers["X-CSRF-Token"] = "csrf"
    return client, db, lease, reauth, resolved


def test_route_rejects_unknown_and_non_bisync_pairs(api):
    client = api[0]
    assert client.get("/api/pairs/missing/conflicts").status_code == 404
    assert client.get("/api/pairs/push/conflicts").status_code == 400
    response = client.post(
        "/api/pairs/push/conflicts/resolve",
        json={"base_path": "a", "choice": "keep_both"},
    )
    assert response.status_code == 400


def test_route_keep_side1_requires_password(api):
    client, db, _, reauth, resolved = api
    response = client.post(
        "/api/pairs/docs/conflicts/resolve",
        json={"base_path": "a.txt", "choice": "keep_side1"},
    )
    assert response.status_code == 403
    assert response.json()["detail"]["reauth_required"] is True
    assert resolved == [] and db.audits == []

    response = client.post(
        "/api/pairs/docs/conflicts/resolve",
        json={"base_path": "a.txt", "choice": "keep_side1", "password": "pw"},
    )
    assert response.status_code == 200
    assert reauth == [("admin", "pw")]
    assert resolved == [("docs", "a.txt", "keep_side1")]


def test_route_keep_both_without_password_audits(api):
    client, db, lease, reauth, resolved = api
    response = client.post(
        "/api/pairs/docs/conflicts/resolve",
        json={"base_path": "a.txt", "choice": "keep_both"},
    )
    assert response.status_code == 200 and response.json()["completed"] is True
    assert reauth == [] and lease.released is True
    audit_details = db.audits[0][1]
    assert len(audit_details.pop("base_path_sha256")) == 16
    assert db.audits == [
        (
            "conflict_resolved",
            {
                "pair": "docs",
                "choice": "keep_both",
                "steps": 1,
                "completed": True,
            },
        )
    ]


def test_route_conflicts_with_running_backup(api):
    client, db, lease, _, resolved = api
    db.running = {"id": 7}
    response = client.post(
        "/api/pairs/docs/conflicts/resolve",
        json={"base_path": "a.txt", "choice": "keep_both"},
    )
    assert response.status_code == 409
    assert resolved == [] and lease.released is True


def test_route_unclear_origin_is_conflict(api, monkeypatch):
    client, db, lease, _, _ = api

    def unclear(pair, base_path, choice):
        raise conflicts.OriginUnclear("Herkunft nicht eindeutig")

    monkeypatch.setattr(conflicts, "resolve_conflict", unclear)
    response = client.post(
        "/api/pairs/docs/conflicts/resolve",
        json={"base_path": "a.txt", "choice": "keep_side1", "password": "pw"},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "Herkunft nicht eindeutig"
    assert lease.released is True and db.audits == []
