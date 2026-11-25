import SwiftUI

struct DataPathsScreen: View {
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var configurationDraft: ConfigurationDraftStore
    @Binding var showingSettings: Bool
    private let cloudPath: String?
    @State private var search = ""
    @State private var editor: PairEditorRequest?
    @State private var showingMultiCloudSetup = false
    @State private var localError: String?
    @State private var currentPassword = ""
    @State private var pendingPathAction: DataPathAction?
    @State private var confirmFullRestoreTest = false

    init(showingSettings: Binding<Bool>, cloudPath: String? = nil) {
        _showingSettings = showingSettings
        self.cloudPath = cloudPath
    }

    private var visiblePairs: [(offset: Int, element: PairConfig)] {
        let query = search.trimmingCharacters(in: .whitespacesAndNewlines)
        return configurationDraft.pairs.enumerated().filter { entry in
            let pair = entry.element
            if let cloudPath, !pair.referencesCloud(cloudPath) { return false }
            return query.isEmpty || [pair.name, pair.local, pair.remote].contains {
                $0.localizedStandardContains(query)
            }
        }
    }

    var body: some View {
        List {
            if let issue = model.configSaveIssue {
                ConfigIssuePanel(
                    issue: issue,
                    password: $currentPassword,
                    reload: { await reload(discardDirty: true) },
                    retryWithPassword: saveWithPassword
                )
            }
            if !model.configWarnings.isEmpty {
                Section("Hinweise") {
                    ForEach(model.configWarnings, id: \.self) { warning in
                        Label(warning, systemImage: "exclamationmark.triangle")
                            .foregroundStyle(.orange)
                    }
                }
            }
            Section {
                let pairs = visiblePairs
                if pairs.isEmpty, !search.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                    ContentUnavailableView.search(text: search)
                } else if pairs.isEmpty {
                    ContentUnavailableView(
                        "Keine Datenwege",
                        systemImage: "arrow.left.arrow.right",
                        description: Text(cloudPath == nil
                            ? "Verbinde einen Ordner mit einem Sicherungsziel."
                            : "Für diesen Cloud-Zugang ist kein Datenweg eingerichtet.")
                    )
                    if cloudPath == nil {
                        Button("Datenweg hinzufügen") { editor = PairEditorRequest(index: nil, pair: nil) }
                            .disabled(configurationDraft.baseRevision == nil)
                    }
                } else {
                    ForEach(pairs, id: \.element.id) { index, pair in
                        Button { editor = PairEditorRequest(index: index, pair: pair) } label: {
                            DataPathConfigurationRow(pair: pair)
                        }
                        .buttonStyle(.plain)
                        .accessibilityLabel("Datenweg \(pair.name) bearbeiten")
                        .accessibilityValue("\(pair.configurationMode), \(pair.configurationStatus). Quelle: \(pair.configurationSource). Ziel: \(pair.configurationTarget)")
                        .accessibilityHint("Öffnet die Einstellungen dieses Datenwegs.")
                        .swipeActions(edge: .leading, allowsFullSwipe: false) {
                            Button { pendingPathAction = .check(pair) } label: {
                                Label("Prüfen", systemImage: "checkmark.shield")
                            }
                            .tint(.blue)
                            .disabled(configurationDraft.isDirty)
                            Button { pendingPathAction = .restore(pair) } label: {
                                Label("Restore-Test", systemImage: "arrow.uturn.backward.circle")
                            }
                            .tint(.orange)
                            .disabled(configurationDraft.isDirty)
                        }
                    }
                    .onDelete { offsets in
                        deletePairs(at: IndexSet(offsets.compactMap { offset in
                            pairs.indices.contains(offset) ? pairs[offset].offset : nil
                        }))
                    }
                }
            } header: {
                if let cloudPath { Text(String(cloudPath.dropLast())) }
            } footer: {
                if configurationDraft.isDirty { Text("Nicht gespeicherte Änderungen") }
            }
            if cloudPath == nil, !configurationDraft.pairs.isEmpty, configurationDraft.definitions.isEmpty, !configurationDraft.isDirty {
                Section {
                    NavigationLink("Ersten Job einrichten") {
                        JobsScreen(showingSettings: $showingSettings)
                    }
                } footer: {
                    Text("Ein Job legt fest, wann und in welcher Reihenfolge deine Datenwege laufen.")
                }
            }
            if cloudPath == nil {
                Section("Werkzeuge") {
                    Button { showingMultiCloudSetup = true } label: {
                        Label("Ordner in mehrere Clouds sichern", systemImage: "square.stack.3d.up.fill")
                    }
                    .disabled(configurationDraft.baseRevision == nil)
                    NavigationLink { CloudsScreen(showingSettings: $showingSettings) } label: {
                        Label("Clouds", systemImage: "cloud")
                    }
                    NavigationLink { QuickSyncView() } label: {
                        Label("Quick Sync", systemImage: "bolt.horizontal.circle")
                    }
                    Button { confirmFullRestoreTest = true } label: {
                        Label("Systemweiten Restore-Test starten", systemImage: "arrow.counterclockwise.circle")
                    }
                    .disabled(configurationDraft.isDirty)
                }
            }
        }
        .listStyle(.insetGrouped)
        .sicherpfadGlassScreen()
        .navigationTitle("Datenwege")
        .sheet(isPresented: $showingMultiCloudSetup) {
            MultiCloudSetupSheet()
        }
        .searchable(text: $search, prompt: "Name, Quelle oder Ziel")
        .toolbar {
            ToolbarItem(placement: .topBarLeading) {
                SettingsButton(showingSettings: $showingSettings)
            }
            ToolbarItemGroup(placement: .topBarTrailing) {
                if cloudPath == nil {
                    Button { editor = PairEditorRequest(index: nil, pair: nil) } label: {
                        Image(systemName: "plus")
                    }
                    .disabled(configurationDraft.baseRevision == nil)
                    .accessibilityLabel("Datenweg hinzufügen")
                }
                Button("Speichern") { Task { await save(password: nil) } }
                    .disabled(!configurationDraft.isDirty || model.isSavingConfig)
            }
        }
        .refreshable { await reload(discardDirty: false) }
        .sheet(item: $editor) { request in
            DataPathEditor(pair: request.pair) { updated in
                configurationDraft.upsertPair(updated, at: request.index)
            }
        }
        .alert("Änderung nicht möglich", isPresented: Binding(
            get: { localError != nil },
            set: { if !$0 { localError = nil } }
        )) {
            Button("OK", role: .cancel) { localError = nil }
        } message: {
            Text(localError ?? "")
        }
        .task { configurationDraft.load(from: model.config) }
        .onChange(of: model.config?.revision) { _, _ in
            configurationDraft.load(from: model.config)
        }
        .confirmationDialog("Aktion starten?", isPresented: Binding(
            get: { pendingPathAction != nil },
            set: { if !$0 { pendingPathAction = nil } }
        ), titleVisibility: .visible) {
            if let action = pendingPathAction {
                Button(action.buttonTitle) {
                    pendingPathAction = nil
                    Task {
                        switch action {
                        case let .check(pair): _ = await model.checkDataPath(name: pair.name)
                        case let .restore(pair): _ = await model.runRestoreTest(pair: pair.name)
                        }
                    }
                }
            }
            Button("Abbrechen", role: .cancel) { pendingPathAction = nil }
        } message: {
            Text("Der Check verändert keine Dateien. Der Restore-Test lädt Stichproben zurück und vergleicht sie mit der Quelle.")
        }
        .confirmationDialog("Alle Datenwege wiederherstellen testen?", isPresented: $confirmFullRestoreTest, titleVisibility: .visible) {
            Button("Restore-Test starten") { Task { _ = await model.runRestoreTest(pair: nil) } }
            Button("Abbrechen", role: .cancel) {}
        } message: {
            Text("Der Drill prüft alle eingerichteten Datenwege mit Stichproben und verändert keine Originaldateien.")
        }
    }

    private func deletePairs(at offsets: IndexSet) {
        for index in offsets.sorted(by: >) {
            guard configurationDraft.pairs.indices.contains(index) else { continue }
            let pair = configurationDraft.pairs[index]
            let referencedBy = configurationDraft.definitions.filter { $0.dataPathIDs.contains(pair.id) }
            if !referencedBy.isEmpty {
                localError = "„\(pair.name)“ ist noch \(referencedBy.count) Job(s) zugewiesen. Entferne zuerst diese Zuweisungen."
                continue
            }
            configurationDraft.removePair(at: index)
        }
    }

    private func reload(discardDirty: Bool) async {
        if configurationDraft.isDirty && !discardDirty {
            localError = "Ungespeicherte Änderungen wurden nicht verworfen. Speichere sie zuerst oder nutze bei einem Konflikt bewusst „Serverstand laden“."
            return
        }
        await model.reloadConfiguration()
        configurationDraft.load(from: model.config, force: true)
    }

    private func save(password: String?) async {
        guard let draftBaseRevision = configurationDraft.baseRevision else {
            localError = "Der Serverstand dieses Entwurfs ist unbekannt. Lade die Konfiguration neu."
            return
        }
        if await model.saveConfiguration(
            pairs: configurationDraft.pairs,
            definitions: configurationDraft.definitions,
            baseRevision: draftBaseRevision,
            currentPassword: password
        ) {
            currentPassword = ""
            configurationDraft.load(from: model.config, force: true)
        }
    }

    private func saveWithPassword() {
        Task { await save(password: currentPassword) }
    }
}

