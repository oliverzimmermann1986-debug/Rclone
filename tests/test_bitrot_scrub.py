import io
import json
import math
import random
import time
from datetime import datetime, timezone
from pathlib import Path

from app.db import Database
from app.jobs import bitrot_scrub as scrub

_DAY = 86400
_MB = 1024 * 1024


def _settings(**overrides):
    return {**scrub.bitrot_scrub_settings({}), **overrides}


def _file(path, minute, size=10):
    return {"path": path, "size": size, "modtime": f"2020-01-01T00:{minute:02d}:00Z"}


def test_settings_defaults_and_clamping():
    assert scrub.bitrot_scrub_settings({}) == {
        "enabled": False,
        "schedule": "0 4 * * *",
        "sample_percent": 2,
        "max_files": 200,
        "max_total_mb": 256,
        "max_scan_files": 20_000,
        "rotation_days": 30,
    }
    clamped = scrub.bitrot_scrub_settings(
        {
            "backup": {
                "bitrot_scrub": {
                    "sample_percent": 500,
                    "max_files": 0,
                    "rotation_days": "x",
                }
            }
        }
    )
    assert clamped["sample_percent"] == 100
    assert clamped["max_files"] == 1
    assert clamped["rotation_days"] == 30
    # Obergrenzen wie in config_validation.
    huge = scrub.bitrot_scrub_settings(
        {
            "backup": {
                "bitrot_scrub": {
                    "max_files": 10**6,
                    "max_total_mb": 10**6,
                    "rotation_days": 10**6,
                }
            }
        }
    )
    assert (huge["max_files"], huge["max_total_mb"], huge["rotation_days"]) == (
        5000,
        10_240,
        365,
    )


def test_sample_takes_oldest_due_files_and_skips_recently_verified():
    now = time.time()
    files = [_file(f"f{i:02d}", i) for i in range(100)][::-1]
    # 01:30+02:00 ist 23:30Z am Vortag, also älter als alle f-Dateien.
    files.append({"path": "offset", "size": 10, "modtime": "2020-01-01T01:30:00+02:00"})
    baseline = {
        "f00": {"last_verified_at": now - 40 * _DAY},  # Rotation fällig
        "f01": {"last_verified_at": now - 2 * _DAY},  # kürzlich geprüft
    }
    settings = _settings(sample_percent=3)  # 3 % von 101 → 4

    sample = scrub._select_sample(files, baseline, settings, now=now)
    assert [item["path"] for item in sample] == ["offset", "f00", "f02", "f03"]

    baseline["offset"] = {"last_verified_at": now - _DAY}
    sample = scrub._select_sample(files, baseline, settings, now=now)
    assert [item["path"] for item in sample] == ["f00", "f02", "f03", "f04"]

    # rclone liefert Nanosekunden, gekürzte Brüche und "Z" (auch unter 3.10).
    key = scrub._modtime_key
    assert key("2020-01-01T00:00:00.123456789Z") == key(
        "2020-01-01T01:00:00.123456+01:00"
    )
    assert key("2020-01-01T00:00:00.5Z") > key("2020-01-01T00:00:00Z")
    assert key("kaputt") == math.inf


def test_sample_respects_byte_budget_max_files_and_minimum():
    files = [
        _file("huge", 0, size=5 * _MB),
        *(_file(f"s{i}", i + 1, size=_MB) for i in range(5)),
    ]
    now = time.time()
    tight = _settings(sample_percent=100, max_files=3, max_total_mb=2)
    assert [i["path"] for i in scrub._select_sample(files, {}, tight, now=now)] == [
        "s0",
        "s1",
    ]
    roomy = _settings(sample_percent=100, max_files=3, max_total_mb=10)
    assert [i["path"] for i in scrub._select_sample(files, {}, roomy, now=now)] == [
        "huge",
        "s0",
        "s1",
    ]
    tiny = _settings(sample_percent=0.01)
    assert len(scrub._select_sample(files, {}, tiny, now=now)) == 1
    assert scrub._select_sample([], {}, tiny, now=now) == []


def test_parse_hashsum_accepts_only_wellformed_lines():
    good = "a" * 64
    text = "\n".join(
        [
            f"{good}  docs/a.txt",
            f"{'B' * 64}  name  with  spaces.txt\r",
            f"{'c' * 63}  short.txt",
            "ERROR  broken.txt",
            f"{'z' * 64}  nothex.txt",
            "",
            f"{good} single-space.txt",
        ]
    )
    assert scrub._parse_hashsum(text) == {
        "docs/a.txt": good,
        "name  with  spaces.txt": "b" * 64,
    }


