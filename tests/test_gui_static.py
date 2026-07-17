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
    for page in ("dashboard", "pairs", "jobs", "doctor", "settings"):
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
    assert "/static/app.js?v=__APP_VERSION__" in html
    assert "Proxmox Backup Console" in login
    assert 'class="shell"' in login


def test_scheduler_settings_explain_cron_and_offer_presets():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    javascript = (STATIC / "app.js").read_text(encoding="utf-8")
    for text in (
        "Wann sollen automatische Läufe starten?",
        "In verständlicher Form",
        "Nächste Standard-Termine",
        "Nachholfenster",
        "Leistungsprofil wählen",
    ):
        assert text in html
    for method in (
        "applyScheduleMode",
        "loadSchedulePreview",
        "setPerformancePreset",
        "schedulerRiskLevel",
    ):
        assert f"{method}(" in javascript