private enum DataPathAction: Identifiable {
    case check(PairConfig)
    case restore(PairConfig)

    var id: String {
        switch self {
        case let .check(pair): "check-\(pair.id)"
        case let .restore(pair): "restore-\(pair.id)"
        }
    }

    var buttonTitle: String {
        switch self {
        case .check: "Datenweg prüfen"
        case .restore: "Restore-Test starten"
        }
    }
}

struct JobsScreen: View {
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var configurationDraft: ConfigurationDraftStore
    @Binding var showingSettings: Bool
    @State private var editor: JobEditorRequest?
    @State private var showingMultiCloudSetup = false
    @State private var plan: PlanPresentation?
    @State private var pendingRun: PendingJobRun?
    @State private var currentPassword = ""
    @State private var localError: String?

    private var scheduledDefinitions: [(offset: Int, element: JobDefinition)] {
        configurationDraft.definitions.enumerated().filter { _, def in
            def.enabled && JobScheduleCodec.selection(for: def.schedule).mode != .manual
        }
    }

    private var manualOrPausedDefinitions: [(offset: Int, element: JobDefinition)] {
        configurationDraft.definitions.enumerated().filter { _, def in
            !def.enabled || JobScheduleCodec.selection(for: def.schedule).mode == .manual
        }
    }

