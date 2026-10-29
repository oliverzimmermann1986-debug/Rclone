import SwiftUI

@MainActor
struct RemoteSetupView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var client: (any APIClientProtocol)?
    @State private var providers: [RemoteProvider] = []
    @State private var name = ""
    @State private var provider = ""
    @State private var session: RemoteSetupSession?
    @State private var answer = ""
    @State private var busy = false
    @State private var errorMessage: String?
    @State private var savedName: String?

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Text("Verbinde einen neuen Speicher mit diesem Server. Anbieterfragen stammen aus rclone und können auf Englisch erscheinen.")
                }
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
                        Section(session.option.name) {
                            Text(session.option.help).textSelection(.enabled)
                            if session.option.exclusive && !session.option.examples.isEmpty {
                                Picker("Antwort", selection: $answer) {
                                    Text(session.option.defaultValue.isEmpty ? "Bitte auswählen" : "Standard: \(session.option.defaultValue)").tag("")
                                    ForEach(session.option.examples) { example in
                                        Text("\(example.value) · \(example.help)").tag(example.value)
                                    }
                                }
                            } else if session.option.secret {
                                SecureField("Antwort", text: $answer)
                            } else {
                                TextField(session.option.defaultValue.isEmpty ? "Antwort" : session.option.defaultValue, text: $answer, axis: .vertical)
                                    .textInputAutocapitalization(.never).autocorrectionDisabled()
                            }
                            if !session.option.defaultValue.isEmpty {
                                Text("Leer lassen für den Standard: \(session.option.defaultValue)").font(.caption).foregroundStyle(.secondary)
                            }
                        }
                    }
                } else {
                    Section("Neuer Speicher") {
                        TextField("Remote-Name", text: $name).textInputAutocapitalization(.never).autocorrectionDisabled()
                        Text("2–64 Buchstaben, Ziffern, Bindestriche oder Unterstriche.").font(.caption).foregroundStyle(.secondary)
                        Picker("Anbieter", selection: $provider) {
                            Text("Bitte auswählen").tag("")
                            ForEach(providers) { Text($0.description).tag($0.name) }
                        }
                    }
                }
                if let errorMessage {
                    Section { Text(errorMessage).foregroundStyle(.red).accessibilityAddTraits(.updatesFrequently) }
                }
                if savedName == nil {
                    Section {
                        Button(session?.ready == true ? "Remote speichern" : "Weiter") { Task { await submit() } }
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
                    self.session = try await client.answerRemoteSetup(id: session.id, answer: answer.isEmpty ? session.option.defaultValue : answer, step: session.step)
                }
            } else {
                session = try await client.startRemoteSetup(name: name.trimmingCharacters(in: .whitespaces), provider: provider)
            }
            answer = ""
            errorMessage = session?.error.isEmpty == false ? session?.error : nil
        } catch { errorMessage = error.localizedDescription }
    }
}
