from __future__ import annotations

from pathlib import Path

from app.recovery_points import browse_point, compare_points, list_points, point_target


def _fixture(tmp_path: Path) -> tuple[dict, dict, Path]:
    current = tmp_path / "current"
    versions = tmp_path / "versions"
    old = versions / "2026-08-20T03-00-00"
    newer = versions / "2026-08-27T03-00-00"
    for directory in (current, old, newer):
        directory.mkdir(parents=True)
    (old / "kept.txt").write_text("old", encoding="utf-8")
    (old / "removed.txt").write_text("gone", encoding="utf-8")
    (newer / "kept.txt").write_text("newer", encoding="utf-8")
    (current / "kept.txt").write_text("current-value", encoding="utf-8")
    (current / "added.txt").write_text("new", encoding="utf-8")
    pair = {
        "id": "documents",
        "name": "Dokumente",
        "direction": "pull",
        "local": str(current),
        "remote": "cloud:/Dokumente",
        "backup_dir": str(versions / "{date}"),
    }
    return {"backup": {"timezone": "Europe/Berlin"}}, pair, versions


def test_lists_and_resolves_version_points(tmp_path: Path):
    config, pair, versions = _fixture(tmp_path)

    points = list_points(config, pair)

    assert [item["id"] for item in points] == [
        "current",
        "2026-08-27T03-00-00",
        "2026-08-20T03-00-00",
    ]
    assert points[-1]["label"] == "20.08.2026 · 03:00"
    assert point_target(config, pair, "2026-08-20T03-00-00") == str(
        versions / "2026-08-20T03-00-00"
    )


def test_browse_and_diff_historical_point(tmp_path: Path):
    config, pair, _versions = _fixture(tmp_path)

    listing = browse_point(config, pair, "2026-08-20T03-00-00", "")
    comparison = compare_points(config, pair, "2026-08-20T03-00-00", "current")

    assert {item["name"] for item in listing["items"]} == {"kept.txt", "removed.txt"}
    assert comparison["counts"] == {"added": 1, "removed": 1, "changed": 1}
    assert comparison["changed"][0]["path"] == "kept.txt"
