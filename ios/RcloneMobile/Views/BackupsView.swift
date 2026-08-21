import SwiftUI

struct JobsScreen: View {
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
            if let scheduler = model.overview?.services.scheduler {
                Section {
                    HStack {
                        Label(scheduler.control?.paused == true ? "Zeitpläne pausiert" : "Zeitpläne aktiv", systemImage: scheduler.control?.paused == true ? "pause.circle.fill" : "calendar.badge.clock")
                        Spacer()
                        StatusBadge(status: scheduler.control?.paused == true ? "warning" : scheduler.active)
                    }
                }
            }
            Section("Geplante Jobs") {
                let pairs = model.overview?.pairs.health ?? []
                if pairs.isEmpty {
                    ContentUnavailableView("Keine Jobs", systemImage: "calendar.badge.exclamationmark", description: Text("Sobald ein Job eingerichtet ist, erscheint er hier."))
                } else {
                    ForEach(pairs) { pair in
                        Button { selectedPair = pair } label: {
                            JobRow(pair: pair)
                        }
                        .buttonStyle(.plain)
                    }
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
                ToolbarItem(placement: .topBarTrailing) {
                    Button { Task { await model.refresh() } } label: { Image(systemName: "arrow.clockwise") }
                        .disabled(model.isRefreshing)
                        .accessibilityLabel("Aktualisieren")
                }
            }
    }
}

private struct RunsListView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        List {
            if model.jobs.isEmpty {
                ContentUnavailableView("Noch keine Läufe", systemImage: "clock.arrow.circlepath", description: Text("Ausgeführte Sicherungen erscheinen hier."))
            } else {
                ForEach(model.jobs) { job in
                    NavigationLink { RunDetailView(job: job) } label: { RunRow(job: job) }
                }
            }
        }
        .listStyle(.insetGrouped)
        .refreshable { await model.refresh() }
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
    @State private var isLoading = true

    var body: some View {
        List {
            Section("Status") {
                LabeledContent("Ergebnis") { StatusBadge(status: detail?.status ?? job.status) }
                LabeledContent("Gestartet", value: AppFormat.date(detail?.startedAt ?? job.startedAt))
                LabeledContent("Dauer", value: AppFormat.duration(start: detail?.startedAt ?? job.startedAt, end: detail?.endedAt ?? job.endedAt))
                LabeledContent("Typ", value: (detail?.kind ?? job.kind).uppercased())
            }
            Section("Protokoll") {
                if isLoading {
                    ProgressView("Protokoll wird geladen …")
                } else if log.isEmpty {
                    Text("Kein Protokoll verfügbar").foregroundStyle(.secondary)
                } else {
                    ScrollView(.horizontal) {
                        Text(log)
                            .font(.system(.caption, design: .monospaced))
                            .textSelection(.enabled)
                    }
                }
            }
        }
        .navigationTitle("Lauf #\(job.id)")
        .navigationBarTitleDisplayMode(.inline)
        .task { await load() }
    }

    private func load() async {
        guard let client = model.client else { return }
        defer { isLoading = false }
        do {
            async let detailRequest = client.getJob(id: job.id)
            async let logRequest = client.getJobLog(id: job.id)
            let (newDetail, newLog) = try await (detailRequest, logRequest)
            detail = newDetail
            log = newLog.log
        } catch {
            model.errorMessage = error.localizedDescription
        }
    }
}

struct DataPathsScreen: View {
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
            let pairs = model.config?.backup.pairs ?? []
            if pairs.isEmpty {
                ContentUnavailableView("Keine Datenwege", systemImage: "arrow.left.arrow.right", description: Text("Eingerichtete Verbindungen zwischen lokalen und entfernten Ordnern erscheinen hier."))
            } else {
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
                LabeledContent("Richtung", value: (pair.direction ?? "bisync").uppercased())
                LabeledContent("Modus", value: (pair.mode ?? "bisync").uppercased())
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
