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


def test_target_scope_groups_by_storage_unit():
    assert target_scope("wasabi:media-bk/2026") == "wasabi:"
    assert target_scope("wasabi:andere") == "wasabi:"
    assert target_scope("/mnt/nas1/backup/x") == "/mnt/nas1"
    assert target_scope("") == ""


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
    pairs = [
        _pair(
            "a", "/srv/fibu", "wasabi:fibu", backup_dir="wasabi:fibu-versions/{date}"
        ),
        _pair("b", "/srv/fibu", "/mnt/nas1/fibu"),
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
