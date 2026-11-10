import SwiftUI
import UIKit

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
                            if let authTarget = session.option.directAuthUrl ?? session.option.authUrl,
                               !authTarget.isEmpty,
                               let url = URL(string: authTarget) {
                                Text("1. Melde dich im Browser bei Google an und bestätige mit „Zulassen“.")
                                    .font(.subheadline)
                                Link(destination: url) {
                                    Label("1. Bei Google im Browser anmelden", systemImage: "safari.fill")
                                        .font(.headline)
                                }
                                Text("2. Danach leitet Google auf eine 127.0.0.1-Adresse weiter (Hinweis „Seite kann nicht geöffnet werden“ am iPhone ist normal). Kopiere diese Adresse oben aus der Safari-Adresszeile und füge sie unten ein:")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
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
                                    session.option.authorizeCommand != nil
                                        ? "2. Adresse aus Safari (http://127.0.0.1:53682/?code=…) einfügen"
                                        : (session.option.defaultValue.isEmpty ? "Antwort" : session.option.defaultValue),
                                    text: $answer,
                                    axis: .vertical
                                )
                                .textInputAutocapitalization(.never)
                                .autocorrectionDisabled()

                                if session.option.authorizeCommand != nil || session.option.name == "config_token" {
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
                            Label("Google-Drive-Express: Alle technischen Vorfragen werden automatisch beantwortet – du meldest dich direkt im Browser bei Google an.", systemImage: "checkmark.seal.fill")
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
            .task { await load() }
            .onDisappear {
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
                    savedName = try await client.saveRemoteSetup(id: session.id).name
                    self.session = nil
                } else {
                    let updated = try await client.answerRemoteSetup(
                        id: session.id,
                        answer: answer.isEmpty ? session.option.defaultValue : answer,
                        step: session.step
                    )
                    if updated.ready {
                        savedName = try await client.saveRemoteSetup(id: updated.id).name
                        self.session = nil
                    } else {
                        self.session = updated
                    }
                }
            } else {
                session = try await client.startRemoteSetup(name: name.trimmingCharacters(in: .whitespaces), provider: provider)
            }
            answer = ""
            errorMessage = session?.error.isEmpty == false ? session?.error : nil
        } catch { errorMessage = error.localizedDescription }
    }
}
