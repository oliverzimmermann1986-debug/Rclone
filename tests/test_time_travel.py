from __future__ import annotations

import os
import subprocess
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import require_auth
from app.recovery_points import RecoveryPointError
from app.routes import api_time_travel
from app.security import require_csrf
from app.time_travel import (
    MAX_FILE_BYTES,
    browse_virtual_point,
    diff_point_files,
    file_version_history,
)


def _setup_timetravel_fixture(tmp_path: Path) -> tuple[dict, dict, Path, Path]:
    tz = ZoneInfo("Europe/Berlin")
    t1_ts = datetime(2026, 8, 20, 3, 0, 0, tzinfo=tz).timestamp()
    t2_ts = datetime(2026, 8, 27, 3, 0, 0, tzinfo=tz).timestamp()
    later_ts = datetime(2026, 9, 5, 12, 0, 0, tzinfo=tz).timestamp()

    current = tmp_path / "current"
    versions = tmp_path / "versions"
    t1_dir = versions / "2026-08-20T03-00-00"
    t2_dir = versions / "2026-08-27T03-00-00"

    for directory in (current, t1_dir, t2_dir):
        directory.mkdir(parents=True)

    # 1. File archived at T1 (modified/deleted during T1 sync)
    f_archived = t1_dir / "notes.txt"
    f_archived.write_text("line1\nold-t1\n", encoding="utf-8")
    os.utime(f_archived, (t1_ts, t1_ts))

    # 2. File unchanged at T1, but displaced later at T2
    f_displaced = t2_dir / "displaced.txt"
    f_displaced.write_text("state-from-t1-displaced-at-t2\n", encoding="utf-8")
    os.utime(f_displaced, (t1_ts - 3600, t1_ts - 3600))

    # Newer version of notes.txt also archived at T2
    f_notes_t2 = t2_dir / "notes.txt"
    f_notes_t2.write_text("line1\nmid-t2\n", encoding="utf-8")
    os.utime(f_notes_t2, (t2_ts, t2_ts))

    # 3. Live current files
    f_notes_live = current / "notes.txt"
    f_notes_live.write_text("line1\nlive-current\nadded-line\n", encoding="utf-8")
    os.utime(f_notes_live, (later_ts, later_ts))

    f_displaced_live = current / "displaced.txt"
    f_displaced_live.write_text("live-displaced\n", encoding="utf-8")
    os.utime(f_displaced_live, (later_ts, later_ts))

    # File in current that existed before T1 and was never touched since
    f_unchanged = current / "evergreen.txt"
    f_unchanged.write_text("unchanged-since-beginning\n", encoding="utf-8")
    os.utime(f_unchanged, (t1_ts - 7200, t1_ts - 7200))

    # File in current that was created only AFTER T1 (should NOT appear in T1 virtual view)
    f_created_later = current / "brandnew.txt"
    f_created_later.write_text("created-in-september\n", encoding="utf-8")
    os.utime(f_created_later, (later_ts, later_ts))

    # Subdirectory that only has unchanged files in current
    sub_current = current / "docs"
    sub_current.mkdir()
    f_sub = sub_current / "manual.txt"
    f_sub.write_text("handbook\n", encoding="utf-8")
    os.utime(f_sub, (t1_ts - 1000, t1_ts - 1000))

    pair = {
        "id": "docs-pair",
        "name": "Dokumente",
        "direction": "pull",
        "local": str(current),
        "remote": "cloud:/Dokumente",
        "backup_dir": str(versions / "{date}"),
    }
    config = {
        "backup": {"timezone": "Europe/Berlin", "pairs": [pair]},
        "paths": {"data_dir": str(tmp_path / "data")},
    }
    return config, pair, current, versions


def test_browse_virtual_point_reconstructs_historical_directory(tmp_path: Path):
    config, pair, _current, _versions = _setup_timetravel_fixture(tmp_path)

    live_view = browse_virtual_point(config, pair, "current", "")
    assert live_view["virtual"] is False
    assert all(item["status_at_point"] == "live" for item in live_view["items"])

    t1_view = browse_virtual_point(config, pair, "2026-08-20T03-00-00", "")
    assert t1_view["virtual"] is True
    by_name = {item["name"]: item for item in t1_view["items"]}

    # notes.txt comes directly from T1 archive
    assert by_name["notes.txt"]["status_at_point"] == "archived"
    assert by_name["notes.txt"]["source_point"] == "2026-08-20T03-00-00"

    # displaced.txt was not in T1 archive, but was displaced in T2
    assert by_name["displaced.txt"]["status_at_point"] == "displaced_later"
    assert by_name["displaced.txt"]["source_point"] == "2026-08-27T03-00-00"

    # evergreen.txt is in current with mtime <= T1
    assert by_name["evergreen.txt"]["status_at_point"] == "unchanged"
    assert by_name["evergreen.txt"]["source_point"] == "current"

    # docs directory is included from current
    assert by_name["docs"]["is_dir"] is True
    assert by_name["docs"]["status_at_point"] == "unchanged"

    # brandnew.txt was created after T1 and must not appear in T1
    assert "brandnew.txt" not in by_name

    # Subdirectory that only exists in current can still be browsed virtually at T1
    sub_view = browse_virtual_point(config, pair, "2026-08-20T03-00-00", "docs")
    assert [item["name"] for item in sub_view["items"]] == ["manual.txt"]
    assert sub_view["items"][0]["status_at_point"] == "unchanged"


