from app import notifications, push_notifications


def test_unknown_notification_event_is_ignored(monkeypatch):
    calls = []
    monkeypatch.setattr(
        push_notifications,
        "queue_push_notification",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    notifications.notify("unknown", "Titel", "Text")

    assert calls == []


def test_notification_is_persisted_with_navigation_context(monkeypatch):
    calls = []

    def queue(event, title, message, *, extra):
        calls.append((event, title, message, extra))
        return {"queued": 1}

    monkeypatch.setattr(push_notifications, "queue_push_notification", queue)

    notifications.notify(
        "sync_error",
        "Fehler",
        "Fotos fehlgeschlagen",
        summary={"job_id": 42, "run_id": "run-42"},
    )

    assert calls == [
        (
            "sync_error",
            "Fehler",
            "Fotos fehlgeschlagen",
            {"summary": {"job_id": 42, "run_id": "run-42"}},
        )
    ]


def test_notification_queue_failure_does_not_fail_backup(monkeypatch):
    def fail(*_args, **_kwargs):
        raise OSError("database unavailable")

    monkeypatch.setattr(push_notifications, "queue_push_notification", fail)

    notifications.notify("sync_error", "Fehler", "Test")
