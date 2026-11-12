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

    func start(port: UInt16 = 53682, onCallback: @escaping @MainActor (String) -> Void) {
        stop()
        let fd = Darwin.socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { return }

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
            return
        }

        lock.lock()
        listenFD = fd
        stopped = false
        lock.unlock()

        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            self?.acceptLoop(fd: fd, port: port, onCallback: onCallback)
        }
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
                let html = """
                <!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Sicherpfad</title></head><body style="font-family:-apple-system,sans-serif;text-align:center;padding:48px 24px;background:#f5f5f7;color:#1d1d1f;"><div style="max-width:360px;margin:0 auto;background:#fff;padding:28px;border-radius:16px;box-shadow:0 4px 16px rgba(0,0,0,0.08);"><h2 style="margin:0 0 12px;color:#1d7d3f;">✓ Google Drive verbunden</h2><p style="margin:0;color:#6e6e73;">Die Anmeldung wird in der Sicherpfad-App abgeschlossen…</p></div></body></html>
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
    @State private var name = "gdrive"
    @State private var provider = "drive"
    @State private var session: RemoteSetupSession?
    @State private var answer = ""
    @State private var busy = false
    @State private var errorMessage: String?
    @State private var savedName: String?
    @State private var safariAuthItem: SafariAuthItem?
    @State private var loopbackServer = LocalOAuthLoopbackServer()

    var body: some View {
        NavigationStack {
            Form {
                if let savedName {
                    Section("Remote angelegt") {
                        Label(savedName, systemImage: "checkmark.circle")
                        Text("Der Speicher ist jetzt in der Ordnerauswahl verfügbar. Dort kannst du die Erreichbarkeit prüfen.")
                        Button("Fertig") { dismiss() }
                    }
                } else if let session {
                    if session.ready {
                        Section("Bereit zum Speichern") {
                            Text("Die Angaben für \(session.name) sind vollständig. Erst beim Speichern wird das Remote in die Serverkonfiguration übernommen.")
                        }
                    } else {
                        Section(session.option.labelDe ?? session.option.name) {
                            if Self.httpsAuthURL(for: session) != nil {
                                Text("Melde dich im geöffneten Google-Fenster an und bestätige mit „Zulassen“. Die Rückleitung wird automatisch erkannt.")
                                    .font(.subheadline)
                                Button {
                                    startAutomaticBrowserAuthIfNeeded(for: session)
                                } label: {
                                    Label("Google-Anmeldung öffnen", systemImage: "safari.fill")
                                        .font(.headline)
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
                                presetButton(title: "Google Drive", providerName: "drive", defaultRemote: "gdrive")
                                presetButton(title: "pCloud", providerName: "pcloud", defaultRemote: "pcloud")
                                presetButton(title: "OneDrive", providerName: "onedrive", defaultRemote: "onedrive")
                                presetButton(title: "Nextcloud / WebDAV", providerName: "webdav", defaultRemote: "nextcloud")
                            }
                            .padding(.vertical, 2)
                        }
                    }
                    Section("Neuer Speicher") {
                        TextField("Remote-Name", text: $name).textInputAutocapitalization(.never).autocorrectionDisabled()
                        Text("2–64 Buchstaben, Ziffern, Bindestriche oder Unterstriche.").font(.caption).foregroundStyle(.secondary)
                        Picker("Anbieter", selection: $provider) {
                            Text("Bitte auswählen").tag("")
                            ForEach(providers) { Text($0.description).tag($0.name) }
                        }
                        if provider == "drive" {
                            Label("Google-Drive-Express: Öffnet direkt die Google-Anmeldung und übernimmt das Konto automatisch.", systemImage: "checkmark.seal.fill")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                    }
                }
                if let errorMessage {
                    Section { Text(errorMessage).foregroundStyle(.red).accessibilityAddTraits(.updatesFrequently) }
                }
                if savedName == nil {
                    Section {
                        Button(primaryButtonTitle) { Task { await submit() } }
                            .disabled(busy || (session == nil && (provider.isEmpty || name.trimmingCharacters(in: .whitespaces).isEmpty)))
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
            .onDisappear {
                loopbackServer.stop()
                safariAuthItem = nil
                answer = ""
                if let id = session?.id, let client { Task { try? await client.cancelRemoteSetup(id: id) } }
            }
        }
    }

    private var primaryButtonTitle: String {
        if session?.ready == true { return "Remote speichern" }
        if session == nil && provider == "drive" { return "Weiter zur Google-Anmeldung" }
        if session != nil { return "Fertigstellen & Speichern" }
        return "Weiter"
    }

    private func presetButton(title: String, providerName: String, defaultRemote: String) -> some View {
        Button(title) {
            provider = providerName
            if name.trimmingCharacters(in: .whitespaces).isEmpty || ["gdrive", "pcloud", "onedrive", "nextcloud"].contains(name) {
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
            if let host = components.host, ["127.0.0.1", "localhost"].contains(host), let port = components.port {
                return UInt16(port)
            }
            if let redirectURI = components.queryItems?.first(where: { $0.name == "redirect_uri" })?.value,
               let redirectComponents = URLComponents(string: redirectURI),
               let port = redirectComponents.port {
                return UInt16(port)
            }
        }
        return nil
    }

    private func startAutomaticBrowserAuthIfNeeded(for session: RemoteSetupSession) {
        guard !session.ready, let url = Self.httpsAuthURL(for: session) else {
            return
        }
        let port = Self.extractLoopbackPort(from: url.absoluteString) ?? 53682
        loopbackServer.start(port: port) { callbackURL in
            self.safariAuthItem = nil
            self.answer = callbackURL
            Task { await self.submit() }
        }
        safariAuthItem = SafariAuthItem(url: url)
    }

    private func load() async {
        guard !busy, providers.isEmpty else { return }
        client = client ?? model.client
        guard let client else { errorMessage = APIError.unauthenticated.localizedDescription; return }
        busy = true
        defer { busy = false }
        do { providers = try await client.getRemoteProviders().providers; errorMessage = nil }
        catch { errorMessage = error.localizedDescription }
    }

    private func submit() async {
        guard !busy, let client else { return }
        busy = true
        defer { busy = false }
        do {
            if let session {
                if session.ready {
                    loopbackServer.stop()
                    savedName = try await client.saveRemoteSetup(id: session.id).name
                    self.session = nil
                } else {
                    let updated = try await client.answerRemoteSetup(
                        id: session.id,
                        answer: answer.isEmpty ? session.option.defaultValue : answer,
                        step: session.step
                    )
                    if updated.ready {
                        loopbackServer.stop()
                        savedName = try await client.saveRemoteSetup(id: updated.id).name
                        self.session = nil
                    } else {
                        self.session = updated
                        startAutomaticBrowserAuthIfNeeded(for: updated)
                    }
                }
            } else {
                let created = try await client.startRemoteSetup(name: name.trimmingCharacters(in: .whitespaces), provider: provider)
                session = created
                startAutomaticBrowserAuthIfNeeded(for: created)
            }
            answer = ""
            errorMessage = session?.error.isEmpty == false ? session?.error : nil
        } catch { errorMessage = error.localizedDescription }
    }
}
