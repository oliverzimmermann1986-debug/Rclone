import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.db import Database
from app.jobs import selective_restore as recovery


@pytest.mark.parametrize(
    "scenario",
    [
        "missing_before",
        "missing_after",
        "changed_after",
        "extra_after",
        "good",
        "no_native_hash",
    ],
)
def test_exact_expected_selection_is_required(monkeypatch, tmp_path, scenario):
    database = Database(tmp_path / "recovery.db")
    config = {"paths": {"recovery_dir": str(tmp_path / "staging")}}
    contents = {"a.txt": b"alpha", "b.txt": b"bravo"}
    rows = [
        {
            "Path": name,
            "Size": len(data),
            "Hashes": {"SHA-256": hashlib.sha256(data).hexdigest()},
        }
        for name, data in contents.items()
    ]
    if scenario == "missing_before":
        rows.pop()
    if scenario == "no_native_hash":
        for row in rows:
            row["Hashes"] = {}
    commands = []

    def run(command, *, timeout):
        commands.append(command)
        stdout = ""
        if command[1] == "lsjson":
            stdout = json.dumps(rows)
        elif command[1] == "hashsum":
            stdout = "\n".join(
                f"{hashlib.sha256(data).hexdigest()}  {name}"
                for name, data in contents.items()
            )
        elif command[1] == "copy":
            destination = Path(command[-1])
            for name, data in contents.items():
                if scenario == "missing_after" and name == "b.txt":
                    continue
                (destination / name).write_bytes(
                    b"wrong" if scenario == "changed_after" else data
                )
            if scenario == "extra_after":
                (destination / "unexpected.txt").write_text("extra", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(recovery, "_run", run)
    monkeypatch.setattr(recovery, "notify", lambda *_args, **_kwargs: None)
    job_id = database.job_start("recovery")
    result = recovery.run_selective_restore(
        database,
        config,
        {
            "id": "pair",
            "name": "Photos",
            "direction": "push",
            "local": "/live",
            "remote": "cloud:/Photos",
        },
        list(contents),
        max_total_mb=1,
        job_id=job_id,
    )
    if scenario in {"good", "no_native_hash"}:
        assert result["status"] == "ready"
        assert result["files"] == 2
        assert result["verification_scope"] == "complete_selection"
        assert result["manifest_sha256"]
        assert any(
            command[1] == "check" and "--download" in command for command in commands
        )
    else:
        assert result["status"] == "error"
        assert not result.get("verified")
        assert not (tmp_path / "staging" / f"recovery-{job_id}").exists()
        if scenario == "missing_before":
            assert not any(command[1] == "copy" for command in commands)