    var body: some View {
        List {
            if let issue = model.configSaveIssue {
                ConfigIssuePanel(
                    issue: issue,
                    password: $currentPassword,
                    reload: { await reload(discardDirty: true) },
                    retryWithPassword: saveWithPassword
                )
            }
            if configurationDraft.definitions.isEmpty {
                Section("Jobliste") {
                    ContentUnavailableView(
                        "Keine Jobs",
                        systemImage: "calendar.badge.plus",
                        description: Text("Jobs legen Zeitplan, Reihenfolge und Ausführung der Datenwege fest.")
                    )
                    Button("Ordner in mehrere Clouds sichern") { showingMultiCloudSetup = true }
                        .disabled(configurationDraft.baseRevision == nil)
                    Button("Job hinzufügen") { editor = JobEditorRequest(index: nil, definition: nil) }
                        .disabled(configurationDraft.pairs.isEmpty)
                }
            } else {
                if !scheduledDefinitions.isEmpty {
                    Section {
                        ForEach(scheduledDefinitions, id: \.element.id) { index, definition in
                            DisclosureGroup {
                                jobDetails(definition, at: index)
                            } label: {
                                JobDefinitionRow(definition: definition, pairs: configurationDraft.pairs)
                            }
                        }
                    } header: {
                        Text("Geplante Automatik (\(scheduledDefinitions.count))")
                    } footer: {
                        if configurationDraft.isDirty {
                            Text("Änderungen speichern, bevor du Plan oder Lauf startest.")
                        }
                    }
                    .sicherpfadGlassListRow()
                }

                if !manualOrPausedDefinitions.isEmpty {
                    Section {
                        ForEach(manualOrPausedDefinitions, id: \.element.id) { index, definition in
                            DisclosureGroup {
                                jobDetails(definition, at: index)
                            } label: {
                                JobDefinitionRow(definition: definition, pairs: configurationDraft.pairs)
                            }
                        }
                    } header: {
                        Text("Manuelle Sicherungen (\(manualOrPausedDefinitions.count))")
                    } footer: {
                        Text("Manuelle Jobs starten nur auf deinen ausdrücklichen Aufruf.")
                    }
                    .sicherpfadGlassListRow()
                }
            }
            Section("Schnell einrichten & Zeitplanung") {
                Button { showingMultiCloudSetup = true } label: {
                    Label("Ordner in mehrere Clouds sichern", systemImage: "square.stack.3d.up.fill")
                }
                .disabled(configurationDraft.baseRevision == nil)
                NavigationLink {
                    JobScheduleOverview(definitions: configurationDraft.definitions, pairs: configurationDraft.pairs)
                } label: {
                    Label("Zeitpläne & Bandbreite ansehen", systemImage: "calendar")
                }
            }
            .sicherpfadGlassListRow()
        }
        .listStyle(.insetGrouped)
        .sicherpfadGlassScreen()
        .navigationTitle("Jobs")
        .sheet(isPresented: $showingMultiCloudSetup) {
            MultiCloudSetupSheet()
        }
        .toolbar {
            ToolbarItem(placement: .topBarLeading) {
                SettingsButton(showingSettings: $showingSettings)
            }
            ToolbarItemGroup(placement: .topBarTrailing) {
                Button { editor = JobEditorRequest(index: nil, definition: nil) } label: {
                    Image(systemName: "plus")
                }
                .disabled(configurationDraft.pairs.isEmpty)
                .accessibilityLabel("Job hinzufügen")
                Button("Speichern") { Task { await save(password: nil) } }
                    .disabled(!configurationDraft.isDirty || model.isSavingConfig)
            }
        }
        .refreshable { await reload(discardDirty: false) }
        .sheet(item: $editor) { request in
            JobDefinitionEditor(
                definition: request.definition,
                paths: configurationDraft.pairs
            ) { updated in
                configurationDraft.upsertDefinition(updated, at: request.index)
            }
        }
        .sheet(item: $plan) { presentation in JobPlanView(plan: presentation.plan) }
        .confirmationDialog(
            pendingRun?.dryRun == true ? "Probelauf starten?" : "Job produktiv starten?",
            isPresented: Binding(
                get: { pendingRun != nil },
                set: { if !$0 { pendingRun = nil } }
            ),
            titleVisibility: .visible
        ) {
            if let pendingRun {
                Button(pendingRun.dryRun ? "Probelauf starten" : "Produktiv starten",
                       role: pendingRun.dryRun ? nil : .destructive) {
                    let request = pendingRun
                    self.pendingRun = nil
                    Task { _ = await model.runJobDefinition(id: request.definition.id, dryRun: request.dryRun) }
                }
            }
            Button("Abbrechen", role: .cancel) { pendingRun = nil }
        } message: {
            Text("Prüfe vor einem produktiven Lauf den Plan und die Löschschutz-Einstellungen.")
        }
        .task { configurationDraft.load(from: model.config) }
        .onChange(of: model.config?.revision) { _, _ in
            configurationDraft.load(from: model.config)
        }
        .alert("Entwurf behalten", isPresented: Binding(
            get: { localError != nil },
            set: { if !$0 { localError = nil } }
        )) {
            Button("OK", role: .cancel) { localError = nil }
        } message: {
            Text(localError ?? "")
        }
    }

    private func jobDetails(_ definition: JobDefinition, at index: Int) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            if !definition.dataPathIDs.isEmpty {
                Text("Datenwege in Reihenfolge").font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
                ForEach(Array(definition.dataPathIDs.enumerated()), id: \.offset) { offset, id in
                    let name = configurationDraft.pairs.first { $0.id == id }?.name
                    Text("\(offset + 1). \(name ?? "Unbekannter Datenweg")")
                        .font(.subheadline)
                }
            }
            Button { editor = JobEditorRequest(index: index, definition: definition) } label: {
                Label("Bearbeiten", systemImage: "pencil")
                    .frame(minHeight: 44, alignment: .leading)
            }
            .accessibilityLabel("Job \(definition.name) bearbeiten")
            Button {
                Task {
                    if let result = await model.jobDefinitionPlan(id: definition.id) {
                        plan = PlanPresentation(plan: result)
                    }
                }
            } label: {
                Label("Plan prüfen", systemImage: "list.bullet.clipboard")
                    .frame(minHeight: 44, alignment: .leading)
            }
                .disabled(configurationDraft.isDirty)
            Button { pendingRun = PendingJobRun(definition: definition, dryRun: true) } label: {
                Label("Probelauf", systemImage: "play.circle")
                    .frame(minHeight: 44, alignment: .leading)
            }
            .disabled(configurationDraft.isDirty)
            Button(role: .destructive) { pendingRun = PendingJobRun(definition: definition, dryRun: false) } label: {
                Label("Produktiv starten", systemImage: "play.fill")
                    .frame(minHeight: 44, alignment: .leading)
            }
            .disabled(configurationDraft.isDirty || !definition.enabled)
            Button(role: .destructive) { configurationDraft.removeDefinition(at: index) } label: {
                Label("Job entfernen", systemImage: "trash")
                    .frame(minHeight: 44, alignment: .leading)
            }
        }
        .buttonStyle(.borderless)
    }

    private func reload(discardDirty: Bool) async {
        if configurationDraft.isDirty && !discardDirty {
            localError = "Ungespeicherte Job-Änderungen wurden nicht verworfen. Speichere sie zuerst oder nutze bei einem Konflikt bewusst „Serverstand laden“."
            return
        }
        await model.reloadConfiguration()
        configurationDraft.load(from: model.config, force: true)
    }

    private func save(password: String?) async {
        guard let draftBaseRevision = configurationDraft.baseRevision else {
            localError = "Der Serverstand dieses Entwurfs ist unbekannt. Lade die Konfiguration neu."
            return
        }
        if await model.saveConfiguration(
            pairs: configurationDraft.pairs,
            definitions: configurationDraft.definitions,
            baseRevision: draftBaseRevision,
            currentPassword: password
        ) {
            currentPassword = ""
            configurationDraft.load(from: model.config, force: true)
        }
    }

    private func saveWithPassword() {
        Task { await save(password: currentPassword) }
    }
}

