import hashlib
import json
import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import file_catalog as catalog
from app.auth import require_auth
from app.recovery_snapshots import pair_fingerprint
from app.routes import api_file_catalog


@pytest.fixture
def config(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    target.mkdir()
    pair = {
        "id": "documents",
        "name": "Documents",
        "direction": "push",
        "local": str(source),
        "remote": str(target),
    }
    return (
        {"paths": {"data_dir": str(tmp_path / "server")}, "backup": {"pairs": [pair]}},
        pair,
        target,
    )


def test_search_traverses_nested_metadata_and_returns_selective_restore_request(
    config, monkeypatch
):
    cfg, pair, target = config
    (target / "folder").mkdir()
    (target / "folder/report.txt").write_text("data")
    monkeypatch.setattr(
        catalog,
        "_available_points",
        lambda *_a: ([{"id": "current", "kind": "current", "label": "Current"}], False),
    )
    result = catalog.search_files(cfg, "report", identity="documents")
    item = result["items"][0]
    assert item["path"] == "folder/report.txt" and item["size_bytes"] == 4
    assert item["verification"] == "metadata_only"
    assert item["restore_request"] == {
        "identity": "documents",
        "point_id": "current",
        "paths": ["folder/report.txt"],
    }
    assert not result["truncated"]


def test_full_snapshot_search_uses_manifest_evidence_without_reading_content(
    config, monkeypatch
):
    cfg, pair, _target = config
    point = "full-" + "a" * 32
    folder = Path(cfg["paths"]["data_dir"]) / "recovery-snapshots" / point
    (folder / "data").mkdir(parents=True)
    manifest = {
        "schema": "sicherpfad-full-snapshot-v1",
        "id": point,
        "identity": "documents",
        "fingerprint": pair_fingerprint(pair),
        "complete": True,
        "created_at": 1,
        "entries": [
            {
                "path": "report.txt",
                "size": 3,
                "sha256": hashlib.sha256(b"old").hexdigest(),
                "modified": 2_000_000_000,
            }
        ],
        "directories": [],
        "total_bytes": 3,
        "files": 1,
    }
    (folder / "manifest.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(
        catalog,
        "_available_points",
        lambda *_a: ([{"id": point, "kind": "full", "label": "Complete"}], False),
    )

    def no_listing(*_a):
        raise AssertionError("Snapshot content must not be opened or downloaded")

    monkeypatch.setattr(catalog, "_point_entries", no_listing)
    result = catalog.search_files(cfg, "report")
    assert result["items"][0]["verification"] == "manifest_sha256"
    assert result["items"][0]["modified_at"] == 2
    assert result["coverage"][0]["complete"] is True
    assert any("nicht erneut" in warning for warning in result["warnings"])


def test_change_archives_are_explicitly_partial_and_result_limit_is_disclosed(
    config, monkeypatch
):
    cfg, _pair, _target = config
    monkeypatch.setattr(
        catalog,
        "_available_points",
        lambda *_a: ([{"id": "old", "kind": "version", "label": "Archive"}], False),
    )
    monkeypatch.setattr(catalog.points, "point_target", lambda *_a: "cloud:archive")
    monkeypatch.setattr(
        catalog,
        "_point_entries",
        lambda *_a: iter(
            [{"path": "report-a.txt", "size": 1}, {"path": "report-b.txt", "size": 2}]
        ),
    )
    result = catalog.search_files(cfg, "report", limit=1)
    assert len(result["items"]) == 1 and result["truncated"]
    assert not result["coverage"][0]["complete"]
    assert any("Änderungsarchive" in warning for warning in result["warnings"])


def test_untrusted_remote_paths_are_rejected_without_restore_links(config, monkeypatch):
    cfg, _pair, _target = config
    monkeypatch.setattr(
        catalog,
        "_available_points",
        lambda *_a: ([{"id": "current", "kind": "current"}], False),
    )
    monkeypatch.setattr(catalog.points, "point_target", lambda *_a: "cloud:archive")
    monkeypatch.setattr(
        catalog,
        "_point_entries",
        lambda *_a: iter([{"path": "../report.txt", "size": 1}]),
    )
    result = catalog.search_files(cfg, "report")
    assert result["items"] == [] and result["truncated"]


def test_local_search_does_not_follow_symlinks(config, tmp_path, monkeypatch):
    cfg, _pair, target = config
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "report-secret.txt").write_text("private")
    try:
        os.symlink(outside, target / "link", target_is_directory=True)
    except OSError:
        pytest.skip("Host does not grant symlink creation")
    monkeypatch.setattr(
        catalog,
        "_available_points",
        lambda *_a: ([{"id": "current", "kind": "current"}], False),
    )
    assert catalog.search_files(cfg, "report")["items"] == []


