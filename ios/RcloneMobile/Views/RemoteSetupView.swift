import Darwin
import Foundation
import SafariServices
import SwiftUI
import UIKit

private struct SafariAuthItem: Identifiable {
    let url: URL
    var id: String { url.absoluteString }
}

private struct SafariAuthSheet: UIViewControllerRepresentable {
    let url: URL

    func makeUIViewController(context: Context) -> SFSafariViewController {
        let config = SFSafariViewController.Configuration()
        config.entersReaderIfAvailable = false
        config.barCollapsingEnabled = false
        let controller = SFSafariViewController(url: url, configuration: config)
        controller.dismissButtonStyle = .cancel
        return controller
    }

    func updateUIViewController(_ uiViewController: SFSafariViewController, context: Context) {}
}

final class LocalOAuthLoopbackServer: @unchecked Sendable {
    private let lock = NSLock()
    private var listenFD: Int32 = -1
    private var stopped = false

    deinit {
        stop()
    }

    func start(port: UInt16 = 53682, onCallback: @escaping @MainActor (String) -> Void) -> Bool {
        stop()
        let fd = Darwin.socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { return false }

        var reuse: Int32 = 1
        _ = Darwin.setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &reuse, socklen_t(MemoryLayout<Int32>.size))
        var noSigPipe: Int32 = 1
        _ = Darwin.setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &noSigPipe, socklen_t(MemoryLayout<Int32>.size))

        var addr = sockaddr_in()
        addr.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = port.bigEndian
        addr.sin_addr = in_addr(s_addr: inet_addr("127.0.0.1"))

        let bindResult = withUnsafePointer(to: &addr) { ptr in
            ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { sockPtr in
                Darwin.bind(fd, sockPtr, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        guard bindResult == 0, Darwin.listen(fd, 8) == 0 else {
            Darwin.close(fd)
            return false
        }

        lock.lock()
        listenFD = fd
        stopped = false
        lock.unlock()

        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            self?.acceptLoop(fd: fd, port: port, onCallback: onCallback)
        }
        return true
    }

    func stop() {
        lock.lock()
        stopped = true
        let fd = listenFD
        listenFD = -1
        lock.unlock()
        if fd >= 0 {
            _ = Darwin.shutdown(fd, SHUT_RDWR)
            Darwin.close(fd)
        }
    }

    private func acceptLoop(fd: Int32, port: UInt16, onCallback: @escaping @MainActor (String) -> Void) {
        while true {
            lock.lock()
            let isStopped = stopped || listenFD != fd
            lock.unlock()
            if isStopped { break }

            let clientFD = Darwin.accept(fd, nil, nil)
            if clientFD < 0 { break }

            var noSigPipe: Int32 = 1
            _ = Darwin.setsockopt(clientFD, SOL_SOCKET, SO_NOSIGPIPE, &noSigPipe, socklen_t(MemoryLayout<Int32>.size))

            var buffer = [UInt8](repeating: 0, count: 8192)
            let bytesRead = Darwin.read(clientFD, &buffer, buffer.count - 1)
            guard bytesRead > 0 else {
                Darwin.close(clientFD)
                continue
            }
            let requestText = String(decoding: buffer.prefix(bytesRead), as: UTF8.self)
            let firstLine = requestText.components(separatedBy: "\r\n").first ?? ""
            let parts = firstLine.split(separator: " ")
            guard parts.count >= 2 else {
                Darwin.close(clientFD)
                continue
            }
            let target = String(parts[1])
            if target.contains("code=") || target.contains("error=") {
                let hasError = URLComponents(string: "http://127.0.0.1\(target)")?.queryItems?.contains(where: { $0.name == "error" }) == true
                let heading = hasError ? "Anmeldung nicht abgeschlossen" : "Anmeldung wird geprüft"
                let html = """
                <!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Sicherpfad</title></head><body style="font-family:-apple-system,sans-serif;text-align:center;padding:48px 24px;background:#f5f5f7;color:#1d1d1f;"><div style="max-width:360px;margin:0 auto;background:#fff;padding:28px;border-radius:16px;"><h2>\(heading)</h2><p>Kehre zur Sicherpfad-App zurück. Der Zugang wird erst nach deiner Bestätigung gespeichert.</p></div></body></html>
                """
                let response = "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: \(html.utf8.count)\r\nConnection: close\r\n\r\n\(html)"
                _ = response.withCString { ptr in
                    Darwin.write(clientFD, ptr, strlen(ptr))
                }
                Darwin.close(clientFD)
                let callbackURL = "http://127.0.0.1:\(port)\(target.hasPrefix("/") ? target : "/" + target)"
                stop()
                Task { @MainActor in
                    onCallback(callbackURL)
                }
                break
            } else {
                let response = "HTTP/1.1 204 No Content\r\nConnection: close\r\n\r\n"
                _ = response.withCString { ptr in
                    Darwin.write(clientFD, ptr, strlen(ptr))
                }
                Darwin.close(clientFD)
            }
        }
    }
}

