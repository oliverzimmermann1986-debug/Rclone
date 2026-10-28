"""Static publication contracts; native simulator behavior is verified on macOS."""

from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DETAILS = (
    "Oliver Zimmermann",
    "c/o COCENTER",
    "Koppoldstr. 1",
    "86551 Aichach",
    "Deutschland",
    "impressum@zimlab.org",
)
PUBLIC_IMPRINT = "https://oliverzimmermann1986-debug.github.io/Rclone/impressum.html"
PUBLIC_SUPPORT = "https://support.zimlab.org/?module=sicherpfad"


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.hrefs.append(dict(attrs).get("href", ""))


def links_from(path):
    parser = Links()
    parser.feed(path.read_text(encoding="utf-8"))
    return parser.hrefs


def test_public_imprint_contains_approved_contact_and_no_scripts():
    page = (ROOT / "docs/impressum.html").read_text(encoding="utf-8")
    assert all(detail in page for detail in DETAILS)
    assert "<script" not in page.lower()
    assert "mailto:impressum@zimlab.org" in links_from(ROOT / "docs/impressum.html")


def test_all_public_pages_link_to_existing_imprint():
    for name in ("index.html", "datenschutz.html"):
        assert "impressum.html" in links_from(ROOT / "docs" / name)
    for page in (ROOT / "docs").glob("*.html"):
        for href in links_from(page):
            if href and ":" not in href and not href.startswith("#"):
                assert (page.parent / href).is_file(), (page.name, href)


def test_web_login_and_dashboard_have_public_imprint_link():
    for name in ("login.html", "index.html"):
        assert PUBLIC_IMPRINT in links_from(ROOT / "app/static" / name)
        assert PUBLIC_SUPPORT in links_from(ROOT / "app/static" / name)


def test_public_support_links_use_the_central_module_form():
    for name in ("index.html", "datenschutz.html", "impressum.html"):
        path = ROOT / "docs" / name
        assert PUBLIC_SUPPORT in links_from(path)
        assert (
            "https://github.com/oliverzimmermann1986-debug/Rclone/issues"
            not in path.read_text(encoding="utf-8")
        )
    metadata = (ROOT / "ios/APP_STORE_METADATA.md").read_text(encoding="utf-8")
    assert f"- Support-URL: `{PUBLIC_SUPPORT}`" in metadata


def test_native_imprint_is_bundled_and_reachable_before_and_after_login():
    views = ROOT / "ios/RcloneMobile/Views"
    legal = (views / "LegalInformationView.swift").read_text(encoding="utf-8")
    login = (views / "LoginView.swift").read_text(encoding="utf-8")
    system = (views / "SystemView.swift").read_text(encoding="utf-8")
    assert all(detail in legal for detail in DETAILS)
    assert PUBLIC_SUPPORT in legal
    assert ".textSelection(.enabled)" in legal
    assert "@EnvironmentObject" not in legal
    assert 'Button("Impressum")' in login
    assert (
        ".sheet(isPresented: $showingLegalInformation) { LegalInformationSheet() }"
        in login
    )
    assert "NavigationLink { LegalInformationView() }" in system
    assert 'Label("Impressum", systemImage: "info.circle")' in system