def test_file_version_history_and_diff_between_points(tmp_path: Path):
    config, pair, current, _versions = _setup_timetravel_fixture(tmp_path)

    history = file_version_history(config, pair, "notes.txt")
    assert history["pair"] == "Dokumente"
    assert history["path"] == "notes.txt"
    assert [v["point_id"] for v in history["versions"]] == [
        "current",
        "2026-08-27T03-00-00",
        "2026-08-20T03-00-00",
    ]

    diff = diff_point_files(
        config,
        pair,
        "notes.txt",
        from_point="2026-08-20T03-00-00",
        to_point="2026-08-27T03-00-00",
    )
    assert diff["from_point"] == "2026-08-20T03-00-00"
    assert diff["to_point"] == "2026-08-27T03-00-00"
    assert [row["type"] for row in diff["rows"]] == ["equal", "replace"]
    assert diff["rows"][1]["left"] == "old-t1"
    assert diff["rows"][1]["right"] == "mid-t2"

    # Reject binary and oversized files
    (current / "binary.bin").write_bytes(b"abc\x00def")
    with pytest.raises(RecoveryPointError, match="Textdateien"):
        diff_point_files(config, pair, "binary.bin", "current", "current")

    (current / "huge.txt").write_bytes(b"a" * (MAX_FILE_BYTES + 1))
    with pytest.raises(RecoveryPointError, match="zu groß"):
        diff_point_files(config, pair, "huge.txt", "current", "current")


def test_diff_rejects_symlinks_and_Check_api_routes(tmp_path: Path, monkeypatch):
    config, pair, current, _versions = _setup_timetravel_fixture(tmp_path)

    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = current / "escape.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        made = (
            os.name == "nt"
            and subprocess.run(
                ["cmd", "/c", "mklink", "/H", str(link), str(outside)],
                capture_output=True,
            ).returncode
            == 999  # skip if symlink privilege unavailable
        )
        if not made:
            link = None

    if link is not None and link.is_symlink():
        with pytest.raises(RecoveryPointError, match="Ungültiger relativer Pfad"):
            diff_point_files(config, pair, "escape.txt", "current", "current")

    monkeypatch.setattr(
        api_time_travel,
        "get_config",
        lambda: SimpleNamespace(snapshot=lambda: config),
    )
    app = FastAPI()
    app.include_router(api_time_travel.router)
    app.dependency_overrides[require_auth] = lambda: "admin"
    app.dependency_overrides[require_csrf] = lambda: None
    client = TestClient(app)

    browse_resp = client.get(
        "/api/recovery/timetravel/browse",
        params={"identity": "Dokumente", "point_id": "2026-08-20T03-00-00"},
    )
    assert browse_resp.status_code == 200
    assert browse_resp.json()["virtual"] is True

    hist_resp = client.get(
        "/api/recovery/timetravel/history",
        params={"identity": "Dokumente", "path": "notes.txt"},
    )
    assert hist_resp.status_code == 200
    assert len(hist_resp.json()["versions"]) == 3

    diff_resp = client.get(
        "/api/recovery/timetravel/diff",
        params={
            "identity": "Dokumente",
            "path": "notes.txt",
            "from_point": "2026-08-20T03-00-00",
            "to_point": "current",
        },
    )
    assert diff_resp.status_code == 200
    assert diff_resp.json()["stats"] == {
        "equal": 1,
        "insert": 1,
        "delete": 0,
        "replace": 1,
    }

    missing_resp = client.get(
        "/api/recovery/timetravel/diff",
        params={
            "identity": "Dokumente",
            "path": "does-not-exist.txt",
            "from_point": "current",
            "to_point": "current",
        },
    )
    assert missing_resp.status_code == 404

    traversal_resp = client.get(
        "/api/recovery/timetravel/diff",
        params={
            "identity": "Dokumente",
            "path": "../secret.txt",
            "from_point": "current",
            "to_point": "current",
        },
    )
    assert traversal_resp.status_code == 400
