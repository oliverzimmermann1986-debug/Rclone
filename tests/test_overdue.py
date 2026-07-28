import app.overdue as overdue_module
from app.db import Database
from app.overdue import (
    alert_settings,
    check_and_notify,
    evaluate_pair,
    is_scheduled,
    notify_overdue,
)


class _Cfg:
    """Minimaler Config-Ersatz mit der get(section, key, default=...)-Signatur."""

    def __init__(self, data):
        self._data = data

    def get(self, *path, default=None):
        node = self._data
        for key in path:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node


def _cfg(pairs=None, **overdue_alerts):
    backup = {"pairs": pairs or []}
    if overdue_alerts:
        backup["overdue_alerts"] = overdue_alerts
    return _Cfg({"backup": backup})


class _Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, event, title, message, **extra):
        self.calls.append({"event": event, "title": title, "message": message, **extra})


def _patch_notify(monkeypatch):
    recorder = _Recorder()
    monkeypatch.setattr("app.notifications.notify", recorder)
    return recorder


def test_evaluate_pair_disabled_when_no_limit():
    verdict = evaluate_pair({"max_success_age_hours": 0}, None, now=1000.0)
    assert verdict["overdue"] is False
    assert verdict["max_success_age_hours"] == 0


def test_evaluate_pair_never_succeeded_is_overdue():
    verdict = evaluate_pair({"max_success_age_hours": 48}, None, now=1000.0)
    assert verdict["overdue"] is True
    assert verdict["success_age_hours"] is None


def test_evaluate_pair_respects_limit():
    now = 100_000.0
    fresh = evaluate_pair({"max_success_age_hours": 48}, now - 47 * 3600, now=now)
    stale = evaluate_pair({"max_success_age_hours": 48}, now - 49 * 3600, now=now)
    assert fresh["overdue"] is False
    assert stale["overdue"] is True
    assert stale["success_age_hours"] == 49.0


def test_evaluate_pair_survives_garbage_limit():
    verdict = evaluate_pair({"max_success_age_hours": "später"}, None, now=1.0)
    assert verdict["overdue"] is False


def test_alert_settings_defaults_and_clamping():
    assert alert_settings(_cfg()) == {"enabled": True, "repeat_hours": 24}
    clamped = alert_settings(_cfg(enabled=False, repeat_hours=99_999))
    assert clamped == {"enabled": False, "repeat_hours": 720}


def test_notify_overdue_debounces_until_repeat_window(tmp_path, monkeypatch):
    recorder = _patch_notify(monkeypatch)
    db = Database(tmp_path / "app.db")
    cfg = _cfg(repeat_hours=24)
    item = {
        "name": "archiv",
        "history_key": "rclone:id:a1",
        "success_age_hours": 72.0,
        "max_success_age_hours": 48.0,
    }

    assert notify_overdue(cfg, db, [item], now=1_000_000.0) == ["archiv"]
    # Innerhalb des Wiederholfensters bleibt es still.
    assert notify_overdue(cfg, db, [item], now=1_000_000.0 + 3600) == []
    # Danach erneut.
    assert notify_overdue(cfg, db, [item], now=1_000_000.0 + 25 * 3600) == ["archiv"]
    assert len(recorder.calls) == 2
    assert recorder.calls[0]["event"] == "pair_overdue"
    assert recorder.calls[0]["pairs"] == ["archiv"]
    assert "72" in recorder.calls[0]["message"]


def test_notify_overdue_resets_after_recovery(tmp_path, monkeypatch):
    _patch_notify(monkeypatch)
    db = Database(tmp_path / "app.db")
    cfg = _cfg(repeat_hours=24)
    item = {"name": "archiv", "history_key": "k1", "max_success_age_hours": 48.0}

    assert notify_overdue(cfg, db, [item], now=1000.0) == ["archiv"]
    # Pair läuft wieder: Zustand wird geräumt.
    assert notify_overdue(cfg, db, [], now=2000.0) == []
    # Nächster Ausfall meldet sofort, nicht erst nach repeat_hours.
    assert notify_overdue(cfg, db, [item], now=3000.0) == ["archiv"]


