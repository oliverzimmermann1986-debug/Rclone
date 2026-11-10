import pytest

from app import preflight_matrix
from app.preflight_matrix import exclude_filter_args, job_matrix, parse_dry_run_log

LOG = (
    "# Sicherpfad rclone-Log\n"
    "# Befehl: rclone sync --dry-run -- a b\n"
    "2026/10/30 03:00:01 NOTICE: ordner/datei.txt: Skipped copy as --dry-run is set (size 1.234Ki)\r\n"
    "2026/10/30 03:00:01 NOTICE: alt.txt: Skipped delete as --dry-run is set (size 12)\n"
    "2026/10/30 03:00:01 NOTICE: groß.bin: Skipped copy as --dry-run is set (size 2 MiB)\n"
    "2026/10/30 03:00:01 NOTICE: ohne.txt: Skipped copy as --dry-run is set\n"
    "2026/10/30 03:00:01 NOTICE: a.txt: Skipped move to b.txt as --dry-run is set\n"
    "2026/10/30 03:00:01 NOTICE: c.txt: Skipped move as --dry-run is set\n"
    "2026/10/30 03:00:01 NOTICE: x.txt: Skipped update modification time as --dry-run is set\n"
    "2026/10/30 03:00:01 NOTICE: leer: Skipped remove directory as --dry-run is set\n"
    "2026/10/30 03:00:01 NOTICE: Zeit: 10: Skipped copy as --dry-run is set (size 1.5 KiB)\n"
    "2026/10/30 03:00:01 INFO  : Transferred: 0 B / 0 B, -, 0 B/s, ETA -\n"
    "2026/10/30 03:00:01 ERROR : kaputt.txt: Failed to copy: denied\n"
    "# x.txt: Skipped copy as --dry-run is set\n"
)


def test_parser_covers_all_action_kinds_and_sizes():
    result = parse_dry_run_log(LOG)
    assert result["copies"] == [
        {"path": "ordner/datei.txt", "size_bytes": 1264},
        {"path": "groß.bin", "size_bytes": 2 * 1024 * 1024},
        {"path": "ohne.txt", "size_bytes": None},
        {"path": "Zeit: 10", "size_bytes": 1536},
    ]
    assert result["deletes"] == [{"path": "alt.txt", "size_bytes": 12}]
    assert result["moves"] == [
        {"path": "a.txt", "to": "b.txt"},
        {"path": "c.txt", "to": None},
    ]
    assert result["other"] == [
        {"path": "x.txt", "action": "update modification time"},
        {"path": "leer", "action": "remove directory"},
    ]
    assert result["counts"] == {"copies": 4, "deletes": 1, "moves": 2, "other": 2}
    assert result["truncated"] is False


def test_parser_uses_last_skipped_separator_and_keeps_path_untouched():
    line = "2026/10/30 03:00:01 NOTICE:  a: Skipped copy as --dry-run is set/b : Skipped copy as --dry-run is set (size 3B)"
    assert parse_dry_run_log(line)["copies"] == [
        {"path": " a: Skipped copy as --dry-run is set/b ", "size_bytes": 3}
    ]


def test_parser_limit_truncates_lists_but_counts_everything():
    text = "\n".join(
        f"NOTICE: f{i}: Skipped delete as --dry-run is set (size off)" for i in range(5)
    )
    result = parse_dry_run_log(text, limit=3)
    assert [item["path"] for item in result["deletes"]] == ["f0", "f1", "f2"]
    assert result["deletes"][0]["size_bytes"] is None
    assert result["counts"]["deletes"] == 5
    assert result["truncated"] is True


def test_exclude_filter_args_escapes_globs_and_dedupes():
    assert exclude_filter_args(["a/b.txt", "x*?[]{}\\y", "a/b.txt"]) == [
        "--filter",
        "- /a/b.txt",
        "--filter",
        "- /x\\*\\?\\[\\]\\{\\}\\\\y",
    ]
    assert exclude_filter_args([]) == []
    assert len(exclude_filter_args(["same"] * 300)) == 2


