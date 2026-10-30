"""Tests für den ZFS-Snapshot-Freeze der Quelle (zfs gemockt)."""

import re
import subprocess
from datetime import datetime

import pytest

from app import zfs_freeze

LISTING = "\n".join(
    [
        "tank\t/tank\tyes",
        "tank/data\t/tank/data\tyes",
        "tank/data2\t/tank/data2\tyes",
        "tank/old\t/tank/old\tno",
        "tank/legacy\tlegacy\tyes",
        "tank/none\tnone\tno",
        "tank/vol\t-\t-",
    ]
)


class FakeZfs:
    def __init__(self, listing=LISTING, fail=None, snapshots=""):
        self.listing = listing
        self.snapshots = snapshots
        self.fail = fail or {}
        self.calls = []

    def __call__(self, cmd, **kwargs):
        assert kwargs["stdin"] is subprocess.DEVNULL and kwargs["timeout"]
        self.calls.append(cmd[1:])
        sub = cmd[1]
        if sub in self.fail:
            return subprocess.CompletedProcess(cmd, 1, "", self.fail[sub])
        out = ""
        if sub == "list":
            out = self.snapshots if "snapshot" in cmd else self.listing
        return subprocess.CompletedProcess(cmd, 0, out, "")


@pytest.fixture(autouse=True)
def posix_realpath(monkeypatch):
    # Windows würde "/tank/..." zu "C:\\tank\\..." auflösen.
    monkeypatch.setattr(zfs_freeze.os.path, "realpath", lambda p: p)


def _pair(**extra):
    return {
        "local": "/tank/data/db",
        "direction": "push",
        "zfs_snapshot": True,
        **extra,
    }


def test_longest_segment_prefix_wins():
    run = FakeZfs()
    assert zfs_freeze.dataset_for_path("/tank/data/db", run=run) == (
        "tank/data",
        "/tank/data",
    )
    assert zfs_freeze.dataset_for_path("/tank/data2/x", run=run) == (
        "tank/data2",
        "/tank/data2",
    )
    assert zfs_freeze.dataset_for_path("/tank/datax", run=run) == ("tank", "/tank")
    # Nicht eingehängtes Dataset zählt nicht, sonst würde ein leerer Snapshot gesichert.
    assert zfs_freeze.dataset_for_path("/tank/old/x", run=run) == ("tank", "/tank")
    assert run.calls[0] == [
        "list",
        "-H",
        "-o",
        "name,mountpoint,mounted",
        "-t",
        "filesystem",
    ]


def test_no_dataset():
    assert zfs_freeze.dataset_for_path("/srv/x", run=FakeZfs()) is None
    with pytest.raises(RuntimeError, match="Pfad liegt auf keinem ZFS-Dataset"):
        with zfs_freeze.frozen_source(_pair(local="/srv/x"), run=FakeZfs()):
            pass


def test_nested_dataset_rejected():
    with pytest.raises(RuntimeError, match="tank/data"):
        zfs_freeze.dataset_for_path("/tank", run=FakeZfs())


def test_snapshot_source_paths():
    assert (
        zfs_freeze.snapshot_source("/tank/data", "s1", "/tank/data")
        == "/tank/data/.zfs/snapshot/s1"
    )
    assert (
        zfs_freeze.snapshot_source("/tank/data", "s1", "/tank/data/db/x")
        == "/tank/data/.zfs/snapshot/s1/db/x"
    )
    assert zfs_freeze.snapshot_source("/", "s1", "/srv") == "/.zfs/snapshot/s1/srv"
    with pytest.raises(ValueError):
        zfs_freeze.snapshot_source("/tank/data", "s1", "/tank/data2")


