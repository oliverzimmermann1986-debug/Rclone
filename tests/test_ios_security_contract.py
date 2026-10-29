"""Source gates only; native behavior is covered by the accompanying XCTest cases."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
IOS = ROOT / "ios" / "RcloneMobile"


def _read(relative):
    return (IOS / relative).read_text(encoding="utf-8")


def _between(source, start, end):
    return source.split(start, 1)[1].split(end, 1)[0]


def test_client_loads_only_https_and_removes_legacy_http_before_requests():
    api = _read("Core/APIClient.swift")
    initializer = _between(
        api,
        "        self.decoder = JSONDecoder()",
        "    static func normalizedServerURL",
    )
    policy = initializer.split('if baseURL.scheme?.lowercased() == "https"', 1)[1]
    https, http = policy.split("} else {", 1)
    assert "self.sessionPersistence?.load(for: baseURL)" in https
    assert "restored.forEach(cookieStorage.setCookie)" in https
    assert "persistedSessionValue = restored.first" in https
    assert "clearCookies()" in http
    assert "sessionPersistence?.remove(for: baseURL)" in http
    assert "load(" not in http
    clear = _between(api, "private func clearCookies()", "private var origin:")
    assert "sessionPersistence?.remove(for: baseURL, matchingSession: value)" in clear
    assert "sessionPersistence?.remove(for: baseURL)" not in clear
    assert "appCookieNames.contains($0.name)" in clear


def test_persistence_checks_base_url_and_cleans_http_even_when_opted_out():
    api = _read("Core/APIClient.swift")
    persist = _between(api, "private func persistSession()", "private func get<")
    guard, rest = persist.split("guard remembersSession", 1)
    assert 'guard baseURL.scheme?.lowercased() == "https" else' in guard
    assert "sessionPersistence?.remove(for: baseURL)" in guard
    assert "return" in guard
    assert (
        "sessionPersistence?.save(cookies, for: baseURL, replacingSession: persistedSessionValue) == true"
        in rest
    )
    assert "persistedSessionValue = value" in rest
    assert "names.contains($0.name)" in rest
    assert "url.scheme" not in persist


def test_downloads_share_the_private_export_writer_without_overwrite():
    api = _read("Core/APIClient.swift")
    get = _between(api, "private func download(", "private func postDownload<")
    post = _between(
        api, "private func postDownload<", "static func sanitizedExportFilename"
    )
    for method in [get, post]:
        assert "let epoch = exportEpoch()" in method
        assert "writeOwnedExport(filename: filename, epoch: epoch)" in method
        assert "temporaryDirectory.appendingPathComponent" not in method
        assert "removeItem(at: target)" not in method
    assert "defer { try? FileManager.default.removeItem(at: temporary) }" in get
    assert get.index("throw APIError.unauthenticated") < get.index("writeOwnedExport")
    assert post.index("try validate(") < post.index("writeOwnedExport")
    owned = _between(api, "private func writeOwnedExport", "private func download(")
    assert owned.index("guard epoch == exportGeneration") < owned.index(
        "Self.writeExport"
    )
    assert "exportDirectories.insert(result.deletingLastPathComponent())" in owned
    clear = _between(api, "func clearLocalSession() {", "func setSessionPersistence")
    assert "exportGeneration += 1" in clear
    assert "removeItem(at: directory)" in clear
    assert "exportDirectories.removeAll()" in clear


def test_export_names_are_single_components_and_exports_are_private_and_unique():
    api = _read("Core/APIClient.swift")
    sanitizer = _between(
        api, "static func sanitizedExportFilename", "static func writeExport"
    )
    assert 'CharacterSet(charactersIn: "/\\\\:").union(.controlCharacters)' in sanitizer
    assert 'bounded == "."' in sanitizer and 'bounded == ".."' in sanitizer
    assert "bounded.isEmpty" in sanitizer and '"download"' in sanitizer
    assert "sanitized.utf8.prefix(180)" in sanitizer
    writer = _between(api, "static func writeExport", "private func put<")
    assert '"sicherpfad-export-\\(UUID().uuidString)"' in writer
    assert "withIntermediateDirectories: false" in writer
    assert ".posixPermissions: 0o700" in writer
    assert ".posixPermissions: 0o600" in writer
    assert writer.count(".protectionKey: FileProtectionType.complete") == 2
    assert (
        "directory.appendingPathComponent(sanitizedExportFilename(filename), isDirectory: false)"
        in writer
    )
    assert "try? files.removeItem(at: directory)" in writer


def test_deferred_navigation_is_consumed_only_in_mounted_signed_in_branch():
    app = _read("RcloneMobileApp.swift")
    consume = _between(app, "mutating func consume(isSignedIn:", "@main")
    assert consume.index("guard isSignedIn") < consume.index("self.destination = nil")
    root = app.split("private struct AppRootView", 1)[1]
    signed_in = _between(root, "case .signedIn:", ".animation(")
    assert ".task(id: pendingNavigation)" in signed_in
    assert signed_in.index("await Task.yield()") < signed_in.index(
        "guard !Task.isCancelled"
    )
    assert signed_in.index("guard !Task.isCancelled") < signed_in.index(
        "pendingNavigation.consume"
    )
    assert "isSignedIn: model.phase == .signedIn," in signed_in
    assert "currentServerID: model.currentPushServerID" in signed_in
    assert "if let serverID, serverID != currentServerID { return nil }" in consume
    open_url = _between(root, ".onOpenURL", '.alert("Bei Sicherungsfehlern')
    assert "AppNavigationDestination(url: url)" in open_url
    assert "pendingNavigation.request(destination)" in open_url
    assert "NotificationCenter.default.post" not in open_url


def test_login_copy_matches_https_only_persistence():
    login = _read("Views/LoginView.swift")
    assert "Nur HTTPS-Sitzungen" in login
    assert "Diese Sitzung wird auch mit Angemeldet bleiben nicht gespeichert" in login
    assert "bei späteren App-Starts wieder verwendet" not in login
    assert ".sheet(isPresented: $showingLegalInformation)" in login
    assert ".sheet(isPresented: $showingOfflineCard)" in login
