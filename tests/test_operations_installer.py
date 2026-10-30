from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_operational_tick_runs_independently_of_backup_scheduler():
    service = (ROOT / "systemd/sicherpfad-operations.service").read_text(
        encoding="utf-8"
    )
    timer = (ROOT / "systemd/sicherpfad-operations.timer").read_text(encoding="utf-8")
    assert "app.operations_cli" in service
    assert "scheduler_cli" not in service
    assert "TimeoutStartSec=90" in service
    assert "NoNewPrivileges=true" in service
    assert "CapabilityBoundingSet=\n" in service
    assert "OnCalendar=*-*-* *:*:00" in timer
    assert "Persistent=true" in timer


def test_installer_backs_up_stops_restores_and_enables_operational_timer():
    source = (ROOT / "scripts/install.sh").read_text(encoding="utf-8")
    assert "OPERATIONS_TIMER_WAS_ACTIVE=0" in source
    assert (
        'restore_active_state sicherpfad-operations.timer "$OPERATIONS_TIMER_WAS_ACTIVE"'
        in source
    )
    assert (
        'restore_enabled_state sicherpfad-operations.timer "$OPERATIONS_TIMER_WAS_ENABLED"'
        in source
    )
    assert "systemctl enable --now sicherpfad-operations.timer" in source
    assert "systemctl start sicherpfad-operations.service" not in source
    for unit in ("sicherpfad-operations.service", "sicherpfad-operations.timer"):
        assert f"/etc/systemd/system/{unit}" in source
        assert f"systemd/{unit}" in (ROOT / ".github/workflows/ci.yml").read_text(
            encoding="utf-8"
        )