@pytest.mark.parametrize(
    "paths",
    [
        [""],
        ["/abs.txt"],
        ["a/../b"],
        [".."],
        ["a\nb"],
        ["a\x00b"],
        ["a\x85b"],
        ["x" * 1025],
        ["endet "],
        [f"f{i}" for i in range(201)],
    ],
)
def test_exclude_filter_args_rejects_invalid(paths):
    with pytest.raises(ValueError):
        exclude_filter_args(paths)


def test_exclude_filter_args_accepts_limits():
    assert len(exclude_filter_args(["x" * 1024])) == 2
    assert len(exclude_filter_args([f"f{i}" for i in range(200)])) == 400
    assert exclude_filter_args(["a..b/c"]) == ["--filter", "- /a..b/c"]


class FakeDb:
    def __init__(self, job):
        self.job = job

    def job_get(self, job_id):
        return self.job


def _backup_job(summary, *, kind="backup", dry_run=True):
    return {"id": 7, "kind": kind, "dry_run": dry_run, "summary": summary}


def test_job_matrix_reads_pair_logs_inside_root(tmp_path):
    logs = tmp_path / "logs"
    (logs / "rclone").mkdir(parents=True)
    log = logs / "rclone" / "sync-a.log"
    log.write_text(LOG, encoding="utf-8")
    outside = tmp_path / "fremd.log"
    outside.write_text(LOG, encoding="utf-8")
    summary = {
        "pairs": [
            {"name": "a", "direction": "push", "mode": "sync", "log_file": str(log)},
            {
                "name": "b",
                "direction": "pull",
                "mode": "copy",
                "log_file": str(outside),
            },
            {"name": "c", "direction": "pull", "mode": "copy", "log_file": ""},
            {
                "name": "d",
                "direction": "pull",
                "mode": "copy",
                "log_file": str(logs / "fehlt.log"),
            },
        ]
    }
    result = job_matrix(7, db=FakeDb(_backup_job(summary)), logs_root=logs)
    assert result["job_id"] == 7 and result["dry_run"] is True
    first, second, third, fourth = result["pairs"]
    assert (
        first["name"] == "a"
        and first["direction"] == "push"
        and first["mode"] == "sync"
    )
    assert first["matrix"]["counts"]["copies"] == 4
    assert "error" not in first
    assert second["matrix"] is None and "außerhalb" in second["error"]
    assert third == {
        "name": "c",
        "direction": "pull",
        "mode": "copy",
        "log_file": None,
        "matrix": None,
    }
    assert fourth["matrix"] is None and fourth["error"]


def test_job_matrix_quicksync_flat_summary_and_size_cap(tmp_path, monkeypatch):
    log = tmp_path / "quick.log"
    log.write_text(LOG, encoding="utf-8")
    summary = {"direction": "pull", "mode": "copy", "log_file": str(log)}
    monkeypatch.setattr(preflight_matrix, "_MAX_LOG_BYTES", 200)
    result = job_matrix(
        7, db=FakeDb(_backup_job(summary, kind="quicksync")), logs_root=tmp_path
    )
    (pair,) = result["pairs"]
    assert pair["direction"] == "pull"
    assert pair["matrix"]["truncated"] is True
    assert pair["matrix"]["counts"]["copies"] == 1


def test_job_matrix_rejects_missing_and_non_dry_run(tmp_path):
    with pytest.raises(LookupError):
        job_matrix(1, db=FakeDb(None), logs_root=tmp_path)
    with pytest.raises(ValueError):
        job_matrix(1, db=FakeDb(_backup_job({}, dry_run=False)), logs_root=tmp_path)
    with pytest.raises(ValueError):
        job_matrix(
            1, db=FakeDb(_backup_job({}, kind="restoretest")), logs_root=tmp_path
        )
