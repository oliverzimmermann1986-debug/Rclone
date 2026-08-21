import SwiftUI

struct LegacyJobsScreen: View {
    @EnvironmentObject private var model: AppModel
    @Binding var showingSettings: Bool
    @State private var selectedPair: PairHealth?

    var body: some View {
        JobsListView(selectedPair: $selectedPair)
        .navigationTitle("Jobs")
        .toolbar {
            ToolbarItem(placement: .topBarLeading) { SettingsButton(showingSettings: $showingSettings) }
            ToolbarItem(placement: .topBarTrailing) {
                Button { Task { await model.refresh() } } label: { Image(systemName: "arrow.clockwise") }
                    .disabled(model.isRefreshing)
                    .accessibilityLabel("Aktualisieren")
            }
        }
        .confirmationDialog("„\(selectedPair?.name ?? "")“ jetzt ausführen?", isPresented: Binding(
            get: { selectedPair != nil },
            set: { if !$0 { selectedPair = nil } }
        ), titleVisibility: .visible) {
            Button("Sicherung starten") {
                let name = selectedPair?.name
                selectedPair = nil
                Task { _ = await model.runBackup(pair: name) }
            }
            Button("Probelauf starten") {
                let name = selectedPair?.name
                selectedPair = nil
                Task { _ = await model.runBackup(pair: name, dryRun: true) }
            }
            Button("Abbrechen", role: .cancel) { selectedPair = nil }
        } message: {
            Text("Ein Probelauf verändert keine Dateien und eignet sich zur sicheren Prüfung.")
        }
    }
}

private struct JobsListView: View {
    @EnvironmentObject private var model: AppModel
    @Binding var selectedPair: PairHealth?

    var body: some View {
        List {
            if let overview = model.overview {
                let scheduler = overview.services.scheduler
                Section {
                    HStack {
                        Label(scheduler.control?.paused == true ? "Zeitpläne pausiert" : "Zeitpläne aktiv", systemImage: scheduler.control?.paused == true ? "pause.circle.fill" : "calendar.badge.clock")
                        Spacer()
                        StatusBadge(status: scheduler.control?.paused == true ? "warning" : scheduler.active)
                    }
                }
                Section("Geplante Jobs") {
                    if overview.pairs.health.isEmpty {
                        ContentUnavailableView("Keine Jobs", systemImage: "calendar.badge.exclamationmark", description: Text("Sobald ein Job eingerichtet ist, erscheint er hier."))
                    } else {
                        ForEach(overview.pairs.health) { pair in
                            Button { selectedPair = pair } label: {
                                JobRow(pair: pair)
                            }
                            .buttonStyle(.plain)
                        }
                    }
                }
            } else {
                switch model.overviewState {
                case let .failed(message):
                    LoadFailureView(title: "Jobs nicht geladen", message: message) {
                        Task { await model.refresh() }
                    }
                default:
                    LoadingSection(label: "Jobs werden geladen …")
                }
            }
        }
        .listStyle(.insetGrouped)
        .refreshable { await model.refresh() }
    }
}

private struct JobRow: View {
    let pair: PairHealth

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: pair.overdue == true ? "calendar.badge.exclamationmark" : "calendar.badge.clock")
                .font(.title3)
                .foregroundStyle(pair.overdue == true ? Color.orange : Color.green)
                .frame(width: 30)
            VStack(alignment: .leading, spacing: 4) {
                Text(pair.name).font(.headline)
                Text(pair.schedule.isEmpty || pair.schedule == "manual" ? "Nur manuell" : "Nächster Lauf: \(AppFormat.date(pair.nextRun))")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            StatusBadge(status: pair.lastStatus)
        }
        .contentShape(Rectangle())
        .accessibilityHint("Öffnet die Startoptionen")
    }
}

struct RunsScreen: View {
    @EnvironmentObject private var model: AppModel
    @Binding var showingSettings: Bool

