import asyncio
import io
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import recovery_snapshots
from app.db import Database
from app.routes import api_recovery


@pytest.mark.parametrize("tampered", [False, True])
def test_export_uses_original_proof_and_preserves_files_and_empty_directories(
    tmp_path: Path, monkeypatch, tampered
):
    target = tmp_path / "target"
    target.mkdir()
    (target / "empty").mkdir()
    (target / "SHA256SUMS.txt").write_bytes(b"user document")
    pair = {
        "id": "export-path",
        "name": "Export",
        "direction": "push",
        "mode": "copy",
        "local": str(tmp_path / "source"),
        "remote": str(target),
    }
    config = {
        "paths": {
            "data_dir": str(tmp_path / "server"),
            "recovery_dir": str(tmp_path / "staging"),
            "recovery_snapshots_dir": str(tmp_path / "points"),
        },
        "backup": {"pairs": [pair]},
    }
    database = Database(tmp_path / "state.db")
    point = recovery_snapshots.capture_snapshot(config, pair, max_total_mb=1)
    job_id = database.job_start("recovery")
    restored = recovery_snapshots.run_snapshot_restore(
        database, config, pair, point_id=point["id"], max_total_mb=1, job_id=job_id
    )
    assert restored["verified"] is True
    monkeypatch.setattr(api_recovery, "get_db", lambda: database)
    monkeypatch.setattr(
        api_recovery, "get_config", lambda: SimpleNamespace(snapshot=lambda: config)
    )
    if tampered:
        (Path(restored["staging_path"]) / "SHA256SUMS.txt").write_bytes(
            b"changed after verification"
        )
        with pytest.raises(HTTPException) as denied:
            api_recovery.download_staging_archive(restored["id"], user="admin")
        assert denied.value.status_code == 409
        return
    response = api_recovery.download_staging_archive(restored["id"], user="admin")

    async def collect():
        return b"".join([chunk async for chunk in response.body_iterator])

    with zipfile.ZipFile(io.BytesIO(asyncio.run(collect()))) as archive:
        names = archive.namelist()
        assert len(names) == len(set(names))
        assert "empty/" in names
        assert archive.read("SHA256SUMS.txt") == b"user document"
        assert b"SHA256SUMS.txt" in archive.read("SHA256SUMS.sicherpfad-1.txt")
