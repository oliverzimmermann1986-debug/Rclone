import SwiftUI
import UniformTypeIdentifiers

struct RecoveryRescueView: View {
    @EnvironmentObject private var model: AppModel
    @State private var envelope: [String: JSONValue]?
    @State private var filename = ""
    @State private var passphrase = ""
    @State private var password = ""
    @State private var preview: RecoveryRescuePreview?
    @State private var mappings: [String: String] = [:]
    @State private var showingImporter = false
    @State private var confirmPreview = false
    @State private var confirmImport = false
    @State private var isWorking = false
    @State private var imported = false
    @State private var message: String?

    private var isHTTP: Bool { (try? APIClient.normalizedServerURL(model.serverAddress).scheme) == "http" }
    private var mappingComplete: Bool {
        guard let preview, preview.records > 0 else { return false }
        return preview.dataPaths.allSatisfy { !(mappings[$0.identity] ?? "").isEmpty }
    }

    var body: some View {
        Form {
            Section("1 · Neuen Server vorbereiten") {
                Text("Melde dich am Ersatzserver an und richte dort den bisherigen Cloud-Zugang und passende Datenwege ein. Die Notfallakte ersetzt keine Cloud-Schlüssel.")
                LabeledContent("Verbunden mit", value: model.serverAddress)
                Text("Rettbar sind die in der Akte aufgeführten Geräte-Vault-Dateien aus dem Cloud-Ziel. Lokale vollständige Stände sind nicht enthalten und schützen allein nicht gegen Serververlust.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section("2 · Verschlüsselte Notfallakte") {
                Button("Datei öffnen …") { showingImporter = true }.disabled(isWorking)
                if !filename.isEmpty { Text(filename).font(.caption) }
                SecureField("Passphrase der Notfallakte", text: $passphrase)
                Button("Inhalt prüfen") { confirmPreview = true }
                    .disabled(envelope == nil || passphrase.isEmpty || isWorking || model.isDemoMode)
                if isHTTP {
                    Label("HTTP ist unverschlüsselt. Paket und Passphrase werden an diesen Server gesendet. Verwende möglichst HTTPS oder einen vertrauenswürdigen VPN-Tunnel.", systemImage: "exclamationmark.lock")
                        .font(.caption).foregroundStyle(.orange)
                }
            }
            if let preview {
                Section("3 · Ziele bewusst zuordnen") {
                    LabeledContent("Vault-Dateien", value: "\(preview.records)")
                    LabeledContent("Datenmenge", value: AppFormat.bytes(preview.totalBytes))
                    ForEach(preview.dataPaths) { source in
                        Picker(source.name, selection: Binding(
                            get: { mappings[source.identity] ?? "" },
                            set: { mappings[source.identity] = $0 }
                        )) {
                            Text("Ziel wählen").tag("")
                            ForEach(preview.availableTargets) { target in
                                Text("\(target.name) · \(target.target ?? "")").tag(target.identity)
                            }
                        }
                        if let hint = source.targetHint { Text("Bisher: \(hint)").font(.caption) }
                    }
                    SecureField("Aktuelles Passwort des Ersatzservers", text: $password)
                    Button("Rettungsinventar importieren") { confirmImport = true }
                        .disabled(!mappingComplete || password.isEmpty || isWorking || imported)
                    Text("Kein Überschreiben der Konfiguration und keine Übernahme alter Anmeldedaten. Noch kein Nachweis, dass die Dateien erreichbar sind.").font(.caption)
                }
            }
            if isWorking { ProgressView("Wird geprüft …") }
            if let message { Section { Text(message).textSelection(.enabled) } }
            if imported {
                Section("4 · Datei wirklich zurückholen") {
                    NavigationLink("Vault öffnen & Datei zurückholen") { DeviceVaultView() }
                    Text("In der Bibliothek eine importierte Datei herunterladen. Erst nach dem Rücklesen und dem SHA-256-Abgleich wird sie bestätigt.").font(.caption)
                }
            }
        }
        .navigationTitle("Serververlust")
        .navigationBarTitleDisplayMode(.inline)
        .fileImporter(isPresented: $showingImporter, allowedContentTypes: [.json, .data]) { result in
            do {
                let url = try result.get()
                let scoped = url.startAccessingSecurityScopedResource()
                defer { if scoped { url.stopAccessingSecurityScopedResource() } }
                let size = try url.resourceValues(forKeys: [.fileSizeKey]).fileSize ?? 0
                guard size > 0 && size <= 2 * 1024 * 1024 else { throw CocoaError(.fileReadTooLarge) }
                envelope = try JSONDecoder().decode([String: JSONValue].self, from: Data(contentsOf: url))
                filename = url.lastPathComponent
                preview = nil; mappings = [:]; imported = false; message = nil
                passphrase = ""; password = ""
            } catch { message = error.localizedDescription }
        }
        .confirmationDialog("Notfallakte an diesen Server senden?", isPresented: $confirmPreview, titleVisibility: .visible) {
            Button("Inhalt am verbundenen Server prüfen", role: isHTTP ? .destructive : nil) { Task { await inspect() } }
            Button("Abbrechen", role: .cancel) {}
        } message: { Text("Die Passphrase wird nur für diese Prüfung verwendet. Vertraue diesem Server.") }
        .confirmationDialog("Zuordnung übernehmen?", isPresented: $confirmImport, titleVisibility: .visible) {
            Button("Rettungsinventar importieren") { Task { await restoreInventory() } }
            Button("Abbrechen", role: .cancel) {}
        }
        .onDisappear { password = ""; passphrase = "" }
    }

    private func inspect() async {
        guard let envelope else { return }
        isWorking = true
        defer { isWorking = false }
        preview = nil; mappings = [:]; imported = false; message = nil
        do {
            preview = try await model.withCurrentClient {
                try await $0.previewRecoveryRescue(RecoveryRescueRequest(envelope: envelope, passphrase: passphrase))
            }
        } catch { message = error.localizedDescription }
    }

    private func restoreInventory() async {
        guard let envelope, mappingComplete else { return }
        isWorking = true
        defer { isWorking = false; password = "" }
        do {
            let result = try await model.withCurrentClient {
                try await $0.importRecoveryRescue(RecoveryRescueRequest(envelope: envelope, passphrase: passphrase,
                    currentPassword: password, mappings: mappings))
            }
            imported = result.ok
            message = "\(result.imported) Einträge importiert, \(result.alreadyPresent) bereits vorhanden. Jetzt eine Datei aus dem Ziel zurückholen und prüfen."
            passphrase = ""
        } catch { message = error.localizedDescription }
    }
}
