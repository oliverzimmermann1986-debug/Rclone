from __future__ import annotations

import re
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


class _TemplateParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []
        self.expressions: list[str] = []

    def handle_starttag(self, _tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for key, value in attrs:
            if key == "id" and value:
                self.ids.append(value)
            if value and (
                key.startswith("@") or key.startswith("x-") or key.startswith(":")
            ):
                self.expressions.append(value)


def test_main_template_has_unique_ids_and_required_navigation():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    parser = _TemplateParser()
    parser.feed(html)
    duplicates = sorted(
        name for name, count in Counter(parser.ids).items() if count > 1
    )
    assert duplicates == []
    for page in ("dashboard", "pairs", "definitions", "runs", "doctor", "settings"):
        assert f"navigate('{page}')" in html
    assert 'class="mobile-nav"' in html
    assert 'class="sidebar"' in html
    assert "setAllPairsOpen(true)" in html
    assert "setAllPairsOpen(false)" in html
    assert "Support-Bundle" in html
    assert "Konfiguration absichern" in html


def test_template_direct_calls_exist_in_alpine_component():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    javascript = (STATIC / "app.js").read_text(encoding="utf-8")
    parser = _TemplateParser()
    parser.feed(html)

    methods = set(
        re.findall(
            r"^\s{4}(?:async\s+)?([A-Za-z_$][\w$]*)\s*\([^\n]*\)\s*\{",
            javascript,
            re.MULTILINE,
        )
    )
    calls: set[str] = set()
    for expression in parser.expressions:
        calls.update(re.findall(r"(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(", expression))

    allowed = {
        "app",
        "if",
        "in",
        "confirm",
        "String",
        "Number",
        "Math",
        "Object",
        "Array",
        "Date",
        "JSON",
        "setTimeout",
        "clearTimeout",
        "encodeURIComponent",
        "parseInt",
        "parseFloat",
    }
    assert sorted(calls - methods - allowed) == []


def test_gui_assets_reference_current_cache_version():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    login = (STATIC / "login.html").read_text(encoding="utf-8")
    assert "/static/style.css?v=__APP_VERSION__" in html
    assert "/static/alpine.min.js?v=__APP_VERSION__" in html
    assert "/static/ui-helpers.js?v=__APP_VERSION__" in html
    assert "/static/app.js?v=__APP_VERSION__" in html
    assert html.index("/static/ui-helpers.js") < html.index("/static/app.js")
    assert "Proxmox Backup Console" in login
    assert 'class="shell"' in login


def test_login_uses_system_theme_and_mobile_full_frame():
    login = (STATIC / "login.html").read_text(encoding="utf-8")

    assert "color-scheme:light dark" in login
    assert "prefers-color-scheme:dark" in login
    assert "min-height:100dvh" in login
    assert (
        ".shell{display:block;width:100%;min-height:100dvh;border:0;border-radius:0;box-shadow:none}"
        in login
    )
    assert "font:inherit;font-size:16px" in login


def test_missing_resource_metrics_never_render_as_zero_percent():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    javascript = (STATIC / "app.js").read_text(encoding="utf-8")

    for path in (
        "overview.data?.system?.cpu?.load_percent",
        "overview.data?.system?.memory?.percent_used",
        "overview.data?.system?.data_disk?.percent_used",
    ):
        assert f"metricPercent({path})" in html
        assert f"metricAvailable({path})" in html
    assert (
        "return this.metricAvailable(value) ? `${Math.round(Number(value))}%` : 'Nicht verfügbar'"
        in javascript
    )
    assert "?? 0) + '%'" not in html


def test_system_health_prioritizes_alerts_over_running_state():
    javascript = (STATIC / "app.js").read_text(encoding="utf-8")
    method = javascript[
        javascript.index("    systemLevel() {") : javascript.index(
            "    systemLabel() {"
        )
    ]

    assert method.index("a.level === 'error'") < method.index("this.busy()")
    assert "['warn', 'warning'].includes(a.level)" in method


def test_scheduler_settings_route_schedules_to_jobs_and_offer_performance_presets():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    javascript = (STATIC / "app.js").read_text(encoding="utf-8")
    for text in (
        "Zeitpläne werden pro Job verwaltet",
        "Jobs öffnen",
        "Nachholfenster",
        "Leistungsprofil wählen",
    ):
        assert text in html
    for method in (
        "loadJobDefinitions",
        "runJobDefinition",
        "setPerformancePreset",
        "schedulerRiskLevel",
    ):
        assert f"{method}(" in javascript


def test_pbs_ui_has_safe_defaults_status_polling_and_job_filter():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    javascript = (STATIC / "app.js").read_text(encoding="utf-8")

    assert (
        "status: { backup: null, check: null, quicksync: null, restoretest: null, pbs: null }"
        in javascript
    )
    assert "keep: { keep_last: 0, keep_daily: 7, keep_weekly: 4" in javascript
    assert "await this.loadConfig(true)" in javascript
    assert "requestKey: 'pbs-status'" in javascript
    assert "this.status.pbs = result.running" in javascript
    assert "this.pending.pbs || this.status?.pbs" in javascript
    assert "if (this.status?.pbs || this.pbs.status?.running)" in javascript
    assert 'value="pbs">PBS-Backup' in html
    assert "['', 'backup', 'check', 'quicksync', 'restoretest', 'pbs']" in javascript
    assert "this.status?.restoretest" in javascript
    assert "if (this.status?.restoretest) return 'Restore-Drill'" in javascript
    assert 'class="table-scroll"' in html
    assert "formatDateTime(target.last_success)" in html


def test_gui_requests_are_latest_response_wins_and_poll_page_aware():
    javascript = (STATIC / "app.js").read_text(encoding="utf-8")

    assert "const requestControllers = new Map()" in javascript
    assert "const requestRevisions = new Map()" in javascript
    assert "const controller = new AbortController()" in javascript
    assert "requestControllers.get(requestKey)?.abort()" in javascript
    assert "requestRevisions.get(requestKey) !== revision" in javascript
    for key in ("jobs", "picker", "schedule-preview", "job-detail", "job-log"):
        assert f"requestKey: '{key}'" in javascript
    assert "if (this.refreshing) return false" in javascript
    assert "if (this.page === 'dashboard')" in javascript
    assert "window.setTimeout(refreshLoop, 30000)" in javascript
    assert "document.addEventListener('visibilitychange'" in javascript
    assert "if (document.hidden) this.stopPolling()" in javascript
    assert "busy ? 2000 : 10000" in javascript
    assert "window.setTimeout(activityLoop, busy ? 2000 : 10000)" in javascript
    assert "setInterval(" not in javascript
    assert "history.pushState" in javascript
    assert "window.addEventListener('popstate'" in javascript


def test_dialog_focus_accessibility_and_loading_contracts():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    javascript = (STATIC / "app.js").read_text(encoding="utf-8")
    css = (STATIC / "style.css").read_text(encoding="utf-8")

    assert '@keydown.tab.window="trapDialogFocus($event)"' in html
    for ref_name in (
        "currentPasswordDialog",
        "planDialog",
        "quickDialog",
        "pickerDialog",
        "jobDialog",
    ):
        assert f'x-ref="{ref_name}"' in html
    assert html.count('aria-modal="true"') == 5
    assert "focusableElements(dialog)" in javascript
    assert "restoreDialogFocus()" in javascript
    assert "dialogFocusStack.push(document.activeElement)" in javascript
    assert 'role="tablist"' in html
    assert 'role="tabpanel"' in html
    assert ":aria-current=" in html
    assert 'role="progressbar"' in html
    assert 'aria-label="Läufe durchsuchen"' in html
    assert 'aria-label="Datenwege nach Name oder Pfad durchsuchen"' in html
    assert ".skip-link" in css
    assert ":focus-visible" in css
    assert ".search-field:focus-within" in css
    assert ".toast.above-unsaved" in css


def test_sensitive_config_save_uses_transient_password_and_strips_web_secrets():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    javascript = (STATIC / "app.js").read_text(encoding="utf-8")

    assert "result.status === 403" in javascript
    assert "requestCurrentPassword(" in javascript
    assert "current_password: currentPassword" in javascript
    assert "this.currentPasswordDialog.password = ''" in javascript
    for key in ("password", "password_hash", "secret_key", "session_version"):
        assert f"'{key}'" in javascript
    assert "delete draft.web[key]" in javascript
    assert "localStorage.setItem('current_password'" not in javascript
    assert 'x-model="config.web.username" readonly aria-readonly="true"' in html
    assert 'autocomplete="current-password"' in html


def test_size_recalculation_is_explicit_and_measurement_age_is_visible():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    javascript = (STATIC / "app.js").read_text(encoding="utf-8")

    assert "loadStorage(true, true)" in javascript
    assert "refresh_sizes=${refreshSizes ? 'true' : 'false'}" in javascript
    assert "pairSizeAge(name, side)" in javascript
    assert "pairSizeAge(pair.name, 'source')" in html
    assert "pairSizeAge(pair.name, 'target')" in html


def test_canonical_jobs_ui_separates_data_paths_definitions_and_runs():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    javascript = (STATIC / "app.js").read_text(encoding="utf-8")

    for label in ("Lagebild", "Datenwege", "Jobs", "Läufe", "System"):
        assert f"<span>{label}</span>" in html
    assert 'x-model="pair.schedule"' not in html
    assert "delete pair.schedule" in javascript
    assert "result.backup.jobs ||= []" in javascript
    assert "'/api/jobs/definitions'" in javascript
    assert "/plan?dry_run=${dryRun}" in javascript
    assert "/run?dry_run=${dryRun}" in javascript
    assert "data_path_ids" in html
    assert "job.definition_name" in html
    assert "jobModal.job?.config_revision" in html
    assert 'min="1" max="10080" x-model.number="job.retry_minutes"' in html
    for heading in ("Lokaler Ordner", "Cloud-Ordner", "Dateien", "Größe", "Messalter"):
        assert f"<th>{heading}</th>" in html
