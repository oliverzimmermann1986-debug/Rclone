import pytest

from app import cli


def test_parser_exposes_config_recovery_commands():
    parser = cli.build_parser()
    assert parser.parse_args(["validate-config"]).func is cli.cmd_validate_config
    args = parser.parse_args(["restore-config-backup", "--yes"])
    assert args.func is cli.cmd_restore_config_backup
    assert args.yes is True


def test_db_maintenance_rejects_unsafe_ranges(capsys):
    assert (
        cli.cmd_db_maintenance(type("Args", (), {"days": 0, "keep_latest": 1})()) == 1
    )
    assert "mindestens 1" in capsys.readouterr().out
    assert (
        cli.cmd_db_maintenance(type("Args", (), {"days": 1, "keep_latest": -1})()) == 1
    )
    assert "nicht negativ" in capsys.readouterr().out


def test_restore_config_backup_uses_valid_secure_backup(tmp_path, monkeypatch):
    import yaml

    primary = tmp_path / "config.yaml"
    primary.write_text("broken: [", encoding="utf-8")
    backup = tmp_path / "config.yaml.bak"
    backup.write_text(
        yaml.safe_dump(
            {
                "web": {"username": "admin", "local_browse_roots": [str(tmp_path)]},
                "paths": {
                    "data_dir": str(tmp_path),
                    "logs_dir": str(tmp_path / "logs"),
                    "temp_dir": str(tmp_path / "temp"),
                },
                "backup": {"pairs": [], "default_schedule": "manual"},
                "notifications": {"webhooks": []},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("RCLONE_SYNC_CONFIG", str(primary))

    assert cli.cmd_restore_config_backup(type("Args", (), {"yes": True})()) == 0
    restored = yaml.safe_load(primary.read_text(encoding="utf-8"))
    assert restored["web"]["username"] == "admin"
    assert restored["schema_version"] == 2
    assert restored["backup"]["enabled"] is True
    assert list(tmp_path.glob("config.yaml.invalid-*"))


def test_restore_config_backup_rejects_symlink(tmp_path, monkeypatch):
    primary = tmp_path / "config.yaml"
    primary.write_text("value: 1\n", encoding="utf-8")
    external = tmp_path / "external.yaml"
    external.write_text("value: 2\n", encoding="utf-8")
    try:
        (tmp_path / "config.yaml.bak").symlink_to(external)
    except OSError:
        pytest.skip("Symlinks require Windows Developer Mode or elevated privileges")
    monkeypatch.setenv("RCLONE_SYNC_CONFIG", str(primary))

    assert cli.cmd_restore_config_backup(type("Args", (), {"yes": True})()) == 1
    assert primary.read_text(encoding="utf-8") == "value: 1\n"
