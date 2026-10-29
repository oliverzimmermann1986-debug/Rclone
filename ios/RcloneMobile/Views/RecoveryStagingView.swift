import SwiftUI

struct RecoveryStagingResponse: Decodable {
    let items: [RecoveryStagingItem]
}

struct RecoveryStagingItem: Decodable, Identifiable {
    let id: String
    let pair: String?
    let status: String?
    let verified: Bool?
    let files: Int?
    let bytes: Int64?
    let stagingPath: String?
    enum CodingKeys: String, CodingKey {
        case id, pair, status, verified, files, bytes
        case stagingPath = "staging_path"
    }
}

struct RecoveryStagingView: View {
    @EnvironmentObject private var model: AppModel
    @State private var items: [RecoveryStagingItem] = []
    @State private var selected: RecoveryStagingItem?
    @State private var password = ""
    @State private var errorMessage: String?
    @State private var busy = false

    var body: some View {
        List {
            Section {
                Text("Zurückgeholte Dateien liegen getrennt von deinen produktiven Daten auf dem Server. Entfernen löscht diese Staging-Kopie; exportiere benötigte Dateien vorher.")
            }
            ForEach(items) { item in
                Section(item.pair ?? item.id) {
                    Label(item.verified == true ? "Auswahl per Prüfsumme bestätigt" : (item.status ?? "Ungeprüft"), systemImage: item.verified == true ? "checkmark.seal" : "exclamationmark.triangle")
                    Text("\(item.files ?? 0) Dateien · \(AppFormat.bytes(item.bytes ?? 0))")
                    if let path = item.stagingPath { Text(path).font(.caption).textSelection(.enabled) }
                    Button("Staging entfernen", role: .destructive) { selected = item }
                        .disabled(busy || item.status == "running")
                }
            }
            if items.isEmpty && !busy { Text("Keine Staging-Wiederherstellungen") }
            if busy { ProgressView() }
            if let errorMessage { Text(errorMessage).foregroundStyle(.red) }
        }
        .navigationTitle("Recovery-Staging")
        .task { await load() }
        .refreshable { await load() }
        .alert("Staging-Kopie endgültig entfernen?", isPresented: Binding(get: { selected != nil }, set: { if !$0 { selected = nil; password = "" } })) {
            SecureField("Aktuelles Serverpasswort", text: $password)
            Button("Staging löschen", role: .destructive) {
                guard let id = selected?.id else { return }
                let currentPassword = password
                password = ""
                selected = nil
                Task { await remove(id: id, password: currentPassword) }
            }
            .disabled(password.isEmpty)
            Button("Abbrechen", role: .cancel) { password = ""; selected = nil }
        } message: { Text("Die zurückgeholten Dateien dieses Stagings werden gelöscht. Produktive Datenwege bleiben unverändert.") }
    }

    private func load() async {
        busy = true
        defer { busy = false }
        do { items = try await model.withCurrentClient { try await $0.getRecoveryStaging() }.items; errorMessage = nil }
        catch { errorMessage = error.localizedDescription }
    }

    private func remove(id: String, password: String) async {
        busy = true
        defer { busy = false }
        do {
            _ = try await model.withCurrentClient { try await $0.removeRecoveryStaging(id: id, currentPassword: password) }
            await load()
        } catch { errorMessage = error.localizedDescription }
    }
}