def test_search_route_requires_auth_and_returns_bound_revision(config, monkeypatch):
    cfg, _pair, target = config
    (target / "report.txt").write_text("data")
    app = FastAPI()
    app.include_router(api_file_catalog.router)
    client = TestClient(app)
    assert client.get("/api/recovery/files/search?query=report").status_code == 401
    app.dependency_overrides[require_auth] = lambda: "admin"

    class Config:
        def snapshot_with_revision(self):
            return cfg, "revision"

    monkeypatch.setattr(api_file_catalog, "get_config", Config)
    monkeypatch.setattr(
        catalog,
        "_available_points",
        lambda *_a: ([{"id": "current", "kind": "current"}], False),
    )
    response = client.get("/api/recovery/files/search?query=report")
    assert (
        response.status_code == 200 and response.json()["config_revision"] == "revision"
    )
    assert (
        client.get(
            "/api/recovery/files/search?query=report&identity=removed"
        ).status_code
        == 404
    )


def _point(cfg, pair, number, paths):
    point_id = "full-" + f"{number:032x}"
    root = Path(cfg["paths"]["data_dir"]) / "recovery-snapshots" / point_id
    (root / "data").mkdir(parents=True)
    manifest = {
        "schema": "sicherpfad-full-snapshot-v1",
        "id": point_id,
        "identity": pair["id"],
        "fingerprint": pair_fingerprint(pair),
        "complete": True,
        "created_at": number,
        "entries": [{"path": path, "size": 1, "sha256": "a" * 64} for path in paths],
        "directories": [],
        "files": len(paths),
        "total_bytes": len(paths),
    }
    (root / "manifest.json").write_text(json.dumps(manifest))
    return point_id


def test_cursor_reaches_older_points_beyond_request_budget(config, monkeypatch):
    cfg, pair, _target = config
    monkeypatch.setattr(catalog, "MAX_SEARCH_POINTS", 2)
    expected = {_point(cfg, pair, number, ["report.txt"]) for number in range(1, 6)}
    cursor, found = "", set()
    for _page in range(8):
        result = catalog.search_files(cfg, "report", revision="revision", cursor=cursor)
        assert result["scanned_points"] <= 2
        found.update(item["point_id"] for item in result["items"])
        cursor = result["next_cursor"]
        if not cursor:
            break
    assert found == expected and cursor is None


def test_cursor_paginates_manifest_matches_and_rejects_query_revision_or_signature_changes(
    config, monkeypatch
):
    cfg, pair, _target = config
    point_id = _point(cfg, pair, 1, [f"report-{n}.txt" for n in range(5)])
    monkeypatch.setattr(
        catalog,
        "_available_points",
        lambda *_a: ([{"id": point_id, "kind": "full"}], False),
    )
    first = catalog.search_files(cfg, "report", limit=2, revision="one")
    second = catalog.search_files(
        cfg, "report", limit=2, revision="one", cursor=first["next_cursor"]
    )
    third = catalog.search_files(
        cfg, "report", limit=2, revision="one", cursor=second["next_cursor"]
    )
    paths = [row["path"] for page in (first, second, third) for row in page["items"]]
    assert len(paths) == len(set(paths)) == 5 and third["next_cursor"] is None
    for query, revision, cursor in [
        ("different", "one", first["next_cursor"]),
        ("report", "two", first["next_cursor"]),
        ("report", "one", "invalid"),
        ("report", "one", "x" + first["next_cursor"][1:]),
    ]:
        with pytest.raises(catalog.CatalogCursorError):
            catalog.search_files(cfg, query, revision=revision, cursor=cursor)


def test_remote_filename_search_streams_beyond_browser_five_hundred_entry_page(
    config, monkeypatch
):
    cfg, _pair, _target = config
    calls = []
    monkeypatch.setattr(
        catalog,
        "_available_points",
        lambda *_a: (
            [{"id": "current", "kind": "current", "_target": "cloud:backup"}],
            False,
        ),
    )

    def lines(command):
        calls.append(command)
        yield from (f"report-{number:04d}.txt\t{number}\n" for number in range(1200))

    monkeypatch.setattr(catalog.points, "_remote_lines", lines)
    result = catalog.search_files(cfg, "report-1100")
    assert result["items"][0]["path"] == "report-1100.txt"
    assert result["scanned_files"] == 1200
    assert calls[0][:3] == ["rclone", "lsf", "--recursive"]
    assert not result["truncated"]
