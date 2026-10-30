"""Zeilenweiser Textvergleich für den Restore-Diff-Viewer."""

from __future__ import annotations

import codecs
from difflib import SequenceMatcher
from typing import Any

SAMPLE_BYTES = 8192
MAX_LINES = 5000


def is_probably_text(sample: bytes) -> bool:
    """Kein NUL-Byte und UTF-8; ein am Probenende abgeschnittenes Zeichen zählt nicht."""
    sample = sample[:SAMPLE_BYTES]
    if b"\x00" in sample:
        return False
    try:
        codecs.getincrementaldecoder("utf-8")().decode(sample, final=False)
    except UnicodeDecodeError:
        return False
    return True


def _lines(text: str) -> list[str]:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines[-1] == "":
        lines.pop()
    return lines


def diff_rows(left: str, right: str, *, max_lines: int = MAX_LINES) -> dict[str, Any]:
    """Vergleicht die ersten max_lines Zeilen beider Seiten (sonst truncated)."""
    a, b = _lines(left), _lines(right)
    truncated = len(a) > max_lines or len(b) > max_lines
    a, b = a[:max_lines], b[:max_lines]
    rows: list[dict[str, Any]] = []

    def add(kind: str, i: int | None, j: int | None) -> None:
        rows.append(
            {
                "type": kind,
                "left_no": None if i is None else i + 1,
                "left": None if i is None else a[i],
                "right_no": None if j is None else j + 1,
                "right": None if j is None else b[j],
            }
        )

    # autojunk=True deckelt O(n*m) bei vielen gleichen Zeilen (CPU-DoS durch präparierte Datei).
    matcher = SequenceMatcher(None, a, b, autojunk=True)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        for k in range(max(i2 - i1, j2 - j1)):
            i = i1 + k if i1 + k < i2 else None
            j = j1 + k if j1 + k < j2 else None
            if i is None:
                add("insert", i, j)
            elif j is None:
                add("delete", i, j)
            else:
                add(tag, i, j)  # equal oder replace (zeilenweise gepaart)
    stats = {kind: 0 for kind in ("equal", "insert", "delete", "replace")}
    for row in rows:
        stats[row["type"]] += 1
    return {"rows": rows, "stats": stats, "truncated": truncated}
