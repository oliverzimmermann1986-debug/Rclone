import os

from app.jobs import log_tail
from app.jobs.log_tail import read_tail


def test_tail_reuses_and_extends_cached_content(tmp_path):
    log = tmp_path / "job.log"
    log.write_bytes(b"first\n")
    assert read_tail(log) == "first\n"
    assert read_tail(log) == "first\n"

    with log.open("ab") as handle:
        handle.write(b"second\n")
    assert read_tail(log) == "first\nsecond\n"


def test_tail_handles_truncation_and_bounds_memory(tmp_path):
    log = tmp_path / "job.log"
    log.write_bytes(b"x" * 3000)
    assert len(read_tail(log, max_bytes=1024)) == 1024

    log.write_bytes(b"new\n")
    assert read_tail(log, max_bytes=1024) == "new\n"


def test_smaller_poll_keeps_larger_cached_window_after_append(tmp_path):
    log_tail._clear_cache()
    log = tmp_path / "mixed-window.log"
    log.write_bytes(b"a" * 3000)
    assert len(read_tail(log, max_bytes=2048)) == 2048

    with log.open("ab") as handle:
        handle.write(b"b" * 100)
    assert len(read_tail(log, max_bytes=1024)) == 1024
    assert len(log_tail._CACHE[str(log)].data) == 2048
    assert read_tail(log, max_bytes=2048) == "a" * 1948 + "b" * 100
    log_tail._clear_cache()


def test_tail_detects_same_size_rewrite(tmp_path):
    log = tmp_path / "same-size.log"
    log.write_bytes(b"first\n")
    assert read_tail(log) == "first\n"
    previous = log.stat().st_mtime_ns

    log.write_bytes(b"other\n")
    os.utime(log, ns=(previous + 1_000_000, previous + 1_000_000))
    assert read_tail(log) == "other\n"


def test_tail_detects_rotation_even_with_same_size(tmp_path):
    log = tmp_path / "rotated.log"
    old = tmp_path / "rotated.log.1"
    log.write_bytes(b"before\n")
    assert read_tail(log) == "before\n"

    log.rename(old)
    log.write_bytes(b"after!\n")
    assert log.stat().st_size == old.stat().st_size
    assert read_tail(log) == "after!\n"


def test_cache_respects_strict_global_byte_budget(tmp_path, monkeypatch):
    log_tail._clear_cache()
    monkeypatch.setattr(log_tail, "_CACHE_MAX_BYTES", 2048)
    for index in range(4):
        path = tmp_path / f"{index}.log"
        path.write_bytes(bytes([65 + index]) * 1500)
        assert len(read_tail(path, max_bytes=1024)) == 1024
        assert log_tail._CACHE_BYTES <= 2048
        assert sum(len(entry.data) for entry in log_tail._CACHE.values()) <= 2048
    assert len(log_tail._CACHE) <= 2
    log_tail._clear_cache()


def test_inplace_truncate_does_not_splice_stale_tail(tmp_path):
    log_tail._clear_cache()
    path = tmp_path / "pair.log"
    path.write_bytes(b"A" * 4096 + b"alte-zeile\n")
    first = log_tail.read_tail(path, max_bytes=8192)
    assert first.endswith("alte-zeile\n")

    # logrotate copytruncate: gleicher Inode, Größe zurück auf 0, danach wächst
    # die Datei über die alte Größe hinaus.
    with path.open("r+b") as handle:
        handle.truncate(0)
    path.write_bytes(b"B" * 8192 + b"neue-zeile\n")

    second = log_tail.read_tail(path, max_bytes=8192)
    assert "alte-zeile" not in second
    assert second.endswith("neue-zeile\n")
    assert "A" not in second
