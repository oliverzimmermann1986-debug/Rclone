#!/usr/bin/env python3
"""Erzeugt eine Offline-Vorschau der Oberfläche für visuelle CSS-Prüfungen.

Die Vorschau nutzt das echte Produktions-Markup aus ``index.html``, ersetzt
aber ``app.js`` durch eine Attrappe mit festen Beispieldaten. So lässt sich
das Stylesheet ohne Backend, Login und laufende Jobs begutachten — und zwar
an genau dem DOM, das später ausgeliefert wird.

    python scripts/make_ui_preview.py
    # danach app/static/_preview.html im Browser öffnen

Die erzeugten Dateien beginnen mit ``_`` und sind über .gitignore
ausgeschlossen; sie dürfen nicht ins Paket gelangen.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
SOURCE = STATIC / "index.html"
STUB = STATIC / "preview-stub.js"

# Bezeichner, die im Template vorkommen, aber echte JS-Globals sind.
JS_GLOBALS = {
    "Math",
    "JSON",
    "Object",
    "Array",
    "String",
    "Number",
    "Boolean",
    "Date",
    "console",
    "window",
    "document",
    "true",
    "false",
    "null",
    "undefined",
    "new",
    "typeof",
    "return",
    "if",
    "else",
    "in",
    "of",
    "for",
    "let",
    "const",
    "var",
    "function",
    "await",
    "async",
    "this",
}


def template_methods(html: str) -> list[str]:
    """Alle im Template aufgerufenen Methodennamen einsammeln.

    Sie werden in der Attrappe als Platzhalter ergänzt, damit eine fehlende
    Methode nicht die gesamte Alpine-Initialisierung abbricht.
    """
    expressions = " ".join(
        m.group(1)
        for m in re.finditer(r'(?:x-[a-z:.]+|:[a-z-]+|@[a-z.:-]+)="([^"]*)"', html)
    )
    names = set(re.findall(r"\b([a-zA-Z_$][\w$]*)\s*\(", expressions))
    return sorted(n for n in names if n not in JS_GLOBALS and not n.startswith("$"))


def build() -> int:
    if not SOURCE.exists() or not STUB.exists():
        print(f"Fehlt: {SOURCE if not SOURCE.exists() else STUB}", file=sys.stderr)
        return 1

    html = SOURCE.read_text(encoding="utf-8")
    methods = template_methods(html)
    (STATIC / "_stub-methods.js").write_text(
        "window.__STUB_METHODS__ = " + json.dumps(methods, ensure_ascii=False) + ";\n",
        encoding="utf-8",
    )

    html = html.replace("?v=__APP_VERSION__", "")
    html = html.replace('src="/static/alpine.min.js"', 'src="alpine.min.js"')
    html = html.replace('href="/static/style.css"', 'href="style.css"')
    html = re.sub(r'<link rel="manifest"[^>]*>', "", html)
    html = html.replace('<script src="/static/ui-helpers.js"></script>', "")
    # app.js ersetzen statt ergänzen: sonst überschreibt die echte app()
    # die Attrappe und greift auf nicht erreichbare API-Endpunkte zu.
    html = html.replace(
        '<script src="/static/app.js"></script>',
        '<script src="_stub-methods.js"></script>\n'
        '  <script src="preview-stub.js"></script>',
    )

    (STATIC / "_preview.html").write_text(html, encoding="utf-8")
    (STATIC / "_preview-dark.html").write_text(
        html.replace('data-theme="system"', 'data-theme="dark"'), encoding="utf-8"
    )
    print(f"Vorschau erzeugt ({len(methods)} Template-Methoden gestubbt):")
    print(f"  {STATIC / '_preview.html'}")
    print(f"  {STATIC / '_preview-dark.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(build())