private struct ConfigIssuePanel: View {
    let issue: ConfigSaveIssue
    @Binding var password: String
    let reload: () async -> Void
    let retryWithPassword: () -> Void

    var body: some View {
        Section("Speichern nicht abgeschlossen") {
            switch issue {
            case let .conflict(message):
                Label(message, systemImage: "arrow.triangle.2.circlepath")
                    .foregroundStyle(.orange)
                Button("Serverstand laden") { Task { await reload() } }
            case let .passwordRequired(message):
                Label(message, systemImage: "lock.shield")
                    .foregroundStyle(.orange)
                SecureField("Aktuelles Passwort", text: $password)
                    .textContentType(.password)
                Button("Mit Passwort erneut speichern", action: retryWithPassword)
                    .disabled(password.isEmpty)
            case let .validation(errors):
                ForEach(errors, id: \.self) { error in
                    Label(error, systemImage: "exclamationmark.triangle")
                        .foregroundStyle(.red)
                }
            }
        }
    }
}

private struct DataPathConfigurationRow: View {
    let pair: PairConfig

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(pair.name).font(.headline)
            Text("\(pair.configurationMode) · \(pair.configurationStatus)")
                .font(.subheadline)
                .foregroundStyle(.secondary)
            configurationEndpoint(pair.direction == "bisync" ? "Seite A" : "Quelle", path: pair.configurationSource)
            configurationEndpoint(pair.direction == "bisync" ? "Seite B" : "Ziel", path: pair.configurationTarget)
        }
        .accessibilityElement(children: .combine)
        .padding(.vertical, 4)
    }

    private func configurationEndpoint(_ title: String, path: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title).font(.caption).foregroundStyle(.secondary)
            Text(path.isEmpty ? "Fehlt" : path).font(.subheadline.monospaced())
        }
    }
}

private struct JobDefinitionRow: View {
    let definition: JobDefinition
    var pairs: [PairConfig] = []

    private var linkedPairs: [PairConfig] {
        definition.dataPathIDs.compactMap { id in pairs.first { $0.id == id } }
    }

    private var isManual: Bool {
        JobScheduleCodec.selection(for: definition.schedule).mode == .manual
    }

    private var statusColor: Color {
        if !definition.enabled { return .secondary }
        return isManual ? .blue : .green
    }

    private var statusLabel: String {
        if !definition.enabled { return "Deaktiviert" }
        return isManual ? "Manuell" : "Aktiv"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack(spacing: 8) {
                Text(definition.name).font(.headline)
                Spacer(minLength: 4)
                Text(statusLabel)
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(statusColor)
                    .padding(.horizontal, 8)
                    .padding(.vertical, 3)
                    .sicherpfadGlassBadge(color: statusColor)
            }
            HStack(spacing: 6) {
                Label(JobScheduleCodec.summary(definition.schedule), systemImage: isManual ? "hand.tap" : "clock")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                if linkedPairs.count >= 2 {
                    Text("· \(definition.executionMode == "parallel" ? "Parallel" : "Seriell")")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            if !linkedPairs.isEmpty {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 6) {
                        ForEach(linkedPairs) { pair in
                            CloudTargetChip(remotePath: pair.direction == "pull" ? pair.local : pair.remote)
                        }
                    }
                }
            } else {
                Text(configurationPathCount(definition.dataPathIDs.count))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .accessibilityElement(children: .combine)
        .padding(.vertical, 4)
    }
}

private struct JobScheduleOverview: View {
    let definitions: [JobDefinition]
    var pairs: [PairConfig] = []

    var body: some View {
        List {
            Section("Automatisch") {
                let scheduled = definitions.filter(isScheduled)
                if scheduled.isEmpty {
                    Text("Keine aktivierten Jobs mit Zeitplan.").foregroundStyle(.secondary)
                } else {
                    ForEach(scheduled) { JobDefinitionRow(definition: $0, pairs: pairs) }
                }
            }
            .sicherpfadGlassListRow()
            let remaining = definitions.filter { !isScheduled($0) }
            if !remaining.isEmpty {
                Section("Manuell oder deaktiviert") {
                    ForEach(remaining) { JobDefinitionRow(definition: $0, pairs: pairs) }
                }
                .sicherpfadGlassListRow()
            }
            Section {
                NavigationLink { BandwidthHeatmapView() } label: {
                    Label("Bandbreite & Zeitfenster", systemImage: "chart.bar.xaxis")
                }
            }
            .sicherpfadGlassListRow()
        }
        .listStyle(.insetGrouped)
        .sicherpfadGlassScreen()
        .navigationTitle("Zeitplanung")
    }

    private func isScheduled(_ definition: JobDefinition) -> Bool {
        definition.enabled && JobScheduleCodec.selection(for: definition.schedule).mode != .manual
    }
}

extension PairConfig {
    fileprivate var configurationSource: String { direction == "pull" ? remote : local }
    fileprivate var configurationTarget: String { direction == "pull" ? local : remote }
    fileprivate var configurationStatus: String { enabled ? "Aktiv" : "Deaktiviert" }

    fileprivate var configurationMode: String {
        if direction == "bisync" { return "Bidirektional" }
        return mode == "sync" ? "Spiegeln" : "Kopieren"
    }