def test_compare_flags_only_unchanged_files_with_new_hash():
    old, new, stamp = "a" * 64, "b" * 64, "2020-01-01T00:00:00Z"
    baseline = {
        "rot": {"sha256": old, "size": 10, "modtime": stamp},
        "same": {"sha256": old, "size": 10, "modtime": stamp},
        "resized": {"sha256": old, "size": 9, "modtime": stamp},
        "touched": {"sha256": old, "size": 10, "modtime": "2021-01-01T00:00:00Z"},
    }
    sample = [
        {"path": path, "size": 10, "modtime": stamp}
        for path in ("rot", "same", "resized", "touched", "new", "unhashed")
    ]
    hashes = {"rot": new, "same": old, "resized": new, "touched": new, "new": new}

    mismatches, verified, recorded = scrub._compare(sample, hashes, baseline)

    assert mismatches == [{"path": "rot", "expected": old, "actual": new, "size": 10}]
    assert verified == ["same"]
    assert [item["path"] for item in recorded] == ["resized", "touched", "new"]
    assert all(item["sha256"] == new for item in recorded)


def test_hash_baseline_upsert_touch_prune_and_stats(tmp_path: Path):
    path = tmp_path / "baseline.db"
    db = Database(path)
    # Bestehende DBs ohne Tabelle erhalten sie beim nächsten Start.
    with db.conn() as connection:
        connection.execute("DROP TABLE file_hash_baseline")
    db = Database(path)
    assert db.hash_baseline_stats("k") == {
        "rows": 0,
        "oldest_recorded_at": None,
        "newest_recorded_at": None,
        "oldest_verified_at": None,
    }

    rows = [
        {"path": f"p{i}", "sha256": "a" * 64, "size": i, "modtime": "t"}
        for i in range(3)
    ]
    db.hash_baseline_upsert("k", rows)
    db.hash_baseline_upsert("other", rows[:1])
    db.hash_baseline_touch("k", ["p0", "p1"], 1000.0)
    got = db.hash_baseline_get_many("k", ["p0", "p2", "missing"])
    assert set(got) == {"p0", "p2"}
    assert got["p0"]["last_verified_at"] == 1000.0
    assert got["p2"]["last_verified_at"] is None

    # Geänderte Datei: neue Baseline, Prüfstand zurückgesetzt.
    db.hash_baseline_upsert("k", [{**rows[0], "sha256": "b" * 64, "size": 99}])
    p0 = db.hash_baseline_get_many("k", ["p0"])["p0"]
    assert (p0["sha256"], p0["size"], p0["last_verified_at"]) == ("b" * 64, 99, None)

    with db.conn() as connection:
        for index, recorded_at in enumerate((10.0, 20.0, 30.0)):
            connection.execute(
                "UPDATE file_hash_baseline SET recorded_at=? "
                "WHERE pair_key='k' AND path=?",
                (recorded_at, f"p{index}"),
            )
    # Prune verwirft die zuletzt verifizierte Zeile (p1); fehlende Verifikation
    # zählt ab recorded_at (p2=30), p0 wurde früh geprüft und bleibt.
    db.hash_baseline_touch("k", ["p0"], 5.0)
    assert db.hash_baseline_prune("k", max_rows=2) == 1
    assert set(db.hash_baseline_get_many("k", ["p0", "p1", "p2"])) == {"p0", "p2"}
    assert db.hash_baseline_stats("k") == {
        "rows": 2,
        "oldest_recorded_at": 10.0,
        "newest_recorded_at": 30.0,
        "oldest_verified_at": 5.0,
    }
    assert db.hash_baseline_stats("other")["rows"] == 1

    bulk = [
        {"path": f"b{i}", "sha256": "c" * 64, "size": 1, "modtime": "t"}
        for i in range(1200)
    ]
    db.hash_baseline_upsert("bulk", bulk)
    assert len(db.hash_baseline_get_many("bulk", (r["path"] for r in bulk))) == 1200


def _listing_lines(count):
    entries = [
        json.dumps({"Path": f"f{i:03d}", "Size": 1, "ModTime": "2020-01-01T00:00:00Z"})
        for i in range(count)
    ]
    return ["[\n", *(f"{entry},\n" for entry in entries), "]\n"]


