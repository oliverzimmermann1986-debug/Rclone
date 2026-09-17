"""Source-level warning UI contracts; native behavior is verified in macOS CI."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
IOS = ROOT / "ios" / "RcloneMobile"


def test_status_style_distinguishes_limited_proof_from_failed_restore():
    source = (IOS / "Core" / "Formatting.swift").read_text(encoding="utf-8")
    assert 'case "warn", "warning", "partial", "stale", "overdue": .orange' in source
    assert 'case "warn", "warning": "Hinweis"' in source
    assert 'case "partial": "Prüfumfang begrenzt"' in source
    assert 'case "error", "failed", "inactive": .red' in source
    assert 'case "error", "failed": "Fehler"' in source
    assert (
        'case "warn", "warning", "partial": "exclamationmark.triangle.fill"' in source
    )


def test_status_badge_warning_is_not_only_a_color_change():
    source = (IOS / "Views" / "Components.swift").read_text(encoding="utf-8")
    assert "systemImage: StatusStyle.symbol(for: status)" in source
    assert '.accessibilityLabel("Status: \\(StatusStyle.label(for: status))")' in source
    assert "configuration.icon.font(isWarning ? .caption2 : .system(size: 7))" in source
    assert ".font(.caption.weight(.semibold))" in source


def test_warning_history_filter_and_push_notice_are_explicit():
    history = (IOS / "Views" / "BackupsView.swift").read_text(encoding="utf-8")
    system = (IOS / "Views" / "SystemView.swift").read_text(encoding="utf-8")
    assert 'Text("Hinweise").tag("warning")' in history
    assert 'Text("Fehler").tag("error")' in history
    assert 'Section("Fehler und Hinweise")' in system
    assert 'case "restore_test_warning": "Restore-Test: Prüfumfang begrenzt"' in system
    assert 'case "restore_test_error": "Restore-Test fehlgeschlagen"' in system
    assert 'event == "restore_test_warning" ? Color.orange : Color.primary' in system


def test_limited_restore_push_opens_recovery_without_requiring_a_job_id():
    source = (IOS / "Core" / "PushNotifications.swift").read_text(encoding="utf-8")
    recovery_branch = source.split('let event = userInfo["event"]', 1)[1].split(
        "if let jobID", 1
    )[0]
    assert '"restore_test_warning"' in recovery_branch
    assert "pendingRecoveryNavigation = true" in recovery_branch
    assert ".pushRecoveryNavigationRequested" in recovery_branch