    var body: some View {
        RunsListView()
            .navigationTitle("Läufe")
            .toolbar {
                ToolbarItem(placement: .topBarLeading) { SettingsButton(showingSettings: $showingSettings) }
            }
    }
}

private struct RunsListView: View {
    @EnvironmentObject private var model: AppModel
    @State private var jobs: [JobRecord] = []
    @State private var total = 0
    @State private var query = ""
    @State private var kind = ""
    @State private var status = ""
    @State private var loadingGeneration: Int?
    @State private var requestGeneration = 0
    @State private var errorMessage: String?
    @State private var exportURL: URL?
    private let pageSize = 50

    var body: some View {
        List {
            Section("Filter") {
                Picker("Typ", selection: $kind) {
                    Text("Alle").tag("")
                    Text("Sicherung").tag("backup")
                    Text("Prüfung").tag("check")
                    Text("Quick Sync").tag("quicksync")
                    Text("Restore-Test").tag("restoretest")
                    Text("PBS").tag("pbs")
                }
                Picker("Status", selection: $status) {
                    Text("Alle").tag("")
                    Text("Läuft").tag("running")
                    Text("Erfolgreich").tag("ok")
                    Text("Fehler").tag("error")
                    Text("Abgebrochen").tag("cancelled")
                    Text("Veraltet").tag("stale")
                    Text("Übersprungen").tag("skipped")
                }
                if let exportURL {
                    ShareLink(item: exportURL) { Label("Gefilterte Läufe als CSV teilen", systemImage: "square.and.arrow.up") }
                } else {
                    Button { Task { await prepareCSV() } } label: { Label("CSV-Export vorbereiten", systemImage: "tablecells") }
                        .disabled(isLoading)
                }
            }
            Section("Läufe (\(total))") {
                ForEach(jobs) { job in
                    NavigationLink { RunDetailView(job: job) } label: { RunRow(job: job) }
                }
                if jobs.count < total {
                    Button { Task { await loadMore() } } label: {
                        HStack { Spacer(); if isLoading { ProgressView() }; Text("Weitere laden"); Spacer() }
                    }
                    .disabled(isLoading)
                } else if jobs.isEmpty && !isLoading {
                    ContentUnavailableView("Keine passenden Läufe", systemImage: "clock.arrow.circlepath", description: Text("Passe Suche oder Filter an."))
                }
            }
            if let errorMessage { Label(errorMessage, systemImage: "exclamationmark.triangle").foregroundStyle(.red) }
        }
        .listStyle(.insetGrouped)
        .searchable(text: $query, prompt: "ID, Definition oder Fehler suchen")
        .onSubmit(of: .search) { Task { await reload() } }
        .onChange(of: query) { _, _ in exportURL = nil }
        .onChange(of: kind) { _, _ in Task { await reload() } }
        .onChange(of: status) { _, _ in Task { await reload() } }
        .refreshable { await reload() }
        .task { if jobs.isEmpty { await reload() } }
    }

    private func reload() async {
        requestGeneration += 1
        let generation = requestGeneration
        jobs = []
        total = 0
        exportURL = nil
        await loadMore(generation: generation)
    }

    private var isLoading: Bool { loadingGeneration != nil }