    func referencesCloud(_ root: String) -> Bool {
        guard root.hasSuffix(":") else { return false }
        return [local, remote].contains {
            $0.trimmingCharacters(in: .whitespacesAndNewlines).hasPrefix(root)
        }
    }
}

func configurationPathCount(_ count: Int) -> String {
    count == 1 ? "1 Datenweg" : "\(count) Datenwege"
}

private struct PairEditorRequest: Identifiable {
    let id = UUID()
    let index: Int?
    let pair: PairConfig?
}

private struct JobEditorRequest: Identifiable {
    let id = UUID()
    let index: Int?
    let definition: JobDefinition?
}

private struct PendingJobRun: Identifiable {
    let id = UUID()
    let definition: JobDefinition
    let dryRun: Bool
}

private struct PlanPresentation: Identifiable {
    let id = UUID()
    let plan: JobPlan
}

private struct DataPathEditor: View {
    @Environment(\.dismiss) private var dismiss
    let original: PairConfig?
    let save: (PairConfig) -> Void
    @State private var name: String
    @State private var local: String
    @State private var remote: String
    @State private var direction: String
    @State private var mode: String
    @State private var enabled: Bool
    @State private var showAdvanced = false
    @State private var allowDelete: Bool
    @State private var maxDeleteText: String
    @State private var backupDir: String
    @State private var minLocalFiles: Int
    @State private var minRemoteFiles: Int
    @State private var requireMountpoint: Bool
    @State private var mountpoint: String
    @State private var sentinelFile: String
    @State private var recoverySnapshots: Bool
    @State private var maxSuccessAgeHoursText: String
    @State private var excludeText: String
    @State private var failureDomain: String
    @State private var airgapDevice: String
    @State private var zfsSnapshot: Bool
    @State private var browseTarget: BrowseTarget?

    init(pair: PairConfig?, save: @escaping (PairConfig) -> Void) {
        original = pair
        self.save = save
        _name = State(initialValue: pair?.name ?? "")
        _local = State(initialValue: pair?.local ?? "")
        _remote = State(initialValue: pair?.remote ?? "")
        _direction = State(initialValue: pair?.direction ?? "push")
        _mode = State(initialValue: pair?.mode ?? "copy")
        _enabled = State(initialValue: pair?.enabled ?? true)
        _allowDelete = State(initialValue: pair?.allowDelete ?? false)
        _maxDeleteText = State(initialValue: pair?.maxDelete.map(String.init) ?? "")
        _backupDir = State(initialValue: pair?.backupDir ?? "")
        _minLocalFiles = State(initialValue: pair?.minLocalFiles ?? 1)
        _minRemoteFiles = State(initialValue: pair?.minRemoteFiles ?? 0)
        _requireMountpoint = State(initialValue: pair?.requireMountpoint ?? false)
        _mountpoint = State(initialValue: pair?.mountpoint ?? "")
        _sentinelFile = State(initialValue: pair?.sentinelFile ?? "")
        _recoverySnapshots = State(initialValue: pair?.recoverySnapshots ?? false)
        let initialAge: String
        if let hours = pair?.maxSuccessAgeHours, hours > 0 {
            initialAge = hours.truncatingRemainder(dividingBy: 1) == 0 ? String(Int(hours)) : String(hours)
        } else {
            initialAge = ""
        }
        _maxSuccessAgeHoursText = State(initialValue: initialAge)
        _excludeText = State(initialValue: pair?.exclude ?? "")
        _failureDomain = State(initialValue: pair?.failureDomain ?? "")
        _airgapDevice = State(initialValue: pair?.airgapDevice ?? "")
        _zfsSnapshot = State(initialValue: pair?.direction == "push" ? (pair?.zfsSnapshot ?? false) : false)
    }

