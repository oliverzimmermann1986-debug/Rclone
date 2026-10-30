import os
import subprocess
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.routes import api_recovery_diff as api
from app.text_diff import diff_rows, is_probably_text


def kinds(result):
    return [row["type"] for row in result["rows"]]


def test_diff_rows_equal_insert_delete_replace():
    result = diff_rows("a\nb\nc\nd\n", "a\nB\nc\nx\ny\n")
    assert kinds(result) == ["equal", "replace", "equal", "replace", "insert"]
    assert result["rows"][1] == {
        "type": "replace",
        "left_no": 2,
        "left": "b",
        "right_no": 2,
        "right": "B",
    }
    assert result["rows"][4]["left"] is None and result["rows"][4]["right"] == "y"
    assert result["stats"] == {"equal": 2, "insert": 1, "delete": 0, "replace": 2}
    assert not result["truncated"]
    assert kinds(diff_rows("a\nb\nc", "a\nc")) == ["equal", "delete", "equal"]
    assert diff_rows("a\nb\nc", "a\nc")["rows"][1]["right_no"] is None


def test_diff_rows_replace_extra_lines_and_line_endings():
    result = diff_rows("a\r\nb\r\nc\r\n", "a\nX\n")
    assert kinds(result) == ["equal", "replace", "delete"]
    assert result["rows"][2]["left"] == "c" and result["rows"][2]["left_no"] == 3
    assert diff_rows("", "")["rows"] == []
    assert kinds(diff_rows("", "x\n")) == ["insert"]


def test_diff_rows_truncated():
    text = "".join(f"{n}\n" for n in range(10))
    result = diff_rows(text, text, max_lines=4)
    assert result["truncated"] and len(result["rows"]) == 4
    assert not diff_rows(text, text, max_lines=10)["truncated"]


def test_is_probably_text():
    assert is_probably_text(b"") and is_probably_text("Grüße\n".encode())
    assert not is_probably_text(b"ab\x00cd")
    assert not is_probably_text(b"\xff\xfe\x00A")
    assert not is_probably_text(b"abc\xe9def")
    # Mehrbyte-Zeichen an der Probengrenze abgeschnitten: weiterhin Text
    assert is_probably_text(("a" * 8191 + "ä").encode())


@pytest.fixture
def env(monkeypatch, tmp_path):
    staging, live = tmp_path / "staging" / "data", tmp_path / "live"
    staging.mkdir(parents=True)
    live.mkdir()
    pair = {"id": "p1", "name": "Dummy", "local": str(live)}
    entry = {"id": "recovery-1", "pair": "Dummy", "staging_path": str(staging)}
    monkeypatch.setattr(
        api,
        "get_config",
        lambda: SimpleNamespace(snapshot=lambda: {"backup": {"pairs": [pair]}}),
    )
    monkeypatch.setattr(api, "get_db", lambda: None)
    monkeypatch.setattr(api, "list_staging", lambda db, config: [entry])
    return SimpleNamespace(staging=staging, live=live, tmp=tmp_path)


def status_of(path, recovery_id="recovery-1"):
    with pytest.raises(HTTPException) as info:
        api.staging_diff(recovery_id, path)
    return info.value.status_code


def test_route_diff_with_live_file(env):
    (env.staging / "sub").mkdir()
    (env.staging / "sub" / "a.txt").write_bytes(b"eins\nzwei\n")
    (env.live / "sub").mkdir()
    (env.live / "sub" / "a.txt").write_bytes(b"eins\ndrei\n")
    result = api.staging_diff("recovery-1", "sub/a.txt")
    assert result["recovery_id"] == "recovery-1" and result["path"] == "sub/a.txt"
    assert result["left"]["size"] == 10 and result["right"]["size"] == 10
    assert isinstance(result["left"]["mtime"], float)
    assert kinds(result) == ["equal", "replace"]


def test_route_live_file_missing(env):
    (env.staging / "new.txt").write_text("x\n", encoding="utf-8")
    result = api.staging_diff("recovery-1", "new.txt")
    assert result["right"] is None
    # Staging-only-Inhalt steht links und gilt gegenüber Live als gelöscht
    assert kinds(result) == ["delete"] and result["rows"][0]["left"] == "x"


def test_route_errors(env):
    (env.tmp / "secret.txt").write_text("geheim", encoding="utf-8")
    (env.staging / "bin.dat").write_bytes(b"ab\x00cd")
    (env.staging / "big.txt").write_bytes(b"a" * (api.MAX_FILE_BYTES + 1))
    (env.staging / "latin.txt").write_bytes(b"a" * 9000 + b"\xe9")
    assert status_of("../secret.txt") == 400
    assert status_of("../../secret.txt") == 400
    assert status_of("/etc/passwd") == 400
    assert status_of(".") == 400
    assert status_of("missing.txt") == 404
    assert status_of("a.txt", recovery_id="recovery-9") == 404
    assert status_of("bin.dat") == 415
    assert status_of("latin.txt") == 415
    assert status_of("big.txt") == 413
    (env.staging / "ok.txt").write_text("x", encoding="utf-8")
    (env.live / "bin.dat").write_bytes(b"\x00\x01")
    (env.live / "ok.txt").write_bytes(b"\x00\x01")
    assert status_of("ok.txt") == 415


def test_route_link_escaping_root(env):
    outside = env.tmp / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("geheim", encoding="utf-8")
    link = env.staging / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:  # Windows ohne Symlink-Recht: Junction
        made = (
            os.name == "nt"
            and subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                capture_output=True,
            ).returncode
            == 0
        )
        if not made:
            pytest.skip("Weder Symlink noch Junction verfügbar")
    assert status_of("link/secret.txt") == 400