@MainActor
struct RemoteSetupView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var client: (any APIClientProtocol)?
    @State private var providers: [RemoteProvider] = []
    @State private var icloudRemotes: [ExistingICloudRemote] = []
    @State private var driveRemotes: [ExistingGoogleDriveRemote] = []
    @State private var driveReconnectSupported = false
    @State private var driveClientId = ""
    @State private var driveClientSecret = ""
    @State private var useStoredDriveClient = false
    @State private var name = "gdrive-eigen"
    @State private var provider = "drive"
    @State private var icloudService = "drive"
    @State private var appleId = ""
    @State private var applePassword = ""
    @State private var reconnect = false
    @State private var session: RemoteSetupSession?
    @State private var answer = ""
    @State private var busy = false
    @State private var errorMessage: String?
    @State private var savedName: String?
    @State private var savedReconnected = false
    @State private var safariAuthItem: SafariAuthItem?
    @State private var loopbackServer = LocalOAuthLoopbackServer()

    var body: some View {
        NavigationStack {
            Form {
                if let savedName {
                    Section(savedReconnected ? "\(providerDisplayName)-Anmeldung erneuert" : "Remote angelegt") {
                        Label(savedName, systemImage: "checkmark.circle")
                        Text(
                            savedReconnected
                                ? (provider == "drive"
                                    ? "Der erneuerte Google-Zugang wurde gespeichert. Bestehende Datenwege und Tresore behalten ihre Zuordnung."
                                    : "Das neue Apple-2FA-Sitzungstoken wurde gespeichert. Bestehende Datenwege laufen unverändert weiter.")
                                : "Der Speicher ist jetzt in der Ordnerauswahl verfügbar. Dort kannst du die Erreichbarkeit prüfen."
                        )
                        Button("Fertig") { dismiss() }
                    }
                } else if let session {
                    if session.ready {
                        Section("Bereit zum Speichern") {
                            if provider == "drive", reconnect {
                                Text("Anmeldung geprüft. Der Zugang für \(session.name) wird erst beim Speichern aktualisiert. Verwende dasselbe Google-Konto; Datenwege und Tresore behalten ihre Zuordnung.")
                            } else {
                                Text("Die Angaben für \(session.name) sind vollständig. Erst beim Speichern wird das Remote in die Serverkonfiguration übernommen.")
                            }
                            if provider == "drive" {
                                Text("Die Vorbereitung läuft 30 Minuten nach Beginn ab. Während einer laufenden Sicherung verweigert der Server das Speichern. Danach kannst du innerhalb dieser Zeit erneut speichern; andernfalls die Anmeldung neu beginnen.")
                                    .font(.footnote)
                                    .foregroundStyle(.secondary)
                            }
                        }
                    } else {
                        Section(session.option.labelDe ?? session.option.name) {
                            if browserAuthURL(for: session) != nil {
                                Text(provider == "drive"
                                    ? "Melde dich im offiziellen Google-Fenster an und bestätige den Zugriff. Dein Google-Passwort bleibt bei Google."
                                    : "Melde dich im geöffneten Browserfenster an und bestätige den Zugriff.")
                                    .font(.subheadline)
                                Button {
                                    startAutomaticBrowserAuthIfNeeded(for: session)
                                } label: {
                                    Label(provider == "drive" ? "Bei Google anmelden" : "Anmeldung öffnen", systemImage: "safari.fill")
                                        .font(.headline)
                                        .frame(minHeight: 44)
                                }
                            } else {
                                Text(session.option.hintDe ?? session.option.help).textSelection(.enabled)
                            }

                            if session.option.exclusive && !session.option.examples.isEmpty {
                                Picker("Antwort", selection: $answer) {
                                    Text(session.option.defaultValue.isEmpty ? "Bitte auswählen" : "Standard: \(session.option.defaultValue)").tag("")
                                    ForEach(session.option.examples) { example in
                                        Text("\(example.value) · \(example.help)").tag(example.value)
                                    }
                                }
                            } else if Self.isTwoFACodeOption(session.option) {
                                TextField("6-stelliger Apple-Code (z. B. 123456)", text: $answer)
                                    .keyboardType(.numberPad)
                                    .textContentType(.oneTimeCode)
                                    .font(.title3.monospacedDigit())
                                    .textInputAutocapitalization(.never)
                                    .autocorrectionDisabled()

                                if session.option.name == "config_2fa" {
                                    Button {
                                        answer = "sms"
                                        Task { await submit() }
                                    } label: {
                                        Label("Kein Apple-Gerät griffbereit? Code per SMS senden", systemImage: "message")
                                    }
                                }
                            } else if session.option.secret && (session.option.authorizeCommand ?? "").isEmpty && session.option.name != "config_token" {
                                SecureField(
                                    session.option.defaultValue.isEmpty ? "Passwort" : session.option.defaultValue,
                                    text: $answer
                                )
                                .textContentType(.password)
                                .textInputAutocapitalization(.never)
                                .autocorrectionDisabled()
                            } else {
                                TextField(
                                    !(session.option.authorizeCommand ?? "").isEmpty
                                        ? "Falls extern angemeldet: 127.0.0.1-Adresse oder Code einfügen"
                                        : (session.option.defaultValue.isEmpty ? "Antwort" : session.option.defaultValue),
                                    text: $answer,
                                    axis: .vertical
                                )
                                .textInputAutocapitalization(.never)
                                .autocorrectionDisabled()

                                if !(session.option.authorizeCommand ?? "").isEmpty || session.option.name == "config_token" {
                                    Button {
                                        if let clip = UIPasteboard.general.string?.trimmingCharacters(in: .whitespacesAndNewlines),
                                           !clip.isEmpty {
                                            answer = clip
                                        }
                                    } label: {
                                        Label("Aus Zwischenablage einfügen", systemImage: "doc.on.clipboard")
                                    }
                                }
                            }
                            if !session.option.defaultValue.isEmpty {
                                Text("Leer lassen für den Standard: \(session.option.defaultValue)").font(.caption).foregroundStyle(.secondary)
                            }
                        }
                    }
                } else {
                    Section("Schnellwahl") {
                        ScrollView(.horizontal, showsIndicators: false) {
                            HStack(spacing: 8) {
                                presetButton(title: "Apple iCloud", providerName: "iclouddrive", defaultRemote: "icloud")
                                presetButton(title: "Google Drive", providerName: "drive", defaultRemote: "gdrive-eigen")
                                presetButton(title: "pCloud", providerName: "pcloud", defaultRemote: "pcloud")
                                presetButton(title: "OneDrive", providerName: "onedrive", defaultRemote: "onedrive")
                                presetButton(title: "Nextcloud / WebDAV", providerName: "webdav", defaultRemote: "nextcloud")
                            }
                            .padding(.vertical, 2)
                        }
                    }
                    Section("Speicheranbieter") {
                        if provider != "drive" {
                            TextField("Remote-Name", text: $name).textInputAutocapitalization(.never).autocorrectionDisabled()
                            Text("2–64 Buchstaben, Ziffern, Bindestriche oder Unterstriche.").font(.caption).foregroundStyle(.secondary)
                        }
                        Picker("Anbieter", selection: $provider) {
                            Text("Bitte auswählen").tag("")
                            ForEach(providers) { Text($0.description).tag($0.name) }
                        }
                    }
                    if provider == "drive" {
                        if providers.isEmpty {
                            Section { Text("Google-Anbindung wird geladen…").foregroundStyle(.secondary) }
                        } else {
                            GoogleDriveLoginForm(driveRemotes: driveRemotes, driveReconnectSupported: driveReconnectSupported,
                                name: $name, reconnect: $reconnect, clientId: $driveClientId,
                                clientSecret: $driveClientSecret, useStoredClient: $useStoredDriveClient)
                        }
                    }
                    if provider == "iclouddrive" {
                        Section("Apple iCloud Schnellstart") {
                            Label("Wichtig: Reguläres Apple-Account-Passwort eingeben (kein App-Passwort!). Danach erscheint der 6-stellige Code direkt auf diesem iPhone.", systemImage: "icloud.fill")
                                .font(.caption)
                                .foregroundStyle(.secondary)

                            if !icloudRemotes.isEmpty {
                                ScrollView(.horizontal, showsIndicators: false) {
                                    HStack(spacing: 8) {
                                        ForEach(icloudRemotes) { item in
                                            Button("Token erneuern: \(item.name)") {
                                                reconnect = true
                                                name = item.name
                                                icloudService = item.service == "photos" ? "photos" : "drive"
                                                appleId = item.appleId
                                                errorMessage = nil
                                            }
                                            .buttonStyle(.bordered)
                                            .tint(reconnect && name == item.name ? .accentColor : .secondary)
                                        }
                                    }
                                    .padding(.vertical, 2)
                                }
                            }

                            Picker("iCloud-Dienst", selection: $icloudService) {
                                Text("iCloud Drive (Backups & Ordner)").tag("drive")
                                Text("iCloud Fotos (Mediathek)").tag("photos")
                            }
                            .onChange(of: icloudService) { _, newValue in
                                let trimmed = name.trimmingCharacters(in: .whitespaces)
                                if !reconnect && ["", "icloud", "icloud-fotos"].contains(trimmed) {
                                    name = newValue == "photos" ? "icloud-fotos" : "icloud"
                                }
                            }

                            TextField("Apple-ID (E-Mail)", text: $appleId)
                                .keyboardType(.emailAddress)
                                .textContentType(.username)
                                .textInputAutocapitalization(.never)
                                .autocorrectionDisabled()

                            SecureField("Apple-Account-Passwort (regulär)", text: $applePassword)
                                .textContentType(.password)
                                .textInputAutocapitalization(.never)
                                .autocorrectionDisabled()

                            Toggle("Bestehendes iCloud-Remote erneuern (30-Tage-Token)", isOn: $reconnect)
                        }
                    }
                }
                if let errorMessage {
                    Section { Text(errorMessage).foregroundStyle(.red).accessibilityAddTraits(.updatesFrequently) }
                }
                if savedName == nil {
                    Section {
                        Button(primaryButtonTitle) { Task { await submit() } }
                            .frame(minHeight: 44)
                            .disabled(busy || (session == nil && (provider.isEmpty || name.trimmingCharacters(in: .whitespaces).isEmpty || !googleStartIsValid)))
                        if busy { ProgressView("Einrichtung wird verarbeitet…") }
                        if providers.isEmpty && session == nil && !busy {
                            Button("Anbieter erneut laden") { Task { await load() } }
                        }
                    }
                }
            }
            .disabled(busy)
            .navigationTitle("Remote einbinden")
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button(savedName == nil ? "Abbrechen" : "Schließen") { dismiss() }.disabled(busy) } }
            .interactiveDismissDisabled(busy)
            .sheet(item: $safariAuthItem) { item in
                SafariAuthSheet(url: item.url)
                    .ignoresSafeArea()
            }
            .task { await load() }
            .onChange(of: provider) { _, _ in
                reconnect = false
                driveClientId = ""
                driveClientSecret = ""
                useStoredDriveClient = false
                errorMessage = nil
            }
            .onDisappear {
                loopbackServer.stop()
                safariAuthItem = nil
                answer = ""
                applePassword = ""
                driveClientSecret = ""
                let draftID = session?.id
                session = nil
                if let id = draftID, let client { Task { try? await client.cancelRemoteSetup(id: id) } }
            }
        }
    }

    private var primaryButtonTitle: String {
        if session?.ready == true { return provider == "drive" && reconnect ? "Zugang speichern" : "Remote speichern" }
        if provider == "drive", session == nil || answer.isEmpty { return "Bei Google anmelden" }
        if session == nil && provider == "iclouddrive" {
            if reconnect { return "iCloud-Token jetzt erneuern" }
            if !appleId.trimmingCharacters(in: .whitespaces).isEmpty && !applePassword.isEmpty {
                return "Weiter zum 6-stelligen Apple-Code"
            }
            return "Weiter zur iCloud-Anmeldung"
        }
        if session != nil { return "Antwort bestätigen" }
        return "Weiter"
    }

    private var providerDisplayName: String {
        provider == "drive" ? "Google-Drive" : (provider == "iclouddrive" ? "iCloud" : "Remote")
    }

    private var usesStoredDriveClient: Bool {
        reconnect && driveReconnectSupported && useStoredDriveClient
            && driveRemotes.contains(where: { $0.name == name && $0.hasOwnClient })
    }

    private var googleStartIsValid: Bool {
        guard provider == "drive" else { return true }
        if reconnect && (!driveReconnectSupported || !driveRemotes.contains(where: { $0.name == name })) { return false }
        return usesStoredDriveClient
            || (!driveClientId.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                && !driveClientSecret.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
    }

    private static func isTwoFACodeOption(_ option: RemoteSetupOption) -> Bool {
        option.inputMode == "numeric" || option.name == "config_2fa" || option.name == "config_2fa_sms"
    }

    private func presetButton(title: String, providerName: String, defaultRemote: String) -> some View {
        Button(title) {
            if provider != providerName {
                reconnect = false
                driveClientSecret = ""
            }
            provider = providerName
            let trimmed = name.trimmingCharacters(in: .whitespaces)
            if !reconnect && (trimmed.isEmpty || ["gdrive", "gdrive-eigen", "icloud", "icloud-fotos", "pcloud", "onedrive", "nextcloud"].contains(trimmed)) {
                name = defaultRemote
            }
        }
        .buttonStyle(.bordered)
        .tint(provider == providerName ? .accentColor : .secondary)
    }

    private static func httpsAuthURL(for session: RemoteSetupSession) -> URL? {
        for candidate in [session.option.directAuthUrl, session.option.authUrl] {
            if let raw = candidate, !raw.isEmpty,
               let url = URL(string: raw),
               url.scheme?.lowercased() == "https" {
                return url
            }
        }
        return nil
    }

    private static func extractLoopbackPort(from authTarget: String) -> UInt16? {
        if let components = URLComponents(string: authTarget) {
            if let host = components.host, ["127.0.0.1", "localhost"].contains(host), let port = components.port, port > 0 {
                return UInt16(exactly: port)
            }
            if let redirectURI = components.queryItems?.first(where: { $0.name == "redirect_uri" })?.value,
               let redirectComponents = URLComponents(string: redirectURI),
               redirectComponents.scheme == "http",
               ["127.0.0.1", "localhost"].contains(redirectComponents.host ?? ""),
               let port = redirectComponents.port, port > 0 {
                return UInt16(exactly: port)
            }
        }
        return nil
    }

    private func browserAuthURL(for session: RemoteSetupSession) -> URL? {
        guard let url = Self.httpsAuthURL(for: session) else { return nil }
        guard provider != "drive" || (url.host?.lowercased() == "accounts.google.com" && url.user == nil && url.password == nil) else { return nil }
        return url
    }

    private func startAutomaticBrowserAuthIfNeeded(for session: RemoteSetupSession) {
        guard !session.ready else { return }
        guard let url = browserAuthURL(for: session) else {
            if provider == "drive", session.option.name == "config_token" {
                errorMessage = "Die offizielle Google-Anmeldeseite konnte nicht bereitgestellt werden. Schließe diese Einrichtung und versuche es erneut."
            }
            return
        }
        let port = Self.extractLoopbackPort(from: url.absoluteString) ?? 53682
        let started = loopbackServer.start(port: port) { callbackURL in
            guard self.session?.id == session.id, self.session?.step == session.step else { return }
            self.safariAuthItem = nil
            if URLComponents(string: callbackURL)?.queryItems?.contains(where: { $0.name == "error" }) == true {
                self.answer = ""
                self.errorMessage = self.provider == "drive"
                    ? "Die Anmeldung wurde nicht abgeschlossen. Du kannst die Google-Anmeldung erneut öffnen."
                    : "Die Anmeldung wurde nicht abgeschlossen. Du kannst das Anmeldefenster erneut öffnen."
                return
            }
            self.answer = callbackURL
            Task { await self.submit() }
        }
        guard started else {
            errorMessage = "Die lokale Rückleitung konnte nicht gestartet werden. Schließe eine andere laufende Anmeldung und versuche es erneut."
            return
        }
        safariAuthItem = SafariAuthItem(url: url)
    }

    private func load() async {
        guard !busy, providers.isEmpty else { return }
        client = client ?? model.client
        guard let client else { errorMessage = APIError.unauthenticated.localizedDescription; return }
        busy = true
        defer { busy = false }
        do {
            let response = try await client.getRemoteProviders()
            providers = response.providers
            icloudRemotes = response.icloudRemotes
            driveRemotes = response.driveRemotes
            driveReconnectSupported = response.driveReconnectSupported
            if !driveReconnectSupported, provider == "drive" { reconnect = false }
            errorMessage = nil
        }
        catch { errorMessage = error.localizedDescription }
    }

    private func submit() async {
        guard !busy, let client else { return }
        busy = true
        defer { busy = false }
        errorMessage = nil
        do {
            if let session {
                if session.ready {
                    loopbackServer.stop()
                    let saved = try await client.saveRemoteSetup(id: session.id)
                    guard saved.ok else { errorMessage = "Der Zugang konnte nicht gespeichert werden. Bitte erneut versuchen."; return }
                    savedReconnected = saved.reconnected == true
                    savedName = saved.name
                    self.session = nil
                } else {
                    if provider == "drive", answer.isEmpty, browserAuthURL(for: session) != nil {
                        startAutomaticBrowserAuthIfNeeded(for: session)
                        return
                    }
                    let updated = try await client.answerRemoteSetup(
                        id: session.id,
                        answer: answer.isEmpty ? session.option.defaultValue : answer,
                        step: session.step
                    )
                    self.session = updated
                    errorMessage = updated.error.isEmpty ? nil : updated.error
                    if updated.ready {
                        loopbackServer.stop()
                    } else {
                        startAutomaticBrowserAuthIfNeeded(for: updated)
                    }
                }
            } else {
                guard googleStartIsValid else {
                    errorMessage = "Für diese Google-Anmeldung werden eine eigene Client-ID und das zugehörige Client-Secret benötigt."
                    return
                }
                defer { driveClientSecret = "" }
                let trimmedName = name.trimmingCharacters(in: .whitespaces)
                let request: RemoteSetupStart
                if provider == "iclouddrive" {
                    request = RemoteSetupStart(
                        name: trimmedName,
                        provider: provider,
                        express: true,
                        icloudService: icloudService,
                        appleId: appleId.trimmingCharacters(in: .whitespaces),
                        applePassword: applePassword.isEmpty ? nil : applePassword,
                        reconnect: reconnect
                    )
                } else if provider == "drive" {
                    var googleRequest = RemoteSetupStart(name: trimmedName, provider: "drive", express: true)
                    googleRequest.clientId = usesStoredDriveClient ? nil : driveClientId.trimmingCharacters(in: .whitespacesAndNewlines)
                    googleRequest.clientSecret = usesStoredDriveClient ? nil : driveClientSecret.trimmingCharacters(in: .whitespacesAndNewlines)
                    googleRequest.requireOwnClient = true
                    googleRequest.reconnect = reconnect
                    request = googleRequest
                } else {
                    request = RemoteSetupStart(
                        name: trimmedName,
                        provider: provider,
                        express: provider == "drive"
                    )
                }
                let created = try await client.startRemoteSetup(request)
                applePassword = ""
                session = created
                errorMessage = created.error.isEmpty ? nil : created.error
                startAutomaticBrowserAuthIfNeeded(for: created)
            }
            answer = ""
        } catch { errorMessage = error.localizedDescription }
    }
}