    var body: some View {
        NavigationStack {
            Form {
                Section("Datenweg") {
                    TextField("Name", text: $name)
                    HStack {
                        TextField("Lokaler Ordner", text: $local)
                            .textInputAutocapitalization(.never).autocorrectionDisabled()
                        Button { browseTarget = .local } label: { Image(systemName: "folder") }
                            .accessibilityLabel("Lokalen Ordner auswählen")
                    }
                    HStack {
                        TextField("Remote oder Zielpfad", text: $remote)
                            .textInputAutocapitalization(.never).autocorrectionDisabled()
                        Button { browseTarget = .remote } label: { Image(systemName: "folder") }
                            .accessibilityLabel("Zielordner auswählen")
                    }
                    Toggle("Aktiv", isOn: $enabled)
                }
                Section("Übertragung") {
                    Picker("Richtung", selection: $direction) {
                        Text("Push").tag("push")
                        Text("Pull").tag("pull")
                        Text("Bi-Sync").tag("bisync")
                    }
                    .pickerStyle(.segmented)
                    .onChange(of: direction) { _, newDirection in
                        if newDirection != "push" {
                            zfsSnapshot = false
                        }
                    }
                    if direction != "bisync" {
                        Picker("Modus", selection: $mode) {
                            Text("Kopieren").tag("copy")
                            Text("Spiegeln").tag("sync")
                        }
                        .pickerStyle(.segmented)
                    }
                }
                DisclosureGroup("Schutz und erweiterte Optionen", isExpanded: $showAdvanced) {
                    Stepper("Mindestens \(minLocalFiles) lokale Dateien", value: $minLocalFiles, in: 0...1_000_000)
                    Stepper("Mindestens \(minRemoteFiles) Dateien am Ziel", value: $minRemoteFiles, in: 0...1_000_000)
                    Toggle("Mountpoint verlangen", isOn: $requireMountpoint)
                    if requireMountpoint { TextField("Mountpoint", text: $mountpoint) }
                    TextField("Sentinel-Datei (relativ)", text: $sentinelFile)
                    VStack(alignment: .leading, spacing: 4) {
                        TextField("Versionsablage", text: $backupDir)
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                        Text("Tipp: Platzhalter {date} (z. B. pcloud:/.rclone-versions/Fotos/{date}) aktiviert datierte GFS-Generationen.")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                    Toggle("Lokale Recovery-Snapshots", isOn: $recoverySnapshots)
                    if direction == "push" {
                        Toggle("Atomarer ZFS-Quell-Snapshot", isOn: $zfsSnapshot)
                    }
                    TextField("Ausfalldomäne (3-2-1, z. B. cloud, nas, usb)", text: $failureDomain)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    TextField("Airgap-Gerät (z. B. /dev/disk/by-uuid/…)", text: $airgapDevice)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    TextField("Überfälligkeits-Frist in Std. (leer = Standard)", text: $maxSuccessAgeHoursText)
                        .keyboardType(.decimalPad)
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Ausschlussmuster (eine Zeile je Muster)")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        TextEditor(text: $excludeText)
                            .font(.system(.footnote, design: .monospaced))
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                            .frame(minHeight: 68)
                    }
                    if destructive {
                        Toggle("Löschungen ausdrücklich freigeben", isOn: $allowDelete)
                            .tint(.orange)
                        TextField("Maximale Löschungen", text: $maxDeleteText)
                            .keyboardType(.numberPad)
                    }
                }
                if shouldShowValidationBanner, let validationMessage {
                    Section { Label(validationMessage, systemImage: "exclamationmark.triangle") }
                        .foregroundStyle(.red)
                } else if destructive {
                    Section {
                        Label(
                            "Spiegeln und Bi-Sync können Dateien am Ziel löschen. Plan und Probelauf vor dem produktiven Start prüfen.",
                            systemImage: "exclamationmark.shield"
                        )
                        .foregroundStyle(.orange)
                    }
                }
            }
            .navigationTitle(original == nil ? "Datenweg anlegen" : "Datenweg bearbeiten")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Abbrechen") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Übernehmen", action: commit).disabled(validationMessage != nil)
                }
            }
        }
        .sheet(item: $browseTarget) { target in
            PathBrowserSheet(
                kind: target == .local ? .local : .remote,
                initialPath: target == .local ? local : remote,
                allowsKindSwitch: target == .remote
            ) { selected in
                if target == .local {
                    local = selected
                    suggestNameIfEmpty(from: selected)
                } else {
                    remote = selected
                    suggestNameIfEmpty(from: selected)
                }
            }
        }
    }

    private var destructive: Bool { direction == "bisync" || mode == "sync" }

    private var shouldShowValidationBanner: Bool {
        original != nil
            || !name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            || !local.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            || !remote.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    private var parsedMaxSuccessAgeHours: Double? {
        let trimmed = maxSuccessAgeHoursText.trimmingCharacters(in: .whitespacesAndNewlines)
            .replacingOccurrences(of: ",", with: ".")
        if trimmed.isEmpty { return 0 }
        guard let value = Double(trimmed), value >= 0, value <= 8760 else { return nil }
        return value
    }

    private var validationMessage: String? {
        let cleanName = name.trimmingCharacters(in: .whitespacesAndNewlines)
        if cleanName.isEmpty {
            return "Bitte einen Namen für den Datenweg eingeben."
        }
        if cleanName.count > 80
            || cleanName.contains("/")
            || cleanName.contains("\\")
            || cleanName.contains(",")
            || cleanName.lowercased().hasPrefix("pbs:") {
            return "Bitte einen gültigen Namen ohne Schrägstriche oder Kommas eingeben."
        }
        if !local.hasPrefix("/") { return "Der lokale Pfad muss absolut sein und mit / beginnen." }
        if !(remote.hasPrefix("/") || remote.contains(":")) {
            return "Das Ziel muss ein absoluter Pfad oder ein rclone-Remote wie cloud:Ordner sein."
        }
        if parsedMaxSuccessAgeHours == nil {
            return "Die Überfälligkeits-Frist muss zwischen 0 und 8760 Stunden liegen."
        }
        if destructive && allowDelete && Int(maxDeleteText) == nil {
            return "Für freigegebene Löschungen ist eine begrenzte maximale Anzahl nötig."
        }
        return nil
    }

    private func suggestNameIfEmpty(from rawPath: String) {
        guard name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        let withoutRemote: String
        if let colon = rawPath.firstIndex(of: ":") {
            let afterColon = String(rawPath[rawPath.index(after: colon)...])
            withoutRemote = afterColon.isEmpty ? String(rawPath[..<colon]) : afterColon
        } else {
            withoutRemote = rawPath
        }
        let candidate = withoutRemote
            .split(separator: "/")
            .map(String.init)
            .last?
            .replacingOccurrences(of: "\\", with: "-")
            .replacingOccurrences(of: ",", with: "-")
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        if !candidate.isEmpty, candidate.count <= 80, !candidate.lowercased().hasPrefix("pbs:") {
            name = candidate
        }
    }

    private func commit() {
        let finalMode = direction == "bisync" ? "bisync" : mode
        let ageHours = parsedMaxSuccessAgeHours ?? 0
        let normalizedExclude = excludeText
            .split(whereSeparator: \.isNewline)
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
            .joined(separator: "\n")
        let updated: PairConfig
        if let original {
            updated = original.replacing(
                name: name.trimmingCharacters(in: .whitespacesAndNewlines),
                local: local.trimmingCharacters(in: .whitespacesAndNewlines),
                remote: remote.trimmingCharacters(in: .whitespacesAndNewlines),
                direction: direction,
                mode: finalMode,
                enabled: enabled,
                allowDelete: destructive && allowDelete,
                maxDelete: destructive ? Int(maxDeleteText) : nil,
                backupDir: backupDir,
                minLocalFiles: minLocalFiles,
                minRemoteFiles: minRemoteFiles,
                requireMountpoint: requireMountpoint,
                mountpoint: mountpoint,
                sentinelFile: sentinelFile,
                recoverySnapshots: recoverySnapshots,
                maxSuccessAgeHours: ageHours,
                exclude: normalizedExclude,
                failureDomain: failureDomain.trimmingCharacters(in: .whitespacesAndNewlines),
                airgapDevice: airgapDevice.trimmingCharacters(in: .whitespacesAndNewlines),
                zfsSnapshot: direction == "push" && zfsSnapshot
            )
        } else {
            updated = PairConfig(
                stableID: UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased(),
                name: name.trimmingCharacters(in: .whitespacesAndNewlines),
                local: local.trimmingCharacters(in: .whitespacesAndNewlines),
                remote: remote.trimmingCharacters(in: .whitespacesAndNewlines),
                direction: direction,
                mode: finalMode,
                enabled: enabled,
                allowDelete: destructive && allowDelete,
                maxDelete: destructive ? Int(maxDeleteText) : nil,
                backupDir: backupDir,
                minLocalFiles: minLocalFiles,
                minRemoteFiles: minRemoteFiles,
                requireMountpoint: requireMountpoint,
                mountpoint: mountpoint,
                sentinelFile: sentinelFile,
                recoverySnapshots: recoverySnapshots,
                maxSuccessAgeHours: ageHours,
                exclude: normalizedExclude,
                failureDomain: failureDomain.trimmingCharacters(in: .whitespacesAndNewlines),
                airgapDevice: airgapDevice.trimmingCharacters(in: .whitespacesAndNewlines),
                zfsSnapshot: direction == "push" && zfsSnapshot
            )
        }
        save(updated)
        dismiss()
    }
}

