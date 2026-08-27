import threading

from app import main


class _ControlledStop:
    def __init__(self, waits_before_stop: int):
        self.waits_before_stop = waits_before_stop
        self.waits: list[float] = []

    def is_set(self) -> bool:
        return self.waits_before_stop == 0

    def wait(self, timeout: float) -> bool:
        self.waits.append(timeout)
        return len(self.waits) >= self.waits_before_stop


def test_maintenance_loop_runs_immediately_then_on_fixed_cadence(monkeypatch):
    calls: list[str] = []
    stop = _ControlledStop(waits_before_stop=3)
    monkeypatch.setattr(
        main,
        "_run_automatic_maintenance_once",
        lambda: calls.append("run"),
    )

    main._run_maintenance_loop(stop, interval_seconds=123)

    assert calls == ["run", "run", "run"]
    assert stop.waits == [123, 123, 123]


def test_maintenance_run_failure_isolated_and_stop_prevents_late_run(
    monkeypatch, caplog
):
    attempts = 0

    def failing_maintenance():
        nonlocal attempts
        attempts += 1
        raise RuntimeError("temporary failure")

    monkeypatch.setattr(main, "run_automatic_maintenance", failing_maintenance)
    main._run_automatic_maintenance_once()

    assert attempts == 1
    assert "Automatische Wartung fehlgeschlagen" in caplog.text

    already_stopped = threading.Event()
    already_stopped.set()
    main._run_maintenance_loop(already_stopped, interval_seconds=1)
    assert attempts == 1