def _patch_listing(monkeypatch, lines, returncode=0):
    def fake_popen(cmd, **_kwargs):
        proc = _ListingProc(cmd, lines)
        proc.returncode = returncode
        return proc

    monkeypatch.setattr(scrub.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(scrub, "_rclone_cache_args", lambda *a: [])
    monkeypatch.setattr(scrub, "_register_proc", lambda *a, **kw: None)
    monkeypatch.setattr(scrub, "_unregister_proc", lambda *a, **kw: None)
    monkeypatch.setattr(scrub, "is_cancelled", lambda *a, **kw: False)


def _list(tmp_path, **kwargs):
    return scrub._list_files(
        "r:", filter_args=[], log_file=tmp_path / "list.log", **kwargs
    )


def test_listing_reservoir_sees_every_entry_and_reaches_past_the_limit(
    tmp_path: Path, monkeypatch
):
    _patch_listing(monkeypatch, _listing_lines(300))
    listing = _list(tmp_path, max_scan=100, rng=random.Random(7))
    paths = [item["path"] for item in listing["files"]]
    assert (listing["scanned"], len(paths), listing["truncated"]) == (300, 100, True)
    assert len(set(paths)) == 100
    assert any(path >= "f100" for path in paths)  # früher nur f000..f099

    again = _list(tmp_path, max_scan=100, rng=random.Random(7))
    assert again["files"] == listing["files"]  # deterministisch bei gleichem Seed

    full = _list(tmp_path, max_scan=300)
    assert (full["scanned"], len(full["files"]), full["truncated"]) == (300, 300, False)


def test_listing_failure_after_full_read_still_raises(tmp_path: Path, monkeypatch):
    _patch_listing(monkeypatch, _listing_lines(200), returncode=1)
    try:
        _list(tmp_path, max_scan=100)
    except RuntimeError as exc:
        assert "exit 1" in str(exc)
    else:
        raise AssertionError("Listing-Fehler wurde verschluckt")


def test_truncated_pair_sets_summary_warning_but_stays_ok(tmp_path: Path, monkeypatch):
    listing = {"files": [], "scanned": 500, "truncated": True}
    monkeypatch.setattr(scrub, "_list_files", lambda *a, **kw: dict(listing))
    monkeypatch.setattr(scrub, "is_cancelled", lambda *a, **kw: False)
    monkeypatch.setattr(scrub, "reset_cancel", lambda *a, **kw: None)
    monkeypatch.setattr(scrub, "get_db", lambda: Database(tmp_path / "w.db"))
    monkeypatch.setattr(scrub, "notify", lambda *a, **kw: None)
    snapshot = {
        "paths": {"logs_dir": str(tmp_path / "logs")},
        "backup": {"pairs": [{"name": "Cloud", "id": "c1", "remote": "gdrive:B"}]},
    }

    summary = scrub.run_bitrot_scrub(config_snapshot=snapshot)
    assert summary["ok"] is True
    assert summary["pairs"][0]["truncated"] is True
    assert "Stichprobe aus begrenztem Listing" in summary["warning"]
    assert "Cloud" in summary["warning"]

    listing.update(truncated=False, scanned=3)
    assert "warning" not in scrub.run_bitrot_scrub(config_snapshot=snapshot)


class _HistoryDb:
    def __init__(self, history=None):
        self.history = history or {}
        self.requested = None

    def pair_last_history(self, identities):
        self.requested = dict(identities)
        return {scrub.BITROT_HISTORY_KEY: self.history}


def _due_cfg(**overrides):
    return {
        "backup": {"timezone": "UTC", "bitrot_scrub": {"enabled": True, **overrides}}
    }


def test_due_uses_own_history_key_and_schedule():
    slot = datetime(2026, 10, 30, 4, 5, tzinfo=timezone.utc).timestamp()
    db = _HistoryDb()
    assert scrub.bitrot_scrub_due({"backup": {}}, db) == {
        "due": False,
        "reason": "disabled",
    }

    first = scrub.bitrot_scrub_due(_due_cfg(), db, now=slot)
    assert first["due"] is True
    assert first["history_key"] == "bitrotscrub:global"
    assert db.requested == {"bitrotscrub:global": "bitrotscrub:global"}
    late = scrub.bitrot_scrub_due(_due_cfg(), db, now=slot + 3600)
    assert (late["due"], late["reason"]) == (False, "waiting_for_first_schedule")

    ran = _HistoryDb(
        {
            "last_success": {"ended_at": slot},
            "last_result": {"ok": True, "ended_at": slot},
        }
    )
    assert scrub.bitrot_scrub_due(_due_cfg(), ran, now=slot + 3600)["due"] is False
    assert scrub.bitrot_scrub_due(_due_cfg(), ran, now=slot + _DAY)["due"] is True

    assert scrub.bitrot_scrub_due(_due_cfg(schedule="manual"), db)["due"] is False
    invalid = scrub.bitrot_scrub_due(_due_cfg(schedule="kaputt"), db)
    assert invalid["reason"] == "invalid_schedule"


class _ListingProc:
    def __init__(self, cmd, lines):
        self.args = cmd
        self.stdout = io.StringIO("".join(lines))
        self.returncode = 0
        self.pid = 4242

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        pass


def test_run_summary_records_then_detects_bitrot(tmp_path: Path, monkeypatch):
    db = Database(tmp_path / "scrub.db")
    listing = [
        "[\n",
        json.dumps(
            {
                "Path": "old.bin",
                "Size": 4,
                "ModTime": "2019-01-01T00:00:00Z",
                "IsDir": False,
            }
        )
        + ",\n",
        json.dumps(
            {
                "Path": "new.bin",
                "Size": 4,
                "ModTime": "2024-01-01T00:00:00Z",
                "IsDir": False,
            }
        )
        + "\n",
        "]\n",
    ]
    cloud = {"old.bin": "a" * 64, "new.bin": "b" * 64}
    calls = []
    notes = []

    def fake_popen(cmd, **_kwargs):
        calls.append(cmd)
        return _ListingProc(cmd, listing)

    def fake_run(cmd, _log_file, **_kwargs):
        calls.append(cmd)
        wanted = Path(cmd[cmd.index("--files-from-raw") + 1]).read_text("utf-8").split()
        Path(cmd[cmd.index("--output-file") + 1]).write_text(
            "".join(f"{cloud[name]}  {name}\n" for name in wanted), encoding="utf-8"
        )
        return 0

    monkeypatch.setattr(scrub.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(scrub, "_run_rclone_command", fake_run)
    monkeypatch.setattr(scrub, "_rclone_cache_args", lambda *a: [])
    monkeypatch.setattr(scrub, "_register_proc", lambda *a, **kw: None)
    monkeypatch.setattr(scrub, "_unregister_proc", lambda *a, **kw: None)
    monkeypatch.setattr(scrub, "is_cancelled", lambda *a, **kw: False)
    monkeypatch.setattr(scrub, "reset_cancel", lambda *a, **kw: None)
    monkeypatch.setattr(scrub, "get_db", lambda: db)
    monkeypatch.setattr(scrub, "notify", lambda *a, **kw: notes.append((a, kw)))
    snapshot = {
        "paths": {"logs_dir": str(tmp_path / "logs")},
        "backup": {
            "bitrot_scrub": {"sample_percent": 50},
            "pairs": [
                {
                    "name": "Cloud",
                    "id": "c1",
                    "local": str(tmp_path / "src"),
                    "remote": "gdrive:Backup",
                    "direction": "push",
                },
                {
                    "name": "Lokal",
                    "local": str(tmp_path / "a"),
                    "remote": str(tmp_path / "b"),
                },
            ],
        },
    }

    first = scrub.run_bitrot_scrub(config_snapshot=snapshot)

    assert (first["kind"], first["ok"], first["trigger"]) == (
        "bitrotscrub",
        True,
        "web",
    )
    assert [item["name"] for item in first["pairs"]] == ["Cloud", "bitrot-scrub"]
    result = first["pairs"][0]
    assert {
        key: result[key]
        for key in (
            "scanned",
            "sampled",
            "verified",
            "recorded",
            "mismatches",
            "truncated",
            "error",
        )
    } == {
        "scanned": 2,
        "sampled": 1,
        "verified": 0,
        "recorded": 1,
        "mismatches": [],
        "truncated": False,
        "error": None,
    }
    assert first["history_keys"] == {
        "Cloud": "bitrotscrub:rclone:id:c1",
        "bitrot-scrub": "bitrotscrub:global",
    }
    assert calls[0][:5] == ["rclone", "lsjson", "-R", "--files-only", "--no-mimetype"]
    assert calls[0][-1] == "gdrive:Backup"
    assert calls[1][:4] == ["rclone", "hashsum", "sha256", "--download"]
    assert notes == []

    # old.bin wird wieder fällig und ist beim Provider still verändert.
    db.hash_baseline_touch("rclone:id:c1", ["old.bin"], 0)
    cloud["old.bin"] = "c" * 64
    second = scrub.run_bitrot_scrub(trigger="scheduler", config_snapshot=snapshot)

    assert (second["ok"], second["mismatch_files"]) == (False, 1)
    assert second["pairs"][0]["mismatches"] == [
        {"path": "old.bin", "expected": "a" * 64, "actual": "c" * 64, "size": 4}
    ]
    assert second["pairs"][-1]["mismatch_count"] == 1
    ((args, kwargs),) = notes
    assert args[:2] == ("bitrot_detected", "Cloud: Bitrot-Verdacht")
    assert "old.bin" not in args[2] and kwargs == {"pairs": ["Cloud"]}
    # Befund bleibt fällig, damit der Alarm bis zur Klärung wiederkehrt.
    assert (
        db.hash_baseline_get_many("rclone:id:c1", ["old.bin"])["old.bin"][
            "last_verified_at"
        ]
        == 0
    )

    # Historie landet unter eigenen Schlüsseln, nicht unter dem Sync-Pair.
    job_id = db.job_start("bitrotscrub")
    db.job_finish(job_id, "error", second)
    history = db.pair_last_history(
        {"bitrotscrub:global": "bitrot-scrub", "rclone:id:c1": "Cloud"}
    )
    assert history["bitrotscrub:global"]["last_result"]["ok"] is False
    assert history["rclone:id:c1"]["last_result"] is None
