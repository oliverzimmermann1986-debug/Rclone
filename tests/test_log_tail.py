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
