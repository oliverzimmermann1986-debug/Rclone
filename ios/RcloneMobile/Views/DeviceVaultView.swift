import PhotosUI
import CoreTransferable
import SwiftUI
import UniformTypeIdentifiers

struct DeviceVaultView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.scenePhase) private var scenePhase
    @StateObject private var transfer = VaultTransferModel()
    @State private var selectedIdentity = ""
    @State private var photoItems: [PhotosPickerItem] = []
    @State private var showingFileImporter = false
    @State private var restoredURL: URL?
    @State private var restoringItemID: String?
    @State private var restoreTask: Task<Void, Never>?
    @State private var inboxItems: [VaultInboxItem] = []
    @State private var isImporting = false
    @State private var isVisible = false
    @State private var exportIsVerified = true
    @State private var pendingReassignment: VaultQueueEntry?
    @State private var pendingReassignmentScope: VaultQueueScope?

    private var pairs: [PairConfig] {
        (model.config?.backup.pairs ?? []).filter(\.enabled)
    }

    var body: some View {
        lifecycleContent
            .alert("Geräte-Vault", isPresented: isErrorPresented) {
                Button("OK") { transfer.errorMessage = nil }
            } message: {
                Text(transfer.errorMessage ?? "")
            }
            .confirmationDialog("Neue Zielzuordnung übernehmen?", isPresented: isReassignmentPresented,
                                titleVisibility: .visible) {
                reassignmentButtons
            } message: {
                Text(reassignmentMessage)
            }
            .safeAreaInset(edge: .bottom) { exportFooter }
    }

    private var lifecycleContent: some View {
        navigationContent
            .fileImporter(isPresented: $showingFileImporter, allowedContentTypes: [.item],
                          allowsMultipleSelection: true, onCompletion: handleImportedFiles)
            .onChange(of: photoItems) { _, items in handlePhotoSelection(items) }
            .onAppear(perform: viewAppeared)
            .onChange(of: pairs.map(\.id)) { _, _ in selectDefaultPair() }
            .onChange(of: selectedIdentity) { _, _ in selectedIdentityChanged() }
            .onChange(of: activeScopeKeys) { _, _ in activeScopesChanged() }
            .onChange(of: scenePhase) { _, phase in scenePhaseChanged(phase) }
            .onDisappear(perform: viewDisappeared)
            .task { await loadLibrary() }
    }

    private var navigationContent: some View {
        vaultList
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
    }

    private var vaultList: some View {
        List {
            heroSection
            destinationSection
            importSection
            inboxSection
            importProgressSection
            unassignedSection
            queueSection
            currentTransferSection
            librarySection
        }
    }

    private var heroSection: some View {
        Section {
                VaultHeroCard()
                    .listRowInsets(EdgeInsets())
                    .listRowBackground(Color.clear)
        }
    }

    private var destinationSection: some View {
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
                    .disabled(transfer.isWorking || isImporting)
                    Text("Die Datei landet getrennt unter „Sicherpfad“, nicht in deiner Live-Quelle.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
        }
    }

    private var importSection: some View {
        Section {
                PhotosPicker(
                    selection: $photoItems,
                    maxSelectionCount: 100,
                    matching: .images
                ) {
                    Label("Fotos auswählen", systemImage: "photo.on.rectangle.angled")
                }
                .disabled(selectedIdentity.isEmpty || transfer.isWorking || isImporting || model.isDemoMode)

                Button { showingFileImporter = true } label: {
                    Label("Dateien auswählen", systemImage: "folder.badge.plus")
                }
                .disabled(selectedIdentity.isEmpty || transfer.isWorking || isImporting || model.isDemoMode)

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
    }

    @ViewBuilder
    private var inboxSection: some View {
            if !inboxItems.isEmpty, !model.isDemoMode {
                Section {
                    ForEach(inboxItems) { item in
                        VaultInboxRow(item: item)
                    }
                    Button {
                        Task { await importInbox() }
                    } label: {
                        Label("\(inboxItems.count) Dateien für „\(selectedPairName)“ übernehmen", systemImage: "tray.and.arrow.down")
                    }
                    .disabled(selectedIdentity.isEmpty || transfer.isWorking || isImporting)
                } header: {
                    Text("Aus dem Teilen-Menü")
                } footer: {
                    Text("Diese Dateien sind nur lokal vorgemerkt. Du bestimmst jetzt den Datenweg; danach startet die Sicherung.")
                }
            }
    }

    @ViewBuilder
    private var importProgressSection: some View {
            if isImporting {
                Section { ProgressView("Dateien geschützt vormerken …") }
            }
    }

    @ViewBuilder
    private var unassignedSection: some View {
            if !transfer.unassignedQueue.isEmpty, !model.isDemoMode {
                Section {
                    ForEach(transfer.unassignedQueue) { entry in
                        VaultUnassignedRow(
                            entry: entry,
                            canReassign: queueScope != nil && !transfer.isWorking && !isImporting,
                            onExport: {
                                Task { await exportQueued(entry) }
                            },
                            onReassign: { requestReassignment(entry) }
                        )
                    }
                } header: {
                    Text("Zuordnung prüfen")
                } footer: {
                    Text("Ein Datenweg wurde geändert oder entfernt. Die lokalen Dateien sind erhalten und werden erst nach deiner neuen Zuordnung übertragen.")
                }
            }
    }

    @ViewBuilder
    private var queueSection: some View {
            if !transfer.queue.isEmpty, !model.isDemoMode {
                Section {
                    ForEach(transfer.queue, id: \.id) { (entry: VaultQueueEntry) in
                        queuedRow(entry)
                    }
                    if transfer.isWorking {
                        Button("Übertragung pausieren", systemImage: "pause.circle") { transfer.pause() }
                    } else {
                        Button("Warteschlange fortsetzen", systemImage: "play.circle") { Task { await resumeQueue() } }
                            .disabled(isImporting)
                    }
                } header: {
                    Text("Warteschlange · \(transfer.queue.count)")
                } footer: {
                    Text("Vorgemerkte Dateien und Fortschritt bleiben bei einem Neustart erhalten. Zum Übertragen die App geöffnet lassen. Verwerfen entfernt nur die lokale Vormerkung.")
                }
            }
    }

    private func queuedRow(_ entry: VaultQueueEntry) -> some View {
        VaultQueuedRow(entry: entry, isActive: entry.id == transfer.activeEntryID)
            .swipeActions {
                if !transfer.isWorking, queueScope != nil {
                    Button("Verwerfen", role: .destructive) { removeQueued(entry) }
                }
            }
    }

    @ViewBuilder
    private var currentTransferSection: some View {
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
    }

    private var librarySection: some View {
            Section("Wiederherstellbare Dateien") {
                if transfer.library.isEmpty {
                    ContentUnavailableView(
                        "Noch keine Geräte-Dateien",
                        systemImage: "iphone.and.arrow.forward",
                        description: Text("Verifizierte Fotos und Dateien erscheinen hier.")
                    )
                } else {
                    ForEach(transfer.library) { item in
                        Button { startRestore(item) } label: {
                            VaultLibraryRow(item: item, isRestoring: restoringItemID == item.id)
                        }
                        .buttonStyle(.plain)
                        .disabled(restoringItemID != nil || !(item.verified || item.status == "remote"))
                        .accessibilityHint("Holt die Datei zurück und prüft sie, bevor sie geteilt werden kann.")
                    }
                }
                if restoringItemID != nil {
                    Text("Falls die lokale Kopie fehlt, wird zuerst die Cloud-Kopie vollständig geladen. Bei großen Dateien kann das mehrere Minuten dauern.")
                        .font(.caption).foregroundStyle(.secondary)
                    Button("Download abbrechen", role: .cancel) { restoreTask?.cancel() }
                }
            }
    }

    private func handleImportedFiles(_ result: Result<[URL], Error>) {
        switch result {
            case let .success(urls):
                Task { await importFiles(urls) }
            case let .failure(error):
                transfer.errorMessage = error.localizedDescription
        }
    }

    private func handlePhotoSelection(_ items: [PhotosPickerItem]) {
        guard !items.isEmpty else { return }
        Task { await importPhotos(items) }
    }

    private func importPhotos(_ items: [PhotosPickerItem]) async {
                guard let scope = queueScope else { return }
                isImporting = true
                var failures = 0
                for (index, item) in items.enumerated() {
                    do {
                        guard let photo = try await item.loadTransferable(type: VaultPhotoSelection.self) else {
                            throw CocoaError(.fileReadUnknown)
                        }
                        defer { try? FileManager.default.removeItem(at: photo.url) }
                        let fileExtension = photo.url.pathExtension.isEmpty ? "heic" : photo.url.pathExtension
                        let filename = "Foto-\(Int(Date().timeIntervalSince1970))-\(index + 1).\(fileExtension)"
                        try await transfer.enqueue(fileURL: photo.url, filename: filename, sourceType: "photo", scope: scope)
                    } catch {
                        failures += 1
                    }
                }
                isImporting = false
                photoItems = []
                if failures > 0 { transfer.errorMessage = "\(failures) von \(items.count) Fotos konnten nicht vorgemerkt werden. Die übrigen stehen in der Warteschlange." }
                else if queueScope == scope { await resumeQueue() }
    }

    private func viewAppeared() { isVisible = true; selectDefaultPair() }
    private func viewDisappeared() { isVisible = false; transfer.pause(); restoreTask?.cancel() }
    private func selectedIdentityChanged() {
        restoreTask?.cancel()
        restoredURL = nil
        Task { await loadLibrary() }
    }
    private func activeScopesChanged() {
        transfer.pause()
        if let scope = queueContextScope { transfer.loadQueue(scope: scope, activeScopeKeys: activeScopeKeys) }
    }
    private func scenePhaseChanged(_ phase: ScenePhase) {
        if phase == .background { transfer.pause() }
        if phase == .active { loadInbox() }
    }
    private func startRestore(_ item: VaultUploadStatus) {
        restoreTask = Task { await restore(item) }
    }
    private func removeQueued(_ entry: VaultQueueEntry) {
        guard !transfer.isWorking, let scope = queueScope else { return }
        transfer.removeQueued(entry, scope: scope)
    }
    private func requestReassignment(_ entry: VaultQueueEntry) {
        pendingReassignmentScope = queueScope
        pendingReassignment = entry
    }

    private var isErrorPresented: Binding<Bool> {
        Binding(
            get: { transfer.errorMessage != nil },
            set: { if !$0 { transfer.errorMessage = nil } }
        )
    }

    private var isReassignmentPresented: Binding<Bool> {
        Binding(
            get: { pendingReassignment != nil },
            set: { if !$0 { pendingReassignment = nil; pendingReassignmentScope = nil } }
        )
    }

    @ViewBuilder
    private var reassignmentButtons: some View {
            if let entry = pendingReassignment, let scope = pendingReassignmentScope {
                Button("Für dieses Ziel vormerken") {
                    Task { await performReassignment(entry, to: scope) }
                }
            }
            Button("Abbrechen", role: .cancel) {}
    }

    private func performReassignment(_ entry: VaultQueueEntry, to scope: VaultQueueScope) async {
        guard queueScope == scope else { transfer.errorMessage = "Der Datenweg hat sich geändert. Bitte prüfe die Zuordnung erneut."; return }
        do { try await transfer.reassign(entry, to: scope) }
        catch { transfer.errorMessage = error.localizedDescription }
    }

    private var reassignmentMessage: String {
        "\(pendingReassignment?.filename ?? "Datei")\nBisher: \(pendingReassignment?.destinationDescription ?? "Unbekannt")\nNeu: \(pendingReassignmentScope?.destinationDescription ?? "Unbekannt")\nDie alte Serverübertragung bleibt unverändert. Fortsetzen startet eine neue Übertragung für das gewählte Ziel."
    }

    @ViewBuilder
    private var exportFooter: some View {
            if let restoredURL {
                ShareLink(item: restoredURL) {
                    Label(exportIsVerified ? "Geprüfte Datei in Dateien sichern" : "Lokale Vormerkung exportieren", systemImage: "square.and.arrow.up")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                .tint(.green)
                .padding()
                .background(.bar)
            }
    }

    private var selectedPairName: String {
        pairs.first(where: { $0.id == selectedIdentity })?.name ?? "Fotos"
    }

    private func selectDefaultPair() {
        guard !pairs.contains(where: { $0.id == selectedIdentity }) else { return }
        selectedIdentity = pairs.first?.id ?? ""
    }

    private var queueScope: VaultQueueScope? {
        pairs.first(where: { $0.id == selectedIdentity }).flatMap { scope(for: $0) }
    }

    private var activeScopeKeys: Set<String> { Set(pairs.compactMap { scope(for: $0)?.key }) }

    private var queueContextScope: VaultQueueScope? {
        if let queueScope { return queueScope }
        guard !model.isDemoMode, let server = try? APIClient.normalizedServerURL(model.serverAddress) else { return nil }
        return VaultQueueScope(serverURL: server, username: model.savedUsername, pairID: "", source: "", target: "", direction: "push")
    }

    private func scope(for pair: PairConfig) -> VaultQueueScope? {
        guard !model.isDemoMode,
              let server = try? APIClient.normalizedServerURL(model.serverAddress) else { return nil }
        return VaultQueueScope(serverURL: server, username: model.savedUsername, pairID: pair.id,
            source: pair.local, target: pair.remote, direction: pair.direction)
    }

    private func importFiles(_ urls: [URL]) async {
        guard let scope = queueScope else { return }
        isImporting = true
        var failures = 0
        for url in urls {
            do { try await transfer.enqueue(fileURL: url, filename: url.lastPathComponent, sourceType: "file", scope: scope) }
            catch { failures += 1 }
        }
        isImporting = false
        if failures > 0 { transfer.errorMessage = "\(failures) von \(urls.count) Dateien konnten nicht vorgemerkt werden. Die übrigen stehen in der Warteschlange." }
        else if queueScope == scope { await resumeQueue() }
    }

    private func importInbox() async {
        guard let scope = queueScope, let inbox = try? VaultInbox() else { return }
        isImporting = true
        defer { isImporting = false; loadInbox() }
        do {
            for item in inboxItems {
                try await transfer.enqueue(fileURL: inbox.payload(for: item), filename: item.filename,
                    sourceType: item.sourceType, scope: scope, id: item.id)
                try inbox.remove(item)
            }
            if queueScope == scope { await resumeQueue() }
        } catch { transfer.errorMessage = error.localizedDescription }
    }

    private func loadInbox() {
        do { inboxItems = try VaultInbox().items() }
        catch { transfer.errorMessage = error.localizedDescription }
    }

    private func resumeQueue() async {
        guard isVisible, scenePhase == .active, let scope = queueScope else { return }
        do {
            try await model.withCurrentClient { client in
                await transfer.resumeQueue(scope: scope, using: client) {
                    isVisible && scenePhase == .active && queueScope == scope && model.phase == .signedIn
                }
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
        loadInbox()
        if let scope = queueContextScope { transfer.loadQueue(scope: scope, activeScopeKeys: activeScopeKeys) }
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
        guard let scope = queueScope, restoringItemID == nil else { return }
        restoringItemID = item.id
        defer { restoringItemID = nil }
        // A previous local export must never inherit this download's verification label.
        restoredURL = nil
        exportIsVerified = false
        do {
            let url = try await model.withCurrentClient {
                try await $0.downloadVaultItem(id: item.id, filename: item.filename)
            }
            try Task.checkCancellation()
            guard queueScope == scope else { return }
            restoredURL = url
            exportIsVerified = true
        } catch {
            guard !Task.isCancelled else { return }
            guard queueScope == scope else { return }
            transfer.errorMessage = error.localizedDescription
        }
        // Cloud fallback can change verification/storage state, including on failure.
        await loadLibrary()
    }

    private func exportQueued(_ entry: VaultQueueEntry) async {
        guard let scope = queueContextScope else { return }
        do {
            let url = try await transfer.export(entry, scope: scope)
            guard queueContextScope?.accountKey == scope.accountKey else { return }
            exportIsVerified = false
            restoredURL = url
        } catch { transfer.errorMessage = error.localizedDescription }
    }
}

private struct VaultQueuedRow: View {
    let entry: VaultQueueEntry
    let isActive: Bool

    private var formattedSize: String { AppFormat.bytes(entry.size) }
    private var statusText: String {
        entry.lastError ?? (isActive ? "Wird übertragen" : "Zum Fortsetzen bereit")
    }
    private var statusColor: Color { entry.lastError == nil ? .secondary : .orange }
    private var progress: Double {
        entry.size > 0 ? Double(entry.received) / Double(entry.size) : 0
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(entry.filename).lineLimit(2)
                Spacer()
                Text(formattedSize).font(.caption).foregroundStyle(.secondary)
            }
            Text(statusText).font(.caption).foregroundStyle(statusColor)
            ProgressView(value: progress)
        }
    }
}

private struct VaultInboxRow: View {
    let item: VaultInboxItem
    private var formattedSize: String { AppFormat.bytes(item.size) }
    private var symbol: String { item.sourceType == "photo" ? "photo" : "doc" }

    var body: some View {
        HStack {
            Label(item.filename, systemImage: symbol)
            Spacer()
            Text(formattedSize).font(.caption).foregroundStyle(.secondary)
        }
    }
}

private struct VaultUnassignedRow: View {
    let entry: VaultQueueEntry
    let canReassign: Bool
    let onExport: () -> Void
    let onReassign: () -> Void

    private var previousTarget: String {
        "Bisheriges Ziel: \(entry.destinationDescription ?? "Nicht mehr zugeordnet")"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(entry.filename).font(.body.weight(.medium))
            Text(previousTarget).font(.caption).foregroundStyle(.secondary)
            Button("Lokale Datei exportieren", systemImage: "square.and.arrow.up", action: onExport)
            Button("Neues Ziel zuordnen", systemImage: "point.3.connected.trianglepath.dotted", action: onReassign)
                .disabled(!canReassign)
        }
    }
}

private struct VaultLibraryRow: View {
    let item: VaultUploadStatus
    let isRestoring: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            VaultTransferRow(item: item, showPath: false)
            if isRestoring {
                ProgressView("Datei wird zurückgeholt und geprüft …")
            } else if item.status == "remote" {
                Text("Aus Notfallakte · zum Zurückholen und Prüfen öffnen")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
    }
}

private struct VaultPhotoSelection: Transferable {
    let url: URL

    static var transferRepresentation: some TransferRepresentation {
        FileRepresentation(importedContentType: .image) { received in
            let suffix = received.file.pathExtension.isEmpty ? "heic" : received.file.pathExtension
            let staged = FileManager.default.temporaryDirectory.appendingPathComponent("sicherpfad-photo-\(UUID().uuidString).\(suffix)")
            try FileManager.default.copyItem(at: received.file, to: staged)
            try FileManager.default.setAttributes([.protectionKey: FileProtectionType.complete], ofItemAtPath: staged.path)
            return VaultPhotoSelection(url: staged)
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