    private func loadMore(generation suppliedGeneration: Int? = nil) async {
        let generation = suppliedGeneration ?? requestGeneration
        guard loadingGeneration != generation else { return }
        loadingGeneration = generation
        let selectedKind = kind
        let selectedStatus = status
        let selectedQuery = query.trimmingCharacters(in: .whitespacesAndNewlines)
        let offset = jobs.count
        defer { if loadingGeneration == generation { loadingGeneration = nil } }
        do {
            let response = try await model.withCurrentClient {
                try await $0.searchJobs(
                    kind: selectedKind.isEmpty ? nil : selectedKind,
                    status: selectedStatus.isEmpty ? nil : selectedStatus,
                    query: selectedQuery,
                    limit: pageSize,
                    offset: offset
                )
            }
            guard generation == requestGeneration else { return }
            jobs.append(contentsOf: response.items)
            total = response.total
            errorMessage = nil
        } catch is CancellationError {
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func prepareCSV() async {
        do {
            exportURL = try await model.withCurrentClient {
                try await $0.downloadJobsCSV(
                    kind: kind.isEmpty ? nil : kind,
                    status: status.isEmpty ? nil : status,
                    query: query.trimmingCharacters(in: .whitespacesAndNewlines)
                )
            }
        } catch is CancellationError {
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}

private struct RunRow: View {
    let job: JobRecord

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: symbol)
                .font(.title3).foregroundStyle(StatusStyle.color(for: job.status)).frame(width: 30)
            VStack(alignment: .leading, spacing: 4) {
                Text(label).font(.headline)
                Text("\(AppFormat.date(job.startedAt)) · \(AppFormat.duration(start: job.startedAt, end: job.endedAt))")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            StatusBadge(status: job.status)
        }
    }

    private var label: String {
        switch job.kind { case "backup": "Sicherung #\(job.id)"; case "check": "Prüfung #\(job.id)"; case "pbs": "PBS #\(job.id)"; default: "Lauf #\(job.id)" }
    }
    private var symbol: String { job.kind == "check" ? "checkmark.shield" : "arrow.triangle.2.circlepath" }
}

struct RunDetailView: View {
    @EnvironmentObject private var model: AppModel
    let job: JobRecord
    @State private var detail: JobRecord?
    @State private var log = ""
    @State private var isDetailLoading = true
    @State private var isLogLoading = true
    @State private var detailError: String?
    @State private var logError: String?
    @State private var logDownloadURL: URL?

    var body: some View {
        List {
            Section("Status") {
                LabeledContent("Ergebnis") { StatusBadge(status: detail?.status ?? job.status) }
                LabeledContent("Gestartet", value: AppFormat.date(detail?.startedAt ?? job.startedAt))
                LabeledContent("Dauer", value: AppFormat.duration(start: detail?.startedAt ?? job.startedAt, end: detail?.endedAt ?? job.endedAt))
                LabeledContent("Typ", value: (detail?.kind ?? job.kind).uppercased())
                if isDetailLoading { ProgressView("Metadaten werden aktualisiert …") }
                if let detailError { Text(detailError).font(.caption).foregroundStyle(.orange) }
            }
            Section("Protokoll") {
                if isLogLoading {
                    ProgressView("Protokoll wird geladen …")
                } else if let logError {
                    LoadFailureView(title: "Protokoll nicht geladen", message: logError) {
                        Task { await loadLog() }
                    }
                } else if log.isEmpty {
                    Text("Kein Protokoll verfügbar").foregroundStyle(.secondary)
                } else {
                    ScrollView(.horizontal) {
                        Text(log)
                            .font(.system(.caption, design: .monospaced))
                            .textSelection(.enabled)
                    }
                }
                if let logDownloadURL {
                    ShareLink(item: logDownloadURL) { Label("Vollständiges redigiertes Log teilen", systemImage: "square.and.arrow.up") }
                } else {
                    Button { Task { await prepareLogDownload() } } label: { Label("Vollständiges Log vorbereiten", systemImage: "doc.badge.arrow.up") }
                }
            }
        }
        .navigationTitle("Lauf #\(job.id)")
        .navigationBarTitleDisplayMode(.inline)
        .task {
            async let detailTask: Void = loadDetail()
            async let logTask: Void = loadLog()
            _ = await (detailTask, logTask)
        }
    }

    private func loadDetail() async {
        isDetailLoading = true
        detailError = nil
        defer { isDetailLoading = false }
        do {
            detail = try await model.withCurrentClient { try await $0.getJob(id: job.id) }
        } catch is CancellationError {
        } catch {
            detailError = error.localizedDescription
        }
    }

    private func loadLog() async {
        isLogLoading = true
        logError = nil
        defer { isLogLoading = false }
        do {
            log = try await model.withCurrentClient { try await $0.getJobLog(id: job.id) }.log
        } catch is CancellationError {
        } catch {
            logError = error.localizedDescription
        }
    }

    private func prepareLogDownload() async {
        do {
            logDownloadURL = try await model.withCurrentClient { try await $0.downloadJobLog(id: job.id) }
        } catch is CancellationError {
        } catch {
            logError = error.localizedDescription
        }
    }
}

struct LegacyDataPathsScreen: View {
    @EnvironmentObject private var model: AppModel
    @Binding var showingSettings: Bool

    var body: some View {
        DataPathsListView()
            .navigationTitle("Datenwege")
            .toolbar {
                ToolbarItem(placement: .topBarLeading) { SettingsButton(showingSettings: $showingSettings) }
                ToolbarItem(placement: .topBarTrailing) {
                    Button { Task { await model.refresh() } } label: { Image(systemName: "arrow.clockwise") }
                        .disabled(model.isRefreshing)
                        .accessibilityLabel("Aktualisieren")
                }
            }
    }
}

private struct DataPathsListView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        List {
            if let pairs = model.config?.backup.pairs, !pairs.isEmpty {
                ForEach(pairs) { pair in
                    NavigationLink { DataPathDetailView(pair: pair, storage: model.storage?.pairs.first { $0.name == pair.name }) } label: {
                        VStack(alignment: .leading, spacing: 5) {
                            HStack {
                                Text(pair.name).font(.headline)
                                Spacer()
                                if pair.enabled == false { Text("Aus").font(.caption.bold()).foregroundStyle(.secondary) }
                            }
                            Label(pair.local, systemImage: "folder").font(.caption).foregroundStyle(.secondary).lineLimit(1)
                            Label(pair.remote, systemImage: "icloud").font(.caption).foregroundStyle(.secondary).lineLimit(1)
                        }
                        .padding(.vertical, 3)
                    }
                }
            } else if model.configState == .loaded {
                ContentUnavailableView("Keine Datenwege", systemImage: "arrow.left.arrow.right", description: Text("Eingerichtete Verbindungen zwischen lokalen und entfernten Ordnern erscheinen hier."))
            } else {
                switch model.configState {
                case let .failed(message):
                    LoadFailureView(title: "Datenwege nicht geladen", message: message) {
                        Task { await model.refresh() }
                    }
                default:
                    LoadingSection(label: "Datenwege werden geladen …")
                }
            }
        }
        .listStyle(.insetGrouped)
        .refreshable { await model.refresh() }
    }
}

private struct DataPathDetailView: View {
    let pair: PairConfig
    let storage: StoragePair?

    var body: some View {
        List {
            Section("Datenweg") {
                LabeledContent("Lokal", value: pair.local)
                LabeledContent("Cloud", value: pair.remote)
                LabeledContent("Richtung", value: pair.direction.uppercased())
                LabeledContent("Modus", value: pair.mode.uppercased())
            }
            Section("Bestand") {
                LabeledContent("Quelle Dateien", value: AppFormat.count(storage?.sourceSize?.count))
                LabeledContent("Quelle Größe", value: AppFormat.bytes(storage?.sourceSize?.bytes))
                LabeledContent("Ziel Dateien", value: AppFormat.count(storage?.targetSize?.count))
                LabeledContent("Ziel Größe", value: AppFormat.bytes(storage?.targetSize?.bytes))
            }
            Section("Schutz") {
                Label(pair.allowDelete == true ? "Löschungen freigegeben" : "Löschungen gesperrt", systemImage: pair.allowDelete == true ? "trash.circle" : "lock.shield")
                    .foregroundStyle(pair.allowDelete == true ? Color.orange : Color.green)
            }
        }
        .navigationTitle(pair.name)
        .navigationBarTitleDisplayMode(.inline)
    }
}
