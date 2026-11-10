"""Review-Befunde #3 (Herkunft im Konfliktnamen) und #13 (rclone-Abschlussblock)."""

from app import conflicts
from app.jobs import rclone_sync


class EmptyConfig:
    def get(self, *keys, default=None):
        return default


def test_bisync_names_conflict_copies_by_origin():
    args = rclone_sync._global_verb_args(EmptyConfig(), "bisync")
    suffix = args[args.index("--conflict-suffix") + 1]
    assert suffix == "conflict-path1-,conflict-path2-"
    assert "--conflict-suffix" not in rclone_sync._global_verb_args(
        EmptyConfig(), "copy"
    )
    # Die daraus gebildeten Namen (mit/ohne --suffix-keep-extension) samt Seite.
    for name, side in (
        ("a.txt.conflict-path1-1", "1"),
        ("a.conflict-path2-3.txt", "2"),
    ):
        match = conflicts.CONFLICT_RE.match(name)
        assert match["side"] == side and conflicts._base_name(match) == "a.txt"


# Wörtlich nach den Format-Strings in rclone fs/accounting/stats.go.
FINAL_BLOCK = (
    "2026/10/30 03:00:10 INFO  : \n"
    "Transferred:   \t    1.234 GiB / 1.234 GiB, 100%, 12.345 MiB/s, ETA 0s\n"
    "Checks:                10 / 10, 100%, Listed 25\n"
    "Transferred:            1 / 1, 100%\n"
    "Elapsed time:       1m0.5s\n"
)


def test_final_stats_take_byte_line_and_file_count_of_last_block():
    older = (
        "Transferred:   \t  100 MiB / 1.234 GiB, 8%, 10 MiB/s, ETA 1m\n"
        "Transferred:            0 / 1, 0%\n"
    )
    assert rclone_sync.parse_final_stats(older + FINAL_BLOCK) == {
        "transferred": "1.234 GiB",
        "total": "1.234 GiB",
        "percent": 100.0,
        "speed": "12.345 MiB/s",
        "eta": "0s",
        "files_transferred": 1,
        "files_total": 1,
    }


def test_final_stats_ignore_file_count_without_byte_line():
    assert rclone_sync.parse_final_stats("Transferred:            3 / 4, 75%\n") == {}


def test_final_stats_from_stats_one_line():
    line = (
        "2026/10/30 03:00:05 INFO  :  512 MiB / 1.234 GiB, 41%, 9 MiB/s, ETA 1m20s (xfr#0/1)\n"
        "2026/10/30 03:00:10 INFO  :    1.234 GiB / 1.234 GiB, 100%, "
        "12.345 MiB/s, ETA 0s (xfr#1/1)\n"
    )
    stats = rclone_sync.parse_final_stats(line)
    assert stats["transferred"] == "1.234 GiB"
    assert stats["speed"] == "12.345 MiB/s"
    assert stats["percent"] == 100.0
    assert (stats["files_transferred"], stats["files_total"]) == (1, 1)
    assert (
        rclone_sync.parse_final_stats("0 B / 0 B, -, 0 B/s, ETA -\n")["percent"] is None
    )
