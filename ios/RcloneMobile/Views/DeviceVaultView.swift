import PhotosUI
import SwiftUI
import UniformTypeIdentifiers

struct DeviceVaultView: View {
    @EnvironmentObject private var model: AppModel
    @StateObject private var transfer = VaultTransferModel()
    @State private var selectedIdentity = ""
    @State private var photoItems: [PhotosPickerItem] = []
    @State private var showingFileImporter = false
    @State private var restoredURL: URL?

    private var pairs: [PairConfig] {
        (model.config?.backup.pairs ?? []).filter(\.enabled)
    }

    var body: some View {
        List {
            Section {
                VaultHeroCard()
                    .listRowInsets(EdgeInsets())
                    .listRowBackground(Color.clear)
            }

            Section("Ziel") {
                if pairs.isEmpty {
                    ContentUnavailableView(
                        "Kein Datenweg verfügbar",
                        systemImage: "point.3.connected.trianglepath.dotted",
                        description: Text("Lege zuerst einen aktiven Datenweg an.")
                    )
                } else {
                    Picker("Datenweg", selection: $selectedIdentity) {
                        ForEach(pairs) { pair in
                            Text(pair.name).tag(pair.id)
                        }
                    }
                    Text("Die Datei landet getrennt unter „Sicherpfad“, nicht in deiner Live-Quelle.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }

            Section {
                PhotosPicker(
                    selection: $photoItems,
                    maxSelectionCount: 20,
                    matching: .images
                ) {
                    Label("Fotos auswählen", systemImage: "photo.on.rectangle.angled")
                }
                .disabled(selectedIdentity.isEmpty || transfer.isWorking || model.isDemoMode)

                Button { showingFileImporter = true } label: {
                    Label("Dateien auswählen", systemImage: "folder.badge.plus")
                }
                .disabled(selectedIdentity.isEmpty || transfer.isWorking || model.isDemoMode)

                if model.isDemoMode {
                    Button {
                        Task { await transfer.simulateDemoUpload(identity: selectedPairName) }
                    } label: {
                        Label("Demo-Sicherung abspielen", systemImage: "play.rectangle.on.rectangle")
                    }
                    .disabled(transfer.isWorking)
                }
            } header: {
                Text("Vom iPhone sichern")
            } footer: {
                Text("Jede Datei wird in Blöcken übertragen, per SHA‑256 dedupliziert und nach dem Schreiben vom Ziel zurückgelesen.")
            }

            if let current = transfer.current {
                Section("Aktuelle Übertragung") {
                    VaultTransferRow(item: current, showPath: true)
                    ProgressView(value: current.fractionCompleted)
                        .tint(current.status == "error" ? .red : .green)
                    if transfer.isWorking {
                        Text(current.status == "transferring" ? "Zielkopie wird zurückgelesen und geprüft …" : "Upload läuft …")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            }

            Section("Wiederherstellbare Dateien") {
                if transfer.library.isEmpty {
                    ContentUnavailableView(
                        "Noch keine Geräte-Dateien",
                        systemImage: "iphone.and.arrow.forward",
                        description: Text("Verifizierte Fotos und Dateien erscheinen hier.")
                    )
                } else {
                    ForEach(transfer.library) { item in
                        Button { Task { await restore(item) } } label: {
                            VaultTransferRow(item: item, showPath: false)
                        }
                        .buttonStyle(.plain)
                        .disabled(!item.verified)
                        .accessibilityHint("Lädt die geprüfte Datei, um sie in Dateien zu sichern oder zu teilen.")
                    }
                }
            }
        }
        .listStyle(.insetGrouped)
        .navigationTitle("Geräte-Vault")
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Button { Task { await loadLibrary() } } label: {
                    Image(systemName: "arrow.clockwise")
                }
                .disabled(selectedIdentity.isEmpty || model.isDemoMode)
                .accessibilityLabel("Vault aktualisieren")
            }
        }
        .fileImporter(
            isPresented: $showingFileImporter,
            allowedContentTypes: [.item],
            allowsMultipleSelection: true
        ) { result in
            guard case let .success(urls) = result else { return }
            Task {
                for url in urls.prefix(20) {
                    await uploadFile(url, filename: url.lastPathComponent, sourceType: "file")
                }
            }
        }
        .onChange(of: photoItems) { _, items in
            guard !items.isEmpty else { return }
            Task {
                for (index, item) in items.enumerated() {
                    guard let data = try? await item.loadTransferable(type: Data.self) else { continue }
                    let fileExtension = item.supportedContentTypes.first?.preferredFilenameExtension ?? "heic"
                    let filename = "Foto-\(Int(Date().timeIntervalSince1970))-\(index + 1).\(fileExtension)"
                    let temporary = FileManager.default.temporaryDirectory
                        .appendingPathComponent("sicherpfad-photo-\(UUID().uuidString).\(fileExtension)")
                    do {
                        try data.write(to: temporary, options: [.atomic, .completeFileProtection])
                        await uploadFile(temporary, filename: filename, sourceType: "photo")
                    } catch {
                        transfer.errorMessage = error.localizedDescription
                    }
                    try? FileManager.default.removeItem(at: temporary)
                }
                photoItems = []
            }
        }
        .onAppear { selectDefaultPair() }
        .onChange(of: pairs.map(\.id)) { _, _ in selectDefaultPair() }
        .onChange(of: selectedIdentity) { _, _ in Task { await loadLibrary() } }
        .task { await loadLibrary() }
        .alert("Geräte-Vault", isPresented: Binding(
            get: { transfer.errorMessage != nil },
            set: { if !$0 { transfer.errorMessage = nil } }
        )) {
            Button("OK") { transfer.errorMessage = nil }
        } message: {
            Text(transfer.errorMessage ?? "")
        }
        .safeAreaInset(edge: .bottom) {
            if let restoredURL {
                ShareLink(item: restoredURL) {
                    Label("Geprüfte Datei in Dateien sichern", systemImage: "square.and.arrow.up")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                .tint(.green)
                .padding()
                .background(.bar)
            }
        }
    }

    private var selectedPairName: String {
        pairs.first(where: { $0.id == selectedIdentity })?.name ?? "Fotos"
    }

    private func selectDefaultPair() {
        guard !pairs.contains(where: { $0.id == selectedIdentity }) else { return }
        selectedIdentity = pairs.first?.id ?? ""
    }

    private func uploadFile(_ url: URL, filename: String, sourceType: String) async {
        guard !selectedIdentity.isEmpty else { return }
        do {
            try await model.withCurrentClient { client in
                await transfer.upload(
                    fileURL: url,
                    filename: filename,
                    sourceType: sourceType,
                    identity: selectedIdentity,
                    using: client
                )
            }
        } catch {
            transfer.errorMessage = error.localizedDescription
        }
    }

    private func loadLibrary() async {
        if model.isDemoMode {
            if transfer.library.isEmpty { await transfer.simulateDemoUpload(identity: selectedPairName) }
            return
        }
        guard !selectedIdentity.isEmpty else { return }
        do {
            try await model.withCurrentClient { client in
                await transfer.refreshLibrary(identity: selectedIdentity, using: client)
            }
        } catch {
            transfer.errorMessage = error.localizedDescription
        }
    }

    private func restore(_ item: VaultUploadStatus) async {
        do {
            restoredURL = try await model.withCurrentClient {
                try await $0.downloadVaultItem(id: item.id, filename: item.filename)
            }
        } catch {
            transfer.errorMessage = error.localizedDescription
        }
    }
}

private struct VaultHeroCard: View {
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(alignment: .top) {
                Image(systemName: "iphone.and.arrow.forward")
                    .font(.system(size: 30, weight: .semibold))
                    .foregroundStyle(.green)
                    .frame(width: 58, height: 58)
                    .background(.green.opacity(0.13), in: RoundedRectangle(cornerRadius: 18, style: .continuous))
                Spacer()
                Label("Ende-zu-Ende geprüft", systemImage: "checkmark.seal.fill")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.green)
            }
            Text("Dein iPhone wird Teil des Schutzpfads.")
                .font(.title2.bold())
            Text("Fotos und Dateien landen direkt in deinem eigenen Datenweg – wiederaufnehmbar, dedupliziert und nach dem Schreiben verifiziert.")
                .font(.subheadline)
                .foregroundStyle(.secondary)
        }
        .padding(20)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 26, style: .continuous))
        .overlay {
            RoundedRectangle(cornerRadius: 26, style: .continuous)
                .stroke(.green.opacity(0.15), lineWidth: 1)
        }
    }
}

private struct VaultTransferRow: View {
    let item: VaultUploadStatus
    let showPath: Bool

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: item.sourceType == "photo" ? "photo.fill" : "doc.fill")
                .foregroundStyle(item.verified ? .green : .blue)
                .frame(width: 28)
            VStack(alignment: .leading, spacing: 4) {
                Text(item.filename).font(.body.weight(.medium)).lineLimit(2)
                Text("\(AppFormat.bytes(item.size)) · \(item.pair)")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                if showPath {
                    Text(item.targetRelative)
                        .font(.caption2.monospaced())
                        .foregroundStyle(.tertiary)
                        .lineLimit(2)
                }
                if let error = item.error {
                    Text(error).font(.caption).foregroundStyle(.red)
                }
            }
            Spacer()
            Image(systemName: item.verified ? "checkmark.seal.fill" : statusSymbol)
                .foregroundStyle(item.verified ? .green : item.status == "error" ? .red : .secondary)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(item.filename), \(AppFormat.bytes(item.size)), Status \(item.status)")
    }

    private var statusSymbol: String {
        item.status == "error" ? "exclamationmark.triangle.fill" : "ellipsis.circle"
    }
}