def test_frozen_source_destroys_even_on_exception():
    run = FakeZfs()
    with pytest.raises(KeyError):
        with zfs_freeze.frozen_source(_pair(), run=run) as frozen:
            name = frozen["snapshot"]
            assert frozen["active"] and frozen["dataset"] == "tank/data"
            assert frozen["source"] == f"/tank/data/.zfs/snapshot/{name}/db"
            raise KeyError("boom")
    assert re.fullmatch(r"sicherpfad-\d{8}-\d{6}-[0-9a-f]{6}", name)
    assert run.calls[1:] == [
        ["list", "-H", "-t", "snapshot", "-o", "name", "tank/data"],
        ["snapshot", f"tank/data@{name}"],
        ["destroy", f"tank/data@{name}"],
    ]


OLD_OWN = "sicherpfad-20200101-000000-abcdef"


def test_stale_own_snapshots_are_destroyed_before_new_one():
    fresh = f"sicherpfad-{datetime.now():%Y%m%d-%H%M%S}-123456"
    run = FakeZfs(
        snapshots="\n".join(
            [
                f"tank/data@{OLD_OWN}",
                f"tank/data@{fresh}",  # evtl. paralleler Lauf
                "tank/data@manuell-20200101",
                f"tank/data/sub@{OLD_OWN}",
                "tank/data@sicherpfad-2020-kaputt",
            ]
        )
    )
    with zfs_freeze.frozen_source(_pair(), run=run) as frozen:
        assert frozen["stale_cleanup"] == {"destroyed": 1, "failed": 0}
    assert [c for c in run.calls if c[0] in ("destroy", "snapshot")] == [
        ["destroy", f"tank/data@{OLD_OWN}"],
        ["snapshot", f"tank/data@{frozen['snapshot']}"],
        ["destroy", f"tank/data@{frozen['snapshot']}"],
    ]


def test_stale_cleanup_failure_is_counted_not_raised():
    run = FakeZfs(snapshots=f"tank/data@{OLD_OWN}", fail={"destroy": "busy"})
    with zfs_freeze.frozen_source(_pair(), run=run) as frozen:
        assert frozen["stale_cleanup"] == {"destroyed": 0, "failed": 1}
    assert ["snapshot", f"tank/data@{frozen['snapshot']}"] in run.calls


def test_destroy_failure_is_recorded_not_raised():
    run = FakeZfs(fail={"destroy": "dataset is busy"})
    with zfs_freeze.frozen_source(_pair(), run=run) as frozen:
        pass
    assert "dataset is busy" in frozen["cleanup_error"]


@pytest.mark.parametrize("direction", ["bisync", "pull", None])
def test_only_push_allowed(direction):
    run = FakeZfs()
    with pytest.raises(RuntimeError, match="nur für direction: push"):
        with zfs_freeze.frozen_source(_pair(direction=direction), run=run):
            pass
    assert run.calls == []


def test_inactive_without_flag():
    run = FakeZfs()
    with zfs_freeze.frozen_source(
        {"local": "/tank/data", "direction": "push"}, run=run
    ) as frozen:
        assert frozen == {"active": False, "source": "/tank/data"}
    assert run.calls == []


@pytest.mark.parametrize(
    "dataset,name",
    [
        ("tank/data", "bad@x"),
        ("tank/data", "a/b"),
        ("tank data", "s"),
        ("-r", "s"),
        ("tank", "a%b"),
    ],
)
def test_name_regex(dataset, name):
    run = FakeZfs()
    with pytest.raises(ValueError):
        zfs_freeze.create_snapshot(dataset, name, run=run)
    with pytest.raises(ValueError):
        zfs_freeze.destroy_snapshot(dataset, name, run=run)
    assert run.calls == []


def test_missing_zfs_binary():
    def run(cmd, **kwargs):
        raise FileNotFoundError(cmd[0])

    with pytest.raises(
        RuntimeError, match=r"zfs-Werkzeuge nicht installiert \(Paket zfsutils-linux\)"
    ):
        zfs_freeze.dataset_for_path("/tank", run=run)


def test_zfs_error_uses_short_stderr():
    run = FakeZfs(fail={"snapshot": "x" * 1000})
    with pytest.raises(RuntimeError) as info:
        zfs_freeze.create_snapshot("tank/data", "s1", run=run)
    assert "x" * 300 in str(info.value) and "x" * 301 not in str(info.value)
