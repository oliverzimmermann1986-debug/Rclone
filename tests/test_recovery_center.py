import base64
import json
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from app.db import Database
from app.jobs import selective_restore
from app.routes import api_recovery


class _Config:
    def __init__(self, value):
        self.value = value

    def snapshot(self):
        return self.value


def test_encrypted_handover_round_trip_and_no_plaintext():
    payload = {"recovery_pass": {"score": 92}, "note": "private-marker"}
    envelope = api_recovery.encrypted_handover(payload, "a-strong-passphrase")
    assert "private-marker" not in json.dumps(envelope)
    salt = base64.b64decode(envelope["salt_b64"])
    key = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=envelope["iterations"],
    ).derive(b"a-strong-passphrase")
    plaintext = AESGCM(key).decrypt(
        base64.b64decode(envelope["nonce_b64"]),
        base64.b64decode(envelope["ciphertext_b64"]),
        b"rclone-recovery-handover-v1",
    )
    assert json.loads(plaintext) == payload


def test_recovery_pass_redacts_paths_by_default(monkeypatch, tmp_path: Path):
    database = Database(tmp_path / "pass.db")
    config = {
        "backup": {
            "pairs": [
                {
                    "name": "Fotos",
                    "local": "/secret/photos",
                    "remote": "cloud:/Photos",
                    "direction": "push",
                    "mode": "copy",
                }
            ]
        }
    }
    monkeypatch.setattr(api_recovery, "get_config", lambda: _Config(config))
    monkeypatch.setattr(api_recovery, "get_db", lambda: database)
    monkeypatch.setattr(
        api_recovery.api_diagnostics,
        "overview",
        lambda: {
            "system": {"hostname": "backup"},
            "pairs": {"total": 1, "enabled": 1, "scheduled": 1, "health": []},
        },
    )
    monkeypatch.setattr(
        api_recovery.api_storage,
        "overview",
        lambda **_kwargs: {
            "pairs": [
                {
                    "name": "Fotos",
                    "direction": "push",
                    "source": "/secret/photos",
                    "target": "cloud:/Photos",
                    "restore_evidence": {"state": "never"},
                }
            ]
        },
    )
    recovery_pass = api_recovery.build_recovery_pass()
    assert recovery_pass["data_paths"][0]["source"] == "***REDACTED***"
    assert recovery_pass["data_paths"][0]["target"] == "***REDACTED***"
    with_paths = api_recovery.build_recovery_pass(include_paths=True)
    assert with_paths["data_paths"][0]["source"] == "/secret/photos"


def test_selection_rejects_traversal_and_deduplicates():
    assert selective_restore.normalize_selection(["docs/a.pdf", "docs/a.pdf"]) == [
        "docs/a.pdf"
    ]
    for unsafe in ("../secret", "/absolute", "a\nother"):
        try:
            selective_restore.normalize_selection([unsafe])
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe path accepted: {unsafe}")


def test_selective_restore_stages_and_verifies_without_writing_original(
    monkeypatch, tmp_path: Path
):
    database = Database(tmp_path / "recovery.db")
    config = {
        "paths": {"recovery_dir": str(tmp_path / "staging")},
        "backup": {"timeout_hours": 1},
    }
    pair = {
        "name": "Fotos",
        "local": "/live/photos",
        "remote": "cloud:/Photos",
        "direction": "push",
        "mode": "copy",
    }
    commands = []

    class Result:
        returncode = 0
        stderr = ""

        def __init__(self, stdout=""):
            self.stdout = stdout

    def fake_run(command, *, timeout):
        commands.append(command)
        if command[1] == "size":
            return Result('{"count": 1, "bytes": 12}')
        if command[1] == "copy":
            destination = Path(command[-1])
            (destination / "docs").mkdir(parents=True)
            (destination / "docs" / "a.txt").write_text("hello", encoding="utf-8")
        return Result()

    monkeypatch.setattr(selective_restore, "_run", fake_run)
    monkeypatch.setattr(selective_restore, "notify", lambda *_args, **_kwargs: None)
    job_id = database.job_start("recovery")
    result = selective_restore.run_selective_restore(
        database,
        config,
        pair,
        ["docs/a.txt"],
        max_total_mb=1,
        job_id=job_id,
    )
    assert result["status"] == "ready"
    assert result["verified"] is True
    assert Path(result["staging_path"]).is_dir()
    assert all("/live/photos" not in " ".join(command) for command in commands)
    assert commands[1][-2:] == ["cloud:/Photos", result["staging_path"]]
    assert database.job_get(job_id)["status"] == "ok"


def test_staging_cleanup_is_confined_to_recovery_root(tmp_path: Path):
    database = Database(tmp_path / "cleanup.db")
    config = {"paths": {"recovery_dir": str(tmp_path / "staging")}}
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        selective_restore.remove_staging(database, config, "../outside")
    except ValueError:
        pass
    else:
        raise AssertionError("traversal was accepted")
    assert outside.is_dir()
