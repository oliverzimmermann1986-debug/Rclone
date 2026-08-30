from __future__ import annotations

import re
import struct
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


def _png_dimensions(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert data[12:16] == b"IHDR"
    return struct.unpack(">II", data[16:24])


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
    main_source = (STATIC.parent / "main.py").read_text(encoding="utf-8")
    assert "/static/style.css?v=__APP_VERSION__" in html
    assert "/static/alpine.min.js?v=__APP_VERSION__" in html
    assert "/static/ui-helpers.js?v=__APP_VERSION__" in html
    assert "/static/app.js?v=__APP_VERSION__" in html
    assert html.index("/static/ui-helpers.js") < html.index("/static/app.js")
    assert (
        'html = html.replace("?v=__APP_VERSION__", f"?v={__version__}")' in main_source
    )
    assert "Verifizierte Backup-Leitstelle" in login
    assert "Sicherpfad" in html
    assert "rclone-sync</strong>" not in html
    assert "rclone-sync</strong>" not in login
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


def test_target_picker_can_switch_storage_and_create_folders():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    javascript = (STATIC / "app.js").read_text(encoding="utf-8")

    assert 'aria-label="Zieltyp auswählen"' in html
    assert "switchPickerKind('remote')" in html
    assert "switchPickerKind('local')" in html
    assert 'aria-label="Name des neuen Ordners"' in html
    assert "createPickerFolder()" in html
    assert "pickerCanSwitchKind()" in javascript
    assert "'/api/browse/directory'" in javascript
    assert "kind: this.picker.kind" in javascript


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
    assert 'class="table-scroll responsive-table-wrap"' in html
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
    assert "polling: { active: false, generation: 0" in javascript
    assert (
        "const isCurrent = () => this.polling.active && this.polling.generation === generation"
        in javascript
    )
    assert "busy ? 2000 : 10000" in javascript
    assert "window.setTimeout(activityLoop, busy ? 2000 : 10000)" in javascript
    assert "setInterval(" not in javascript
    assert "history.pushState" in javascript
    assert "window.addEventListener('popstate'" in javascript


def test_config_save_preserves_edits_made_while_request_is_running():
    javascript = (STATIC / "app.js").read_text(encoding="utf-8")

    assert "configEditGeneration: 0" in javascript
    assert "const savedGeneration = this.configEditGeneration" in javascript
    assert (
        "const editedDuringSave = this.configEditGeneration !== savedGeneration"
        in javascript
    )
    assert "neuere Änderungen sind noch ungespeichert" in javascript
    assert (
        "if (result.config?._revision) this.config._revision = result.config._revision"
        in javascript
    )
    assert "this.configEditGeneration += 1" in javascript


def test_dialog_focus_accessibility_and_loading_contracts():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    javascript = (STATIC / "app.js").read_text(encoding="utf-8")
    css = (STATIC / "style.css").read_text(encoding="utf-8")

    assert '@keydown.tab.window="trapDialogFocus($event)"' in html
    assert '@focusin.window="enforceActiveDialogFocus($event)"' in html
    for ref_name in (
        "confirmationDialog",
        "currentPasswordDialog",
        "planDialog",
        "quickDialog",
        "pickerDialog",
        "jobDialog",
    ):
        assert f'x-ref="{ref_name}"' in html
    assert html.count('aria-modal="true"') == 6
    assert "focusableElements(dialog)" in javascript
    assert "restoreDialogFocus()" in javascript
    assert "dialogFocusStack.push(document.activeElement)" in javascript
    assert "const DIALOG_FOCUS_RETRY_MS = 50" in javascript
    assert "const generation = ++dialogFocusGeneration" in javascript
    assert "this.focusOpenedDialog(refName);" in javascript
    assert "if (generation !== dialogFocusGeneration) return" in javascript
    assert "}, DIALOG_FOCUS_RETRY_MS)" in javascript
    assert "dialogFocusGeneration += 1" in javascript
    assert "window.requestAnimationFrame" not in javascript
    assert (
        "const initialTarget = dialog.querySelector('[data-dialog-initial-focus]')"
        in javascript
    )
    assert (
        "if (initialTarget && !this.visibleElement(initialTarget)) return false"
        in javascript
    )
    assert (
        "const focusIsOutside = !dialog.contains(document.activeElement)" in javascript
    )
    assert (
        "const focusIsNotInitial = document.activeElement !== initialTarget"
        in javascript
    )
    assert "if (focusIsOutside || focusIsNotInitial) initialTarget.focus" in javascript
    assert "enforceActiveDialogFocus(event)" in javascript
    assert (
        "if (eventTarget?.nodeType && dialog.contains(eventTarget)) return true"
        in javascript
    )
    assert "this.visibleElement(initialTarget)" in javascript
    assert "this.focusableElements(dialog)[0] || dialog" in javascript
    assert "element.getClientRects().length === 0" in javascript
    assert "return dialog.contains(document.activeElement)" in javascript
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


def test_confirmation_navigation_and_pair_actions_are_accessible():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    javascript = (STATIC / "app.js").read_text(encoding="utf-8")

    assert "confirm(" not in html
    assert "confirm(" not in javascript
    assert "requestConfirmation(message, options = {})" in javascript
    assert "respondConfirmation(confirmed)" in javascript
    assert "if (this.confirmationDialog.show)" in javascript
    assert 'aria-describedby="confirmation-dialog-message"' in html
    assert "data-dialog-initial-focus>Abbrechen" in html
    assert "navigationFocusReturn = document.activeElement" in javascript
    assert "this.focusableElements(this.$refs.mainSidebar)[0]?.focus()" in javascript
    assert "this.navigationIsModal() ? this.$refs.mainSidebar : null" in javascript
    assert 'x-ref="mainSidebar"' in html
    assert ':inert="navigationIsModal()"' in html
    assert "document.getElementById('main-content')?.focus" in javascript
    assert (
        "target?.isConnected ? target : document.getElementById('main-content')"
        in javascript
    )
    assert 'role="status" aria-live="polite" aria-atomic="true"' in html
    assert "selectedPairNames().join(', ')" in html
    assert "dataPathName(pathId) + ' in Job '" in html
    assert "(pair.name || 'ohne Namen') + ' nach oben verschieben'" in html


def test_mobile_tables_touch_targets_and_dialog_safe_areas_are_hardened():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    css = (STATIC / "style.css").read_text(encoding="utf-8")

    assert html.count('class="table responsive-table"') >= 2
    assert 'class="table copy-table responsive-table"' in html
    for label in (
        "Datenweg",
        "Lokaler Ordner",
        "Cloud-Ordner",
        "Dateien",
        "Größe",
        "Messalter",
    ):
        assert f'data-label="{label}"' in html
    assert ".responsive-table td::before" in css
    assert "@media (pointer: coarse)" in css
    assert "min-height: 44px" in css
    assert "max-height: calc(100dvh" in css
    assert "env(safe-area-inset-bottom)" in css


def test_pwa_caches_only_versioned_static_allowlist_and_has_install_icon():
    manifest = (STATIC / "manifest.json").read_text(encoding="utf-8")
    service_worker = (STATIC / "sw.js").read_text(encoding="utf-8")

    assert '"name": "Sicherpfad Backup-Leitstelle"' in manifest
    assert '"short_name": "Sicherpfad"' in manifest

    for size in (192, 512, 1024):
        icon = STATIC / f"app-icon-{size}.png"
        assert icon.is_file()
        assert _png_dimensions(icon) == (size, size)
        assert f'"src": "/static/app-icon-{size}.png"' in manifest
        assert f'"sizes": "{size}x{size}"' in manifest
        assert f"'/static/app-icon-{size}.png'" in service_worker
    assert '"sizes": "1024x1024"' in manifest
    assert '"purpose": "any"' in manifest
    assert "const CACHE_PREFIX = 'rclone-sync-static-'" in service_worker
    assert "const CACHE_NAME = `${CACHE_PREFIX}v3`" in service_worker
    assert "const STATIC_ASSETS = new Set([" in service_worker
    assert "new Request(path, { cache: 'reload' })" in service_worker
    for forbidden in ("'/api/", "'/login", "'/logout", "'/'"):
        assert (
            forbidden
            not in service_worker.split("const STATIC_ASSETS", 1)[1].split("]);", 1)[0]
        )
    assert "if (!STATIC_ASSETS.has(url.pathname)) return null" in service_worker
    assert "if (event.request.method !== 'GET') return" in service_worker
    assert "name.startsWith(CACHE_PREFIX) && name !== CACHE_NAME" in service_worker
    fetch_handler = service_worker.split("self.addEventListener('fetch'", 1)[1]
    assert "new Request(event.request, { cache: 'no-cache' })" in fetch_handler
    assert fetch_handler.index("await fetch(request)") < fetch_handler.index(
        "await cache.match(path)"
    )
    assert "if (!response.ok)" in fetch_handler
    assert "return (await cache.match(path)) || response" in fetch_handler
    assert "if (response.type === 'basic')" in fetch_handler
    assert "await cache.put(path, response.clone())" in fetch_handler
    assert "catch (error)" in fetch_handler
    assert "if (cached) return cached" in fetch_handler
    assert "throw error" in fetch_handler
    assert not (STATIC / "preview-stub.js").exists()


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


def test_storage_dashboard_distinguishes_initial_partial_and_total_failures():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    javascript = (STATIC / "app.js").read_text(encoding="utf-8")

    assert "storageState: 'loading'" in javascript
    assert "storageState = hadPairs ? 'stale' : 'failed'" in javascript
    assert "['loaded', 'partial', 'failed', 'stale']" in javascript
    assert "captureError: true" in javascript
    assert "requestKey: 'storage-overview'" in javascript
    assert "timeoutMs: includeRemote ? 70000 : 30000" in javascript
    assert "const previous = new Map(this.storagePairs()" in javascript
    assert "source_size: pair.source_size || old.source_size" in javascript
    assert (
        "`${summary.loaded || 0}/${summary.total || 0} Messungen erfolgreich`"
        in javascript
    )
    assert "copySideError(entry, 'local')" in html
    assert "copySideError(entry, 'cloud')" in html
    assert "pairSizeError(pair.name, 'source')" in html
    assert "pairSizeError(pair.name, 'target')" in html
    assert "Erneut messen" in html
    assert "storageState === 'loaded' && !storagePairs().length" in html
    assert "!storageLoading && !storagePairs().length" not in html


def test_webhook_management_is_not_exposed_in_the_user_interface():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    javascript = (STATIC / "app.js").read_text(encoding="utf-8")

    assert "settings-tab-notifications" not in html
    assert "settings-panel-notifications" not in html
    assert "Webhook" not in html
    assert "addWebhook()" not in javascript
    assert "testWebhook(" not in javascript
    assert "delete result.notifications.webhooks" in javascript
    assert "result.notifications.webhooks ||= []" not in javascript


def test_push_delivery_status_and_test_are_visible_without_webhooks():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    javascript = (STATIC / "app.js").read_text(encoding="utf-8")

    assert "Push-Mitteilungen" in html
    assert "push.status?.outbox?.pending" in html
    assert "push.status?.outbox?.failed" in html
    assert "Testmitteilung" in html
    assert "loadPushStatus(true)" in javascript
    assert "'/api/push/status'" in javascript
    assert "'/api/push/test'" in javascript


def test_failed_job_retry_is_revision_bound_and_visible_in_run_detail():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    javascript = (STATIC / "app.js").read_text(encoding="utf-8")

    assert "canRetryJob(jobModal.job)" in html
    assert "Job erneut starten" in html
    assert "job?.definition_id" in javascript
    assert "job?.config_revision" in javascript
    assert "`/api/jobs/${job.id}/retry?dry_run=false`" in javascript
    assert "unverändert" in javascript


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
