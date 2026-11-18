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
    let paths: [String]?

    enum CodingKeys: String, CodingKey {
        case id, pair, status, verified, files, bytes, paths
        case stagingPath = "staging_path"
    }

    init(
        id: String,
        pair: String? = nil,
        status: String? = nil,
        verified: Bool? = nil,
        files: Int? = nil,
        bytes: Int64? = nil,
        stagingPath: String? = nil,
        paths: [String]? = nil
    ) {
        self.id = id
        self.pair = pair
        self.status = status
        self.verified = verified
        self.files = files
        self.bytes = bytes
        self.stagingPath = stagingPath
        self.paths = paths
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(String.self, forKey: .id)
        pair = try container.decodeIfPresent(String.self, forKey: .pair)
        status = try container.decodeIfPresent(String.self, forKey: .status)
        verified = try container.decodeIfPresent(Bool.self, forKey: .verified)
        files = try container.decodeIfPresent(Int.self, forKey: .files)
        bytes = try container.decodeIfPresent(Int64.self, forKey: .bytes)
        stagingPath = try container.decodeIfPresent(String.self, forKey: .stagingPath)
        paths = try? container.decodeIfPresent([String].self, forKey: .paths)
    }
}

struct RecoveryStagingView: View {
    @EnvironmentObject private var model: AppModel
    @State private var items: [RecoveryStagingItem] = []
    @State private var selected: RecoveryStagingItem?
    @State private var diffTarget: RecoveryStagingItem?
    @State private var exportedArchives: [String: URL] = [:]
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
                    Label(
                        item.verified == true ? "Auswahl per Prüfsumme bestätigt" : (item.status ?? "Ungeprüft"),
                        systemImage: item.verified == true ? "checkmark.seal" : "exclamationmark.triangle"
                    )
                    Text("\(item.files ?? 0) Dateien · \(AppFormat.bytes(item.bytes ?? 0))")
                    if let path = item.stagingPath {
                        Text(path).font(.caption).textSelection(.enabled)
                    }
                    Button {
                        Task { await exportArchive(for: item) }
                    } label: {
                        Label("Als ZIP exportieren", systemImage: "arrow.down.doc")
                    }
                    .disabled(busy || item.status == "running")

                    if let exportedURL = exportedArchives[item.id] {
                        ShareLink(item: exportedURL) {
                            Label("ZIP-Archiv teilen", systemImage: "square.and.arrow.up")
                                .foregroundStyle(.green)
                        }
                    }

                    Button {
                        diffTarget = item
                    } label: {
                        Label("Abweichungen prüfen", systemImage: "doc.text.magnifyingglass")
                    }
                    .disabled(busy || item.status == "running")

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
        .sheet(item: $diffTarget) { item in
            NavigationStack {
                StagingDiffInspectorSheet(item: item)
            }
        }
        .alert(
            "Staging-Kopie endgültig entfernen?",
            isPresented: Binding(get: { selected != nil }, set: { if !$0 { selected = nil; password = "" } })
        ) {
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
        } message: {
            Text("Die zurückgeholten Dateien dieses Stagings werden gelöscht. Produktive Datenwege bleiben unverändert.")
        }
    }

    private func load() async {
        busy = true
        defer { busy = false }
        do {
            items = try await model.withCurrentClient { try await $0.getRecoveryStaging() }.items
            errorMessage = nil
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func exportArchive(for item: RecoveryStagingItem) async {
        busy = true
        errorMessage = nil
        defer { busy = false }
        do {
            let url = try await model.withCurrentClient {
                try await $0.downloadRecoveryStagingArchive(recoveryID: item.id)
            }
            exportedArchives[item.id] = url
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func remove(id: String, password: String) async {
        busy = true
        defer { busy = false }
        do {
            _ = try await model.withCurrentClient {
                try await $0.removeRecoveryStaging(id: id, currentPassword: password)
            }
            exportedArchives.removeValue(forKey: id)
            await load()
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}

private struct StagingDiffInspectorSheet: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss
    let item: RecoveryStagingItem
    @State private var relativePath: String
    @State private var diffResult: TextDiffResponse?
    @State private var loading = false
    @State private var errorMessage: String?

    init(item: RecoveryStagingItem) {
        self.item = item
        _relativePath = State(initialValue: item.paths?.first ?? "")
    }

    var body: some View {
        Group {
            if let diffResult {
                LineDiffViewerView(diff: diffResult)
            } else {
                Form {
                    Section("Textdatei mit Live-Bestand vergleichen") {
                        TextField("Relativer Dateipfad (z. B. dokument.txt)", text: $relativePath)
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                        if let candidates = item.paths, !candidates.isEmpty {
                            ForEach(candidates, id: \.self) { candidate in
                                Button(candidate) { relativePath = candidate }
                                    .font(.caption.monospaced())
                            }
                        }
                        Button {
                            Task { await runDiff() }
                        } label: {
                            HStack {
                                if loading { ProgressView() }
                                Label("Abweichungen prüfen", systemImage: "arrow.left.arrow.right")
                            }
                        }
                        .disabled(loading || relativePath.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    }
                    if let errorMessage {
                        Section {
                            Text(errorMessage).font(.footnote).foregroundStyle(.red)
                        }
                    }
                }
                .navigationTitle(item.pair ?? item.id)
            }
        }
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .confirmationAction) {
                Button("Fertig") { dismiss() }
            }
            if diffResult != nil {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Andere Datei") { diffResult = nil }
                }
            }
        }
    }

    private func runDiff() async {
        let trimmed = relativePath.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        loading = true
        errorMessage = nil
        defer { loading = false }
        do {
            diffResult = try await model.withCurrentClient {
                try await $0.getStagingDiff(recoveryID: item.id, path: trimmed)
            }
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}