private enum BrowseTarget: String, Identifiable {
    case local, remote
    var id: String { rawValue }
}

private struct JobDefinitionEditor: View {
    @Environment(\.dismiss) private var dismiss
    let original: JobDefinition?
    let paths: [PairConfig]
    let save: (JobDefinition) -> Void
    @State private var name: String
    @State private var enabled: Bool
    @State private var schedule: String
    @State private var scheduleMode: JobScheduleMode
    @State private var scheduleTime: Date
    @State private var weeklyDay: Int
    @State private var executionMode: String
    @State private var maxParallel: Int
    @State private var retryMinutes: Int
    @State private var selectedPathIDs: [String]
    @State private var showAdvanced = false

    init(definition: JobDefinition?, paths: [PairConfig], save: @escaping (JobDefinition) -> Void) {
        original = definition
        self.paths = paths
        self.save = save
        _name = State(initialValue: definition?.name ?? "")
        _enabled = State(initialValue: definition?.enabled ?? true)
        let initialSchedule = definition?.schedule ?? "manual"
        let scheduleSelection = JobScheduleCodec.selection(for: initialSchedule)
        _schedule = State(initialValue: initialSchedule)
        _scheduleMode = State(initialValue: scheduleSelection.mode)
        _scheduleTime = State(initialValue: scheduleSelection.time)
        _weeklyDay = State(initialValue: scheduleSelection.weeklyDay)
        _executionMode = State(initialValue: definition?.executionMode ?? "sequential")
        _maxParallel = State(initialValue: definition?.maxParallel ?? 2)
        _retryMinutes = State(initialValue: definition?.retryMinutes ?? 60)
        _selectedPathIDs = State(initialValue: definition?.dataPathIDs ?? [])
    }

    var body: some View {
        NavigationStack {
            Form {
                Section("Job") {
                    TextField("Name", text: $name)
                    Toggle("Aktiv", isOn: $enabled)
                    Picker("Rhythmus", selection: $scheduleMode) {
                        ForEach(JobScheduleMode.allCases) { mode in
                            Text(mode.label).tag(mode)
                        }
                    }
                    if scheduleMode != .manual && scheduleMode != .custom {
                        DatePicker("Uhrzeit", selection: $scheduleTime, displayedComponents: .hourAndMinute)
                    }
                    if scheduleMode == .weekly {
                        Picker("Wochentag", selection: $weeklyDay) {
                            ForEach(JobScheduleCodec.weekdays) { weekday in
                                Text(weekday.label).tag(weekday.value)
                            }
                        }
                    }
                    if scheduleMode == .custom {
                        TextField("Cron-Zeitplan", text: $schedule)
                            .textInputAutocapitalization(.never).autocorrectionDisabled()
                        Text("Minute Stunde Tag Monat Wochentag, z. B. 0 3 * * *")
                            .font(.caption).foregroundStyle(.secondary)
                    } else {
                        Text(JobScheduleCodec.summary(schedule))
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }
                Section("Datenwege in Reihenfolge") {
                    if selectedPathIDs.isEmpty {
                        Text("Mindestens einen Datenweg auswählen").foregroundStyle(.secondary)
                    }
                    ForEach(selectedPathIDs, id: \.self) { id in
                        HStack {
                            Image(systemName: "line.3.horizontal")
                                .foregroundStyle(.secondary)
                            Text(pathName(id))
                            Spacer()
                            Button(role: .destructive) {
                                selectedPathIDs.removeAll { $0 == id }
                            } label: { Image(systemName: "minus.circle") }
                                .buttonStyle(.plain)
                                .accessibilityLabel("\(pathName(id)) entfernen")
                        }
                    }
                    .onMove { selectedPathIDs.move(fromOffsets: $0, toOffset: $1) }
                    ForEach(paths.filter { !selectedPathIDs.contains($0.id) }) { path in
                        Button { selectedPathIDs.append(path.id) } label: {
                            Label(path.name, systemImage: "plus.circle")
                        }
                    }
                }
                Section("Ausführung") {
                    Picker("Modus", selection: $executionMode) {
                        Text("Nacheinander").tag("sequential")
                        Text("Parallel").tag("parallel")
                    }
                    .pickerStyle(.segmented)
                    if executionMode == "parallel" {
                        Stepper("Maximal \(maxParallel) parallel", value: $maxParallel, in: 1...16)
                    }
                }
                DisclosureGroup("Erweitert", isExpanded: $showAdvanced) {
                    Stepper("Nach Fehler nach \(retryMinutes) Minuten", value: $retryMinutes, in: 1...10_080, step: 5)
                }
                if let validationMessage {
                    Section { Label(validationMessage, systemImage: "exclamationmark.triangle") }
                        .foregroundStyle(.red)
                }
            }
            .environment(\.editMode, .constant(.active))
            .navigationTitle(original == nil ? "Job anlegen" : "Job bearbeiten")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Abbrechen") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Übernehmen", action: commit).disabled(validationMessage != nil)
                }
            }
        }
        .onChange(of: scheduleMode) { _, _ in rebuildSchedule() }
        .onChange(of: scheduleTime) { _, _ in rebuildSchedule() }
        .onChange(of: weeklyDay) { _, _ in rebuildSchedule() }
    }

    private var validationMessage: String? {
        let cleanName = name.trimmingCharacters(in: .whitespacesAndNewlines)
        if cleanName.isEmpty || cleanName.count > 80 || cleanName.contains("/") || cleanName.contains("\\") {
            return "Bitte einen gültigen Jobnamen eingeben."
        }
        if selectedPathIDs.isEmpty { return "Wähle mindestens einen Datenweg aus." }
        let cleanSchedule = schedule.trimmingCharacters(in: .whitespacesAndNewlines)
        let manual = ["", "manual", "off", "disabled", "none"].contains(cleanSchedule.lowercased())
        if !manual && cleanSchedule.split(separator: " ").count != 5 {
            return "Ein automatischer Zeitplan braucht fünf Cron-Felder."
        }
        return nil
    }

    private func pathName(_ id: String) -> String {
        paths.first { $0.id == id }?.name ?? "Unbekannter Datenweg"
    }

    private func rebuildSchedule() {
        guard scheduleMode != .custom else { return }
        schedule = JobScheduleCodec.cron(
            mode: scheduleMode,
            time: scheduleTime,
            weeklyDay: weeklyDay
        )
    }

    private func commit() {
        save(JobDefinition(
            id: original?.id ?? UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased(),
            name: name.trimmingCharacters(in: .whitespacesAndNewlines),
            enabled: enabled,
            dataPathIDs: selectedPathIDs,
            schedule: schedule.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? "manual" : schedule.trimmingCharacters(in: .whitespacesAndNewlines),
            executionMode: executionMode,
            maxParallel: executionMode == "parallel" ? maxParallel : 1,
            retryMinutes: retryMinutes
        ))
        dismiss()
    }
}

