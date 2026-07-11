from pathlib import Path

from app.security import is_relative_to


def test_path_prefix_is_not_treated_as_containment(tmp_path: Path):
    root = tmp_path / "logs"
    sibling = tmp_path / "logs-evil" / "file.log"
    root.mkdir()
    sibling.parent.mkdir()
    sibling.write_text("x", encoding="utf-8")
    assert is_relative_to(sibling, root) is False