def test_notify_overdue_disabled_sends_nothing(tmp_path, monkeypatch):
    recorder = _patch_notify(monkeypatch)
    db = Database(tmp_path / "app.db")
    cfg = _cfg(enabled=False)
    item = {"name": "archiv", "history_key": "k1", "max_success_age_hours": 48.0}
    assert notify_overdue(cfg, db, [item], now=1000.0) == []
    assert recorder.calls == []


def test_notify_overdue_state_is_bounded(tmp_path, monkeypatch):
    _patch_notify(monkeypatch)
    db = Database(tmp_path / "app.db")
    cfg = _cfg(repeat_hours=1)
    items = [
        {"name": f"pair{i}", "history_key": f"k{i}", "max_success_age_hours": 1.0}
        for i in range(overdue_module._MAX_TRACKED + 50)
    ]
    notify_overdue(cfg, db, items, now=1000.0)
    stored = db.runtime_get(overdue_module._STATE_KEY, {})
    assert len(stored) == overdue_module._MAX_TRACKED


def test_check_and_notify_reads_history(tmp_path, monkeypatch):
    recorder = _patch_notify(monkeypatch)
    db = Database(tmp_path / "app.db")
    now = 1_000_000.0

    job_id = db.job_start("backup")
    db.job_finish(
        job_id,
        "ok",
        {
            "ok": True,
            "pairs": [{"name": "frisch", "ok": True}],
            "history_keys": {"frisch": "rclone:id:fresh"},
        },
    )
    # job_finish stempelt ended_at auf "jetzt"; für die Altersrechnung
    # zurückdatieren.
    with db.conn() as connection:
        connection.execute(
            "UPDATE pair_runs SET started_at=?, ended_at=?", (now - 7200, now - 3600)
        )

    cfg = _cfg(
        pairs=[
            {
                "name": "frisch",
                "id": "fresh",
                "enabled": True,
                "schedule": "0 2 * * *",
                "max_success_age_hours": 48,
            },
            {
                "name": "nie",
                "id": "never",
                "enabled": True,
                "schedule": "0 2 * * *",
                "max_success_age_hours": 48,
            },
            {
                "name": "ohne-frist",
                "id": "nolimit",
                "enabled": True,
                "schedule": "0 2 * * *",
                "max_success_age_hours": 0,
            },
            {
                "name": "aus",
                "id": "off",
                "enabled": False,
                "schedule": "0 2 * * *",
                "max_success_age_hours": 1,
            },
            {
                "name": "manuell",
                "id": "man",
                "enabled": True,
                "schedule": "manual",
                "max_success_age_hours": 1,
            },
        ]
    )

    reported = check_and_notify(cfg, db, now=now)
    assert reported == ["nie"]
    assert len(recorder.calls) == 1
    assert "nie" in recorder.calls[0]["message"]


def test_is_scheduled_excludes_manual_pairs():
    assert is_scheduled({"schedule": "0 2 * * *"}, "") is True
    assert is_scheduled({"schedule": "manual"}, "0 3 * * *") is False
    assert is_scheduled({"schedule": ""}, "0 3 * * *") is True
    assert is_scheduled({"schedule": ""}, "") is False
    assert is_scheduled({"schedule": "  OFF  "}, "") is False


def test_check_and_notify_skips_manual_pair(tmp_path, monkeypatch):
    recorder = _patch_notify(monkeypatch)
    db = Database(tmp_path / "app.db")
    cfg = _cfg(
        pairs=[
            {
                "name": "adhoc",
                "id": "adhoc",
                "enabled": True,
                "schedule": "manual",
                "max_success_age_hours": 1,
            }
        ]
    )
    assert check_and_notify(cfg, db, now=1_000_000.0) == []
    assert recorder.calls == []