private enum JobScheduleMode: String, CaseIterable, Identifiable {
    case manual, daily, weekdays, weekly, custom

    var id: String { rawValue }
    var label: String {
        switch self {
        case .manual: "Manuell"
        case .daily: "Täglich"
        case .weekdays: "Werktags"
        case .weekly: "Wöchentlich"
        case .custom: "Eigener Zeitplan"
        }
    }
}

private struct JobWeekday: Identifiable {
    let value: Int
    let label: String
    var id: Int { value }
}

private enum JobScheduleCodec {
    static let weekdays = [
        JobWeekday(value: 1, label: "Montag"), JobWeekday(value: 2, label: "Dienstag"),
        JobWeekday(value: 3, label: "Mittwoch"), JobWeekday(value: 4, label: "Donnerstag"),
        JobWeekday(value: 5, label: "Freitag"), JobWeekday(value: 6, label: "Samstag"),
        JobWeekday(value: 0, label: "Sonntag")
    ]

    static func selection(for schedule: String) -> (mode: JobScheduleMode, time: Date, weeklyDay: Int) {
        let clean = schedule.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        let fallback = Calendar.current.date(from: DateComponents(hour: 3, minute: 0)) ?? Date()
        guard !["", "manual", "off", "disabled", "none"].contains(clean) else {
            return (.manual, fallback, 1)
        }
        let fields = clean.split(whereSeparator: { $0.isWhitespace }).map(String.init)
        guard fields.count == 5,
              let minute = Int(fields[0]), (0...59).contains(minute),
              let hour = Int(fields[1]), (0...23).contains(hour),
              fields[2] == "*", fields[3] == "*"
        else { return (.custom, fallback, 1) }
        let time = Calendar.current.date(from: DateComponents(hour: hour, minute: minute)) ?? fallback
        if fields[4] == "*" { return (.daily, time, 1) }
        if fields[4] == "1-5" { return (.weekdays, time, 1) }
        if let day = Int(fields[4]), (0...6).contains(day) { return (.weekly, time, day) }
        return (.custom, time, 1)
    }

    static func cron(mode: JobScheduleMode, time: Date, weeklyDay: Int) -> String {
        guard mode != .manual else { return "manual" }
        let parts = Calendar.current.dateComponents([.hour, .minute], from: time)
        let minute = parts.minute ?? 0
        let hour = parts.hour ?? 3
        switch mode {
        case .manual: return "manual"
        case .daily: return "\(minute) \(hour) * * *"
        case .weekdays: return "\(minute) \(hour) * * 1-5"
        case .weekly: return "\(minute) \(hour) * * \(min(max(weeklyDay, 0), 6))"
        case .custom: return ""
        }
    }

    static func summary(_ schedule: String) -> String {
        let selection = selection(for: schedule)
        guard selection.mode != .manual else { return "Nur bei manuellem Start" }
        guard selection.mode != .custom else { return schedule }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "de_DE")
        formatter.dateFormat = "HH:mm"
        let time = formatter.string(from: selection.time)
        switch selection.mode {
        case .daily: return "Täglich um \(time) Uhr"
        case .weekdays: return "Montag bis Freitag um \(time) Uhr"
        case .weekly:
            let day = weekdays.first { $0.value == selection.weeklyDay }?.label ?? "Wöchentlich"
            return "\(day) um \(time) Uhr"
        case .manual: return "Nur bei manuellem Start"
        case .custom: return schedule
        }
    }
}

private struct JobPlanView: View {
    @Environment(\.dismiss) private var dismiss
    let plan: JobPlan

    var body: some View {
        NavigationStack {
            List {
                Section("Übersicht") {
                    LabeledContent("Datenwege", value: "\(plan.totalPairs)")
                    LabeledContent("Ergebnis") { StatusBadge(status: plan.ok ? "ok" : "error") }
                    LabeledContent("Modus", value: plan.dryRun ? "Probelauf" : "Produktiv")
                }
                if !plan.warnings.isEmpty {
                    Section("Hinweise") {
                        ForEach(plan.warnings, id: \.self) { warning in
                            Label(warning, systemImage: "exclamationmark.triangle")
                                .foregroundStyle(.orange)
                        }
                    }
                }
                Section("Befehle") {
                    ForEach(plan.pairs) { pair in
                        DisclosureGroup(pair.name) {
                            if let error = pair.error { Text(error).foregroundStyle(.red) }
                            if let command = pair.command {
                                Text(command).font(.system(.caption, design: .monospaced)).textSelection(.enabled)
                            }
                        }
                    }
                }
            }
            .navigationTitle("Jobplan")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Fertig") { dismiss() } } }
        }
    }
}
