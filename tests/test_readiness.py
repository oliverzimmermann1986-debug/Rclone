import json
from pathlib import Path

from app import main
from app.db import Database


class _Config:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir

    def get(self, *keys, default=None):
        if keys == ("paths",):
            return {"data_dir": str(self.data_dir)}
        if keys == ("web",):
            return {
                "allowed_hosts": ["testserver"],
                "secure_cookie": True,
                "hsts_seconds": 3600,
            }
        return default


def _body(response) -> dict:
    return json.loads(response.body)


def test_readyz_does_not_create_a_missing_database(tmp_path: Path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    missing = data_dir / "missing.db"
    monkeypatch.setattr(main, "get_config", lambda: _Config(data_dir))
    monkeypatch.setattr(main, "database_path", lambda: missing)

    response = main.readyz()

    assert response.status_code == 503
    assert _body(response)["ok"] is False
    assert not missing.exists()


def test_readyz_fails_closed_for_replaced_non_sqlite_file(tmp_path: Path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    replaced = data_dir / "rclone-sync.db"
    replaced.write_text("not sqlite", encoding="utf-8")
    monkeypatch.setattr(main, "get_config", lambda: _Config(data_dir))
    monkeypatch.setattr(main, "database_path", lambda: replaced)

    response = main.readyz()

    assert response.status_code == 503
    assert _body(response)["ok"] is False
    assert replaced.read_text(encoding="utf-8") == "not sqlite"


def test_readyz_accepts_existing_initialized_database(tmp_path: Path, monkeypatch):
    data_dir = tmp_path / "data"
    database = Database(data_dir / "rclone-sync.db")
    monkeypatch.setattr(main, "get_config", lambda: _Config(data_dir))
    monkeypatch.setattr(main, "database_path", lambda: database.path)

    response = main.readyz()

    assert response.status_code == 200
    assert _body(response)["ok"] is True
