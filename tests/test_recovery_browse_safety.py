import io
import json
import os
import stat
from types import SimpleNamespace

import pytest

from app import recovery_points as points
from app.routes import api_recovery as api


def configure(monkeypatch, root):
    pair = {
        "name": "Dummy",
        "direction": "push",
        "local": str(root.parent / "source"),
        "remote": str(root),
    }
    config = {"backup": {"pairs": [pair]}}
    monkeypatch.setattr(
        api, "get_config", lambda: SimpleNamespace(snapshot=lambda: config)
    )
    return config, pair


@pytest.mark.parametrize("endpoint", ["backup", "point"])
def test_local_browse_sorts_bounded_metadata(monkeypatch, tmp_path, endpoint):
    root = tmp_path / "target"
    root.mkdir()
    (root / "z-dir").mkdir()
    (root / "a.txt").write_bytes(b"dummy")
    (root / "b.txt").write_bytes(b"data")
    config, pair = configure(monkeypatch, root)
    monkeypatch.setattr(points, "MAX_BROWSE_ITEMS", 2)
    result = (
        api.browse_backup("Dummy", "")
        if endpoint == "backup"
        else points.browse_point(config, pair, "current", "")
    )
    assert [row["name"] for row in result["items"]] == ["z-dir", "a.txt"]
    assert result["items"][0]["size"] is None
    assert result["items"][1]["size"] == 5
    assert result["truncated"]


def test_entry_disappears_or_becomes_link_without_following_sort_or_stat(
    monkeypatch, tmp_path
):
    class Entry:
        def __init__(self, name, mode):
            self.name, self.mode = name, mode

        def stat(self, *, follow_symlinks):
            assert follow_symlinks is False
            if self.mode is None:
                raise FileNotFoundError("dummy disappeared")
            return SimpleNamespace(st_mode=self.mode, st_size=7, st_mtime=10)

        def is_dir(self):
            pytest.fail("sort/type check followed a pathname")

        is_file = is_dir
        is_symlink = is_dir

    class Scan:
        def __enter__(self):
            return iter(
                [
                    Entry("disappeared", None),
                    Entry("swapped-link", stat.S_IFLNK),
                    Entry("fifo", stat.S_IFIFO),
                    Entry("good", stat.S_IFREG),
                ]
            )

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(points.os, "scandir", lambda target: Scan())
    items, truncated = points._local_browse(str(tmp_path), "")
    assert [row["name"] for row in items] == ["good"]
    assert not truncated


def test_scan_stops_at_limit_even_when_entries_are_links(monkeypatch, tmp_path):
    calls = []

    class Scan:
        def __enter__(self):
            for index in range(100):
                calls.append(index)
                yield SimpleNamespace(
                    name=str(index),
                    stat=lambda **kw: SimpleNamespace(st_mode=stat.S_IFLNK),
                )

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(points.os, "scandir", lambda target: Scan())
    monkeypatch.setattr(points, "MAX_BROWSE_SCAN", 3)
    assert points._local_browse(str(tmp_path), "") == ([], True)
    assert len(calls) == 4  # one lookahead, no metadata read beyond the cap


@pytest.mark.parametrize(
    "path", ["/absolute", "../outside", "C:/outside", "a/../../outside"]
)
def test_point_browse_rejects_unsafe_paths(monkeypatch, tmp_path, path):
    config, pair = configure(monkeypatch, tmp_path)
    with pytest.raises(points.RecoveryPointError):
        points.browse_point(config, pair, "current", path)


def test_browse_rejects_symlink_directory_and_hides_link_metadata(
    monkeypatch, tmp_path
):
    root = tmp_path / "target"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_bytes(b"dummy outside")
    try:
        (root / "link").symlink_to(outside, target_is_directory=True)
        (root / "file-link").symlink_to(outside / "secret")
    except OSError:
        pytest.skip("Host does not grant symlink creation")
    config, pair = configure(monkeypatch, root)
    assert api.browse_backup("Dummy", "")["items"] == []
    assert points.browse_point(config, pair, "current", "")["items"] == []
    with pytest.raises(points.RecoveryPointError):
        points.browse_point(config, pair, "current", "link")
    with pytest.raises(api.HTTPException):
        api.browse_backup("Dummy", "link")
    with pytest.raises(points.RecoveryPointError):
        points._local_browse(str(root / "link"), "")


@pytest.mark.skipif(os.name != "posix", reason="POSIX no-follow directory handles")
def test_posix_browse_keeps_directory_handle_when_path_is_replaced(
    monkeypatch, tmp_path
):
    root = tmp_path / "target"
    root.mkdir()
    (root / "safe").write_bytes(b"safe")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_bytes(b"secret")
    original = os.scandir

    def swap(target):
        assert isinstance(target, int)
        root.rename(tmp_path / "original")
        root.symlink_to(outside, target_is_directory=True)
        return original(target)

    monkeypatch.setattr(points.os, "scandir", swap)
    monkeypatch.setattr(points.os, "supports_fd", {swap})
    items, _ = points._local_browse(str(root), "")
    assert [row["name"] for row in items] == ["safe"]


@pytest.mark.parametrize("endpoint", ["backup", "point"])
@pytest.mark.parametrize(
    "payload", [{"invalid": []}, [{"Name": str(i)} for i in range(600)]]
)
def test_remote_browse_validates_shape_and_caps_results(monkeypatch, endpoint, payload):
    pair = {
        "name": "Dummy",
        "direction": "push",
        "local": "/dummy",
        "remote": "cloud:/dummy",
    }
    config = {"backup": {"pairs": [pair]}}
    monkeypatch.setattr(
        api, "get_config", lambda: SimpleNamespace(snapshot=lambda: config)
    )
    monkeypatch.setattr(
        points.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(returncode=0, stdout=json.dumps(payload)),
    )
    processes = []

    class Process:
        def __init__(self, command, **kwargs):
            assert command[:2] == ["rclone", "lsjson"]
            assert kwargs["stderr"] == points.subprocess.DEVNULL
            self.stdout = io.BytesIO(json.dumps(payload).encode("utf-8"))
            self.returncode = None
            self.waited = False
            processes.append(self)

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = -9

        def wait(self, timeout):
            self.waited = True
            if self.returncode is None:
                self.returncode = 0
            return self.returncode

    monkeypatch.setattr(points.subprocess, "Popen", Process)

    def call():
        return (
            api.browse_backup("Dummy", "")
            if endpoint == "backup"
            else points.browse_point(config, pair, "current", "")
        )

    if isinstance(payload, dict):
        with pytest.raises((api.HTTPException, points.RecoveryPointError)):
            call()
    else:
        result = call()
        assert len(result["items"]) == 500 and result["truncated"]
    if endpoint == "point":
        assert len(processes) == 1
        assert processes[0].waited and processes[0].stdout.closed
