import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install.sh"


def test_installer_preserves_failure_status_and_uses_consistent_db_backup():
    script = INSTALLER.read_text(encoding="utf-8")

    assert 'trap \'on_error "$LINENO" "$?"\' ERR' in script
    assert 'local status="${2:-1}"' in script
    assert 'exit "$status"' in script
    assert (
        "systemctl stop sync-scheduler.timer rclone-sync.timer "
        "sync-scheduler.service rclone-sync-web.service rclone-sync.service"
    ) in script
    assert (
        "for unit in sync-scheduler.timer rclone-sync.timer "
        "sync-scheduler.service rclone-sync-web.service rclone-sync.service"
    ) in script
    assert 'systemctl is-active --quiet "$unit"' in script
    assert "\".backup '$sqlite_backup'\"" in script
    assert 'sqlite3 "$sqlite_backup" "PRAGMA quick_check;"' in script


def test_installer_uses_pinned_requirements_and_services_have_safe_runtime_defaults():
    script = INSTALLER.read_text(encoding="utf-8")
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    web_service = (ROOT / "systemd" / "rclone-sync-web.service").read_text(
        encoding="utf-8"
    )
    backup_service = (ROOT / "systemd" / "rclone-sync.service").read_text(
        encoding="utf-8"
    )
    scheduler_service = (ROOT / "systemd" / "sync-scheduler.service").read_text(
        encoding="utf-8"
    )

    assert '-r "$APP_DIR/requirements.txt"' in script
    assert "--upgrade pip wheel" not in script
    assert "uvicorn[standard]==" in requirements
    assert "uvloop==" in requirements
    for line in requirements.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            assert "==" in line, f"ungepinnte Laufzeitabhängigkeit: {line}"
    assert "--host 127.0.0.1" in web_service
    assert "TimeoutStartSec=infinity" in backup_service
    assert "TimeoutStartSec=infinity" in scheduler_service


def test_installer_fails_closed_for_source_and_backup_paths():
    script = INSTALLER.read_text(encoding="utf-8")

    assert "https://github.com/oliverzimmermann1986-debug/Rclone.git" in script
    assert '[[ -e "$APP_DIR" || -L "$APP_DIR" ]]' in script
    assert '[[ ! -d "$APP_DIR/.git" ]]' in script
    assert 'rm -rf "$APP_DIR"' not in script
    assert "ALLOW_DIRTY_UPGRADE" not in script
    assert '[[ ! "$SOURCE_COMMIT" =~ ^[0-9a-fA-F]{40}$ ]]' in script
    assert 'checkout --detach "$SOURCE_COMMIT"' in script
    assert '"$CHECKED_OUT_COMMIT" != "$SOURCE_COMMIT"' in script
    assert "merge-base --is-ancestor" in script
    assert "pull --ff-only" not in script
    assert 'git -c safe.directory="$APP_DIR" -C "$APP_DIR" status' in script
    assert 'case "$BACKUP_ROOT_CANONICAL" in' in script
    assert '"$APP_DIR_CANONICAL"|"$APP_DIR_CANONICAL"/*)' in script
    assert 'BACKUP_MARKER=".rclone-sync-backup-v1"' in script
    assert re.search(r"\^\[0-9\]\{8\}-\[0-9\]\{6\}\$", script)
    assert '[[ ! -L "$BACKUP_ROOT/$old/$BACKUP_MARKER" ]]' in script
    assert 'rm -rf -- "${BACKUP_ROOT:?}/$old"' in script


def test_installer_persists_normalized_config_and_prepares_rclone_directory():
    script = INSTALLER.read_text(encoding="utf-8")

    assert 'sudo -u "$APP_USER" -H env RCLONE_SYNC_CONFIG=' in script
    assert "normalized, warnings = validate_config(config.snapshot())" in script
    assert "config.replace(normalized)" in script
    assert '"/home/$APP_USER/.config/rclone"' in script
    assert 'install -d -m 0700 -o "$APP_USER" -g "$APP_GROUP"' in script


def test_installer_rollback_restores_unit_states_without_starting_scheduler_oneshot():
    script = INSTALLER.read_text(encoding="utf-8")

    assert "WEB_WAS_ENABLED=0" in script
    assert "SCHEDULER_TIMER_WAS_ENABLED=0" in script
    assert "LEGACY_TIMER_WAS_ENABLED=0" in script
    assert "restore_enabled_state rclone-sync-web.service" in script
    assert "restore_enabled_state sync-scheduler.timer" in script
    assert "restore_enabled_state rclone-sync.timer" in script
    assert "restore_active_state sync-scheduler.service" not in script
    assert "systemctl start sync-scheduler.service" not in script
    assert "/etc/systemd/system/rclone-sync.timer" in script
    assert "/etc/systemd/system/sync-scheduler.timer" in script


def test_installer_rollback_restores_normalized_config_and_migrated_database():
    script = INSTALLER.read_text(encoding="utf-8")

    for name in ("config.yaml", "config.yaml.bak", "rclone-sync.db"):
        assert name in script
        assert f'"$backup_dir/runtime-state/$name.present"' in script
        assert f'"$backup_dir/runtime-state/$name.missing"' in script
    assert "restore_runtime_backup || rollback_failed=1" in script
    assert '[[ "$(< "$candidate/source-path.txt")" == "$APP_DIR_CANONICAL" ]]' in script
    assert '[[ -d "$candidate" && ! -L "$candidate" ]]' in script
    assert '[[ -f "$source" && ! -L "$source" ]]' in script
    assert 'sqlite3 "$source" "PRAGMA quick_check;"' in script
    assert '"$APP_DIR_CANONICAL/data/rclone-sync.db-wal"' in script
    assert '"$APP_DIR_CANONICAL/data/rclone-sync.db-shm"' in script
    assert 'rm -rf "$APP_DIR/data"' not in script
    assert script.index("restore_runtime_backup || rollback_failed=1") < script.index(
        'restore_active_state rclone-sync-web.service "$WEB_WAS_ACTIVE"'
    )
    assert 'if (( rollback_failed == 0 )); then' in script


def test_ci_covers_supported_python_dependencies_ui_and_systemd_units():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert 'python-version: ["3.10", "3.11", "3.12", "3.13"]' in workflow
    assert "actions/checkout@v7" in workflow
    assert "actions/setup-python@v7" in workflow
    assert "-r requirements-dev.txt" in workflow
    assert "python -m pip check" in workflow
    assert "node --check app/static/app.js" in workflow
    for unit in (
        "rclone-sync-web.service",
        "rclone-sync.service",
        "rclone-sync.timer",
        "sync-scheduler.service",
        "sync-scheduler.timer",
    ):
        assert f"systemd/{unit}" in workflow
