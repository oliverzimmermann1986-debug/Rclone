from types import SimpleNamespace

from app.copies import build_matrix, target_scope
from app.db import Database


class _Cfg:
    def __init__(self, data):
        self._data = data

    def get(self, *path, default=None):
        node = self._data
        for key in path:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node


def _cfg(pairs):
    return _Cfg({"backup": {"pairs": pairs, "default_schedule": "0 3 * * *"}})


def _pair(name, local, remote, **overrides):
    pair = {
        "name": name,
        "id": name.replace("-", "")[:32].ljust(32, "0"),
        "local": local,
        "remote": remote,
        "direction": "push",
        "mode": "copy",
        "enabled": True,
        "schedule": "0 2 * * *",
    }
    pair.update(overrides)
    return pair


def test_target_scope_groups_by_storage_unit(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    assert target_scope("wasabi:media-bk/2026") == "remote-name:wasabi:"
    assert target_scope("wasabi:andere") == "remote-name:wasabi:"
    assert target_scope(str(first)) == target_scope(str(second))
    assert target_scope(str(first)).startswith("local-device:")
    assert target_scope("") == ""


def test_local_targets_on_same_device_count_as_one_scope(tmp_path):
    first = tmp_path / "disk-a"
    second = tmp_path / "disk-b"
    first.mkdir()
    second.mkdir()
    db = Database(tmp_path / "same-device.db")

    row = build_matrix(
        _cfg(
            [
                _pair("a", "/srv/fibu", str(first)),
                _pair("b", "/srv/fibu", str(second)),
            ]
        ),
        db,
        now=1000.0,
    )["sources"][0]

    assert row["scope_count"] == 1
    assert len(row["domains"]) == 1
    assert row["domains"][0]["source"] == "st_dev"
    assert row["domains"][0]["confidence"] == "high"
    assert set(row["domains"][0]["targets"]) == {str(first), str(second)}
    assert "alle Kopien im selben Speicherort" in row["findings"]


def test_local_targets_on_different_devices_count_as_two_scopes(
    tmp_path, monkeypatch
):
    targets = {"/mnt/disk-a/backup": 101, "/mnt/disk-b/backup": 202}

    def fake_stat(path):
        value = str(path).replace("\\", "/")
        if value in targets:
            return SimpleNamespace(st_dev=targets[value])
        raise FileNotFoundError(value)

    monkeypatch.setattr("app.copies._path_stat", fake_stat)
    db = Database(tmp_path / "different-devices.db")
    row = build_matrix(
        _cfg(
            [
                _pair("a", "/srv/fibu", "/mnt/disk-a/backup"),
                _pair("b", "/srv/fibu", "/mnt/disk-b/backup"),
            ]
        ),
        db,
        now=1000.0,
    )["sources"][0]

    assert row["scope_count"] == 2
    assert {domain["id"] for domain in row["domains"]} == {
        "local-device:101",
        "local-device:202",
    }
    assert not row["domain_warnings"]
    assert "alle Kopien im selben Speicherort" not in row["findings"]


def test_missing_local_mounts_are_grouped_conservatively(tmp_path):
    first = tmp_path / "missing-mount-a" / "backup"
    second = tmp_path / "missing-mount-b" / "backup"
    db = Database(tmp_path / "missing-mounts.db")

    row = build_matrix(
        _cfg(
            [
                _pair("a", "/srv/fibu", str(first)),
                _pair("b", "/srv/fibu", str(second)),
            ]
        ),
        db,
        now=1000.0,
    )["sources"][0]

    assert row["scope_count"] == 1
    assert row["domains"][0]["source"] == "nearest_existing_parent"
    assert row["domains"][0]["confidence"] == "low"
    assert len(row["domain_warnings"]) == 2
    assert "alle Kopien im selben Speicherort" in row["findings"]
    assert any("nur abgeleitet oder unsicher" in item for item in row["findings"])


def test_explicit_remote_domain_groups_different_remotes(tmp_path):
    db = Database(tmp_path / "explicit-remote-domain.db")
    row = build_matrix(
        _cfg(
            [
                _pair(
                    "a",
                    "/srv/fibu",
                    "wasabi:fibu",
                    failure_domain="Rechenzentrum EU-1",
                ),
                _pair(
                    "b",
                    "/srv/fibu",
                    "hetzner:fibu",
                    location_id="rechenzentrum eu-1",
                ),
            ]
        ),
        db,
        now=1000.0,
    )["sources"][0]

    assert row["scope_count"] == 1
    assert row["domains"][0]["id"] == "explicit:rechenzentrum eu-1"
    assert row["domains"][0]["source"] == "explicit"
    assert row["domains"][0]["confidence"] == "high"
    assert not row["domain_warnings"]
    assert "alle Kopien im selben Speicherort" in row["findings"]


def test_scheduled_flag_comes_from_job_definitions(tmp_path):
    pair = _pair("archiv", "/srv/archiv", "wasabi:archiv")
    pair.pop("schedule", None)
    config = _Cfg(
        {
            "backup": {
                "pairs": [pair],
                "jobs": [
                    {
                        "id": "a" * 32,
                        "name": "Archiv manuell",
                        "enabled": True,
                        "data_path_ids": [pair["id"]],
                        "schedule": "manual",
                    }
                ],
            }
        }
    )
    database = Database(tmp_path / "copies-schedule.db")

    manual = build_matrix(config, database)["sources"][0]["copies"][0]
    assert manual["scheduled"] is False

    config._data["backup"]["jobs"].append(
        {
            "id": "b" * 32,
            "name": "Archiv nachts",
            "enabled": True,
            "data_path_ids": [pair["id"]],
            "schedule": "0 2 * * *",
        }
    )
    planned = build_matrix(config, database)["sources"][0]["copies"][0]
    assert planned["scheduled"] is True


def test_single_copy_is_an_error(tmp_path):
    db = Database(tmp_path / "app.db")
    matrix = build_matrix(
        _cfg([_pair("archiv", "/srv/archiv", "wasabi:archiv")]), db, now=1000.0
    )
    row = matrix["sources"][0]
    assert row["source"] == "/srv/archiv"
    assert row["copy_count"] == 1
    assert row["level"] == "error"
    assert "nur eine Kopie" in row["findings"]
    assert matrix["totals"]["single_copy"] == 1


def test_two_copies_in_same_scope_are_flagged(tmp_path):
    db = Database(tmp_path / "app.db")
    matrix = build_matrix(
        _cfg(
            [
                _pair("a", "/srv/fibu", "wasabi:fibu-1"),
                _pair("b", "/srv/fibu", "wasabi:fibu-2"),
            ]
        ),
        db,
        now=1000.0,
    )
    row = matrix["sources"][0]
    assert row["copy_count"] == 2
    assert row["scope_count"] == 1
    assert "alle Kopien im selben Speicherort" in row["findings"]


def test_two_scopes_without_versioning_only_warns(tmp_path):
    db = Database(tmp_path / "app.db")
    matrix = build_matrix(
        _cfg(
            [
                _pair("a", "/srv/fibu", "wasabi:fibu"),
                _pair("b", "/srv/fibu", "hetzner:fibu"),
            ]
        ),
        db,
        now=1000.0,
    )
    row = matrix["sources"][0]
    assert row["scope_count"] == 2
    assert "alle Kopien im selben Speicherort" not in row["findings"]
    assert "keine Versionsablage" in row["findings"]


def test_versioned_offsite_pair_is_clean(tmp_path):
    db = Database(tmp_path / "app.db")
    now = 1_000_000.0
    local_target = tmp_path / "nas1" / "fibu"
    local_target.mkdir(parents=True)
    pairs = [
        _pair(
            "a",
            "/srv/fibu",
            "wasabi:fibu",
            backup_dir="wasabi:fibu-versions/{date}",
            failure_domain="Wasabi EU",
        ),
        _pair("b", "/srv/fibu", str(local_target)),
    ]
    job_id = db.job_start("backup")
    db.job_finish(
        job_id,
        "ok",
        {
            "ok": True,
            "pairs": [{"name": "a", "ok": True}, {"name": "b", "ok": True}],
            "history_keys": {
                "a": f"rclone:id:{pairs[0]['id']}",
                "b": f"rclone:id:{pairs[1]['id']}",
            },
        },
    )
    with db.conn() as connection:
        connection.execute("UPDATE pair_runs SET ended_at=?", (now - 3600,))

    matrix = build_matrix(_cfg(pairs), db, now=now)
    row = matrix["sources"][0]
    assert row["copy_count"] == 2
    assert row["scope_count"] == 2
    assert row["offsite_count"] == 1
    assert row["newest_age_hours"] == 1.0
    assert row["findings"] == []
    assert row["level"] == "ok"


def test_local_only_copies_report_missing_offsite(tmp_path):
    db = Database(tmp_path / "app.db")
    matrix = build_matrix(
        _cfg(
            [
                _pair("a", "/srv/fibu", "/mnt/nas1/fibu"),
                _pair("b", "/srv/fibu", "/mnt/nas2/fibu"),
            ]
        ),
        db,
        now=1000.0,
    )
    row = matrix["sources"][0]
    assert row["offsite_count"] == 0
    assert "keine Kopie außer Haus" in row["findings"]
    assert matrix["totals"]["without_offsite"] == 1


def test_pull_direction_treats_local_as_the_copy(tmp_path):
    db = Database(tmp_path / "app.db")
    matrix = build_matrix(
        _cfg(
            [
                _pair(
                    "restore-kopie",
                    "/srv/spiegel",
                    "wasabi:quelle",
                    direction="pull",
                )
            ]
        ),
        db,
        now=1000.0,
    )
    row = matrix["sources"][0]
    assert row["source"] == "wasabi:quelle"
    assert row["copies"][0]["target"] == "/srv/spiegel"


def test_disabled_pairs_are_excluded(tmp_path):
    db = Database(tmp_path / "app.db")
    matrix = build_matrix(
        _cfg(
            [
                _pair("a", "/srv/fibu", "wasabi:fibu"),
                _pair("b", "/srv/fibu", "hetzner:fibu", enabled=False),
            ]
        ),
        db,
        now=1000.0,
    )
    assert matrix["sources"][0]["copy_count"] == 1


def test_pair_without_endpoints_is_skipped(tmp_path):
    db = Database(tmp_path / "app.db")
    matrix = build_matrix(_cfg([_pair("leer", "", "")]), db, now=1000.0)
    assert matrix["sources"] == []
    assert matrix["totals"]["sources"] == 0


def test_never_run_copy_raises_level_to_error(tmp_path):
    db = Database(tmp_path / "app.db")
    matrix = build_matrix(
        _cfg(
            [
                _pair("a", "/srv/fibu", "wasabi:fibu"),
                _pair("b", "/srv/fibu", "hetzner:fibu"),
            ]
        ),
        db,
        now=1000.0,
    )
    row = matrix["sources"][0]
    assert row["level"] == "error"
    assert any("ohne je erfolgreichen Lauf" in item for item in row["findings"])


def test_sources_sorted_by_risk_first(tmp_path):
    db = Database(tmp_path / "app.db")
    matrix = build_matrix(
        _cfg(
            [
                _pair("a", "/srv/viele", "wasabi:viele"),
                _pair("b", "/srv/viele", "hetzner:viele"),
                _pair("c", "/srv/einzeln", "wasabi:einzeln"),
            ]
        ),
        db,
        now=1000.0,
    )
    # Die riskanteste Quelle (eine Kopie) steht oben.
    assert matrix["sources"][0]["source"] == "/srv/einzeln"
