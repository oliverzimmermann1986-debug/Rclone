from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_ios_uses_server_origin_for_generic_self_hosted_webauthn():
    browser = (
        ROOT / "ios" / "RcloneMobile" / "Core" / "WebAuthnBrowserSession.swift"
    ).read_text(encoding="utf-8")
    api = (ROOT / "ios" / "RcloneMobile" / "Core" / "APIClient.swift").read_text(
        encoding="utf-8"
    )
    plist = (ROOT / "ios" / "RcloneMobile" / "Info.plist").read_text(encoding="utf-8")

    assert "ASWebAuthenticationSession" in browser
    assert 'callbackURLScheme: "rclonesync"' in browser
    assert 'baseURL.scheme?.lowercased() == "https"' in browser
    assert "SHA256.hash" in browser
    assert 'URLQueryItem(name: "app_challenge"' in browser
    assert "/webauthn/native" in browser
    assert "/api/webauthn/native/exchange" in api
    assert "exchangeWebAuthnToken" in api
    assert "verifier: String" in api
    assert "<string>rclonesync</string>" in plist


def test_login_exposes_passkey_and_physical_security_key_separately():
    login = (ROOT / "ios" / "RcloneMobile" / "Views" / "LoginView.swift").read_text(
        encoding="utf-8"
    )
    assert "Mit Passkey anmelden" in login
    assert "Mit Sicherheitsschlüssel" in login
    assert 'performWebAuthn(method: "passkey")' in login
    assert 'performWebAuthn(method: "security_key")' in login
