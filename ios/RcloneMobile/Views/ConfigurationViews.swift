import SwiftUI

struct DataPathsScreen: View {
    @EnvironmentObject private var model: AppModel
    @Binding var showingSettings: Bool
    @State private var pairs: [PairConfig] = []
    @State private var isDirty = false
    @State private var editor: PairEditorRequest?
    @State private var localError: String?
    @State private var currentPassword = ""
    @State private var pendingPathAction: DataPathAction?
    @State private var confirmFullRestoreTest = false

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
                if pairs.isEmpty {
                    ContentUnavailableView(
                        "Keine Datenwege",
                        systemImage: "arrow.left.arrow.right",
                        description: Text("Lege zuerst die Verbindung zwischen lokalem Ordner und Ziel an.")
                    )
                } else {
                    ForEach(Array(pairs.enumerated()), id: \.element.id) { index, pair in
                        Button { editor = PairEditorRequest(index: index, pair: pair) } label: {
                            DataPathConfigurationRow(pair: pair)
                        }
                        .buttonStyle(.plain)
                        .swipeActions(edge: .leading, allowsFullSwipe: false) {
                            Button { pendingPathAction = .check(pair) } label: {
                                Label("Prüfen", systemImage: "checkmark.shield")
                            }
                            .tint(.blue)
                            .disabled(isDirty)
                            Button { pendingPathAction = .restore(pair) } label: {
                                Label("Restore-Test", systemImage: "arrow.uturn.backward.circle")
                            }
                            .tint(.orange)
                            .disabled(isDirty)
                        }
                    }
                    .onDelete(perform: deletePairs)
                }
            } header: {
                Text("Datenwege")
            } footer: {
                if isDirty { Text("Nicht gespeicherte Änderungen") }
            }
            Section("Werkzeuge") {
                NavigationLink { QuickSyncView() } label: {
                    Label("Quick Sync", systemImage: "bolt.horizontal.circle")
                }
                Button { confirmFullRestoreTest = true } label: {
                    Label("Systemweiten Restore-Test starten", systemImage: "arrow.counterclockwise.circle")
                }
                .disabled(isDirty)
            }
        }
        .listStyle(.insetGrouped)
        .navigationTitle("Datenwege")
        .toolbar {
            ToolbarItem(placement: .topBarLeading) {
                SettingsButton(showingSettings: $showingSettings)
            }
            ToolbarItemGroup(placement: .topBarTrailing) {
                Button { editor = PairEditorRequest(index: nil, pair: nil) } label: {
                    Image(systemName: "plus")
                }
                .accessibilityLabel("Datenweg hinzufügen")
                Button("Speichern") { Task { await save(password: nil) } }
                    .disabled(!isDirty || model.isSavingConfig)
            }
        }
        .refreshable { await reload(discardDirty: false) }
        .sheet(item: $editor) { request in
            DataPathEditor(pair: request.pair) { updated in
                if let index = request.index, pairs.indices.contains(index) {
                    pairs[index] = updated
                } else {
                    pairs.append(updated)
                }
                isDirty = true
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
        .task { loadFromModel(force: false) }
        .onChange(of: model.config?.revision) { _, _ in loadFromModel(force: false) }
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
            guard pairs.indices.contains(index) else { continue }
            let pair = pairs[index]
            let referencedBy = model.jobDefinitions.filter { $0.dataPathIDs.contains(pair.id) }
            if !referencedBy.isEmpty {
                localError = "„\(pair.name)“ ist noch \(referencedBy.count) Job(s) zugewiesen. Entferne zuerst diese Zuweisungen."
                continue
            }
            pairs.remove(at: index)
            isDirty = true
        }
    }

    private func loadFromModel(force: Bool) {
        guard force || !isDirty else { return }
        pairs = model.config?.backup.pairs ?? []
        isDirty = false
    }

    private func reload(discardDirty: Bool) async {
        if isDirty && !discardDirty {
            localError = "Ungespeicherte Änderungen wurden nicht verworfen. Speichere sie zuerst oder nutze bei einem Konflikt bewusst „Serverstand laden“."
            return
        }
        await model.reloadConfiguration()
        loadFromModel(force: true)
    }

    private func save(password: String?) async {
        if await model.saveConfiguration(
            pairs: pairs,
            definitions: model.jobDefinitions,
            currentPassword: password
        ) {
            isDirty = false
            currentPassword = ""
            loadFromModel(force: true)
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
    @Binding var showingSettings: Bool
    @State private var definitions: [JobDefinition] = []
    @State private var isDirty = false
    @State private var editor: JobEditorRequest?
    @State private var plan: PlanPresentation?
    @State private var pendingRun: PendingJobRun?
    @State private var currentPassword = ""
    @State private var localError: String?

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
            Section {
                if definitions.isEmpty {
                    ContentUnavailableView(
                        "Keine Jobs",
                        systemImage: "calendar.badge.plus",
                        description: Text("Jobs legen Zeitplan, Reihenfolge und Ausführung der Datenwege fest.")
                    )
                } else {
                    ForEach(Array(definitions.enumerated()), id: \.element.id) { index, definition in
                        JobDefinitionRow(
                            definition: definition,
                            pathNames: pathNames(for: definition)
                        )
                        .contentShape(Rectangle())
                        .onTapGesture { editor = JobEditorRequest(index: index, definition: definition) }
                        .swipeActions(edge: .leading, allowsFullSwipe: false) {
                            Button {
                                Task {
                                    if let result = await model.jobDefinitionPlan(id: definition.id) {
                                        plan = PlanPresentation(plan: result)
                                    }
                                }
                            } label: { Label("Plan", systemImage: "list.bullet.clipboard") }
                                .tint(.blue)
                                .disabled(isDirty)
                        }
                        .swipeActions(edge: .trailing, allowsFullSwipe: false) {
                            Button(role: .destructive) {
                                definitions.remove(at: index)
                                isDirty = true
                            } label: { Label("Löschen", systemImage: "trash") }
                            Button {
                                pendingRun = PendingJobRun(definition: definition, dryRun: true)
                            } label: { Label("Test", systemImage: "play.circle") }
                                .tint(.orange)
                                .disabled(isDirty)
                        }
                    }
                }
            } header: {
                Text("Jobdefinitionen")
            } footer: {
                Text(isDirty ? "Änderungen speichern, bevor du Plan oder Lauf startest." : "Nach links wischen: Plan prüfen. Nach rechts wischen: Probelauf oder Löschen.")
            }
        }
        .listStyle(.insetGrouped)
        .navigationTitle("Jobs")
        .toolbar {
            ToolbarItem(placement: .topBarLeading) {
                SettingsButton(showingSettings: $showingSettings)
            }
            ToolbarItemGroup(placement: .topBarTrailing) {
                Button { editor = JobEditorRequest(index: nil, definition: nil) } label: {
                    Image(systemName: "plus")
                }
                .disabled(model.config?.backup.pairs.isEmpty != false)
                .accessibilityLabel("Job hinzufügen")
                Button("Speichern") { Task { await save(password: nil) } }
                    .disabled(!isDirty || model.isSavingConfig)
            }
        }
        .refreshable { await reload(discardDirty: false) }
        .sheet(item: $editor) { request in
            JobDefinitionEditor(
                definition: request.definition,
                paths: model.config?.backup.pairs ?? []
            ) { updated in
                if let index = request.index, definitions.indices.contains(index) {
                    definitions[index] = updated
                } else {
                    definitions.append(updated)
                }
                isDirty = true
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
                Button(pendingRun.dryRun ? "Probelauf starten" : "Produktiv starten") {
                    let request = pendingRun
                    self.pendingRun = nil
                    Task { _ = await model.runJobDefinition(id: request.definition.id, dryRun: request.dryRun) }
                }
                if pendingRun.dryRun {
                    Button("Stattdessen produktiv starten", role: .destructive) {
                        let request = pendingRun
                        self.pendingRun = nil
                        Task { _ = await model.runJobDefinition(id: request.definition.id, dryRun: false) }
                    }
                }
            }
            Button("Abbrechen", role: .cancel) { pendingRun = nil }
        } message: {
            Text("Prüfe vor einem produktiven Lauf den Plan und die Löschschutz-Einstellungen.")
        }
        .task { loadFromModel(force: false) }
        .onChange(of: model.config?.revision) { _, _ in loadFromModel(force: false) }
        .alert("Entwurf behalten", isPresented: Binding(
            get: { localError != nil },
            set: { if !$0 { localError = nil } }
        )) {
            Button("OK", role: .cancel) { localError = nil }
        } message: {
            Text(localError ?? "")
        }
    }

    private func pathNames(for definition: JobDefinition) -> String {
        let names = definition.dataPathIDs.compactMap { id in
            model.config?.backup.pairs.first { $0.id == id }?.name
        }
        return names.isEmpty ? "Keine Datenwege" : names.joined(separator: " → ")
    }

    private func loadFromModel(force: Bool) {
        guard force || !isDirty else { return }
        definitions = model.jobDefinitions
        isDirty = false
    }

    private func reload(discardDirty: Bool) async {
        if isDirty && !discardDirty {
            localError = "Ungespeicherte Job-Änderungen wurden nicht verworfen. Speichere sie zuerst oder nutze bei einem Konflikt bewusst „Serverstand laden“."
            return
        }
        await model.reloadConfiguration()
        loadFromModel(force: true)
    }

    private func save(password: String?) async {
        if await model.saveConfiguration(
            pairs: model.config?.backup.pairs ?? [],
            definitions: definitions,
            currentPassword: password
        ) {
            isDirty = false
            currentPassword = ""
            loadFromModel(force: true)
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
        HStack(spacing: 12) {
            Image(systemName: pair.enabled ? "arrow.left.arrow.right.circle.fill" : "pause.circle")
                .font(.title2)
                .foregroundStyle(pair.enabled ? .green : .secondary)
            VStack(alignment: .leading, spacing: 4) {
                Text(pair.name).font(.headline)
                Text(pair.local).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                Text(pair.remote).font(.caption).foregroundStyle(.secondary).lineLimit(1)
            }
            Spacer()
            Text(pair.direction.uppercased()).font(.caption.weight(.semibold))
        }
        .accessibilityElement(children: .combine)
        .padding(.vertical, 3)
    }
}

private struct JobDefinitionRow: View {
    let definition: JobDefinition
    let pathNames: String

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: definition.enabled ? "calendar.badge.clock" : "calendar.badge.minus")
                .font(.title2)
                .foregroundStyle(definition.enabled ? .green : .secondary)
            VStack(alignment: .leading, spacing: 4) {
                Text(definition.name).font(.headline)
                Text(definition.schedule == "manual" ? "Nur manuell" : definition.schedule)
                    .font(.caption).foregroundStyle(.secondary)
                Text(pathNames).font(.caption).foregroundStyle(.secondary).lineLimit(2)
            }
            Spacer()
            Image(systemName: "chevron.right").font(.caption.bold()).foregroundStyle(.tertiary)
        }
        .accessibilityElement(children: .combine)
        .padding(.vertical, 3)
    }
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
                    TextField("Remote oder Zielpfad", text: $remote)
                        .textInputAutocapitalization(.never).autocorrectionDisabled()
                    Toggle("Aktiv", isOn: $enabled)
                }
                Section("Übertragung") {
                    Picker("Richtung", selection: $direction) {
                        Text("Push").tag("push")
                        Text("Pull").tag("pull")
                        Text("Bi-Sync").tag("bisync")
                    }
                    .pickerStyle(.segmented)
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
                    TextField("Versionsablage", text: $backupDir)
                    if destructive {
                        Toggle("Löschungen ausdrücklich freigeben", isOn: $allowDelete)
                            .tint(.orange)
                        TextField("Maximale Löschungen", text: $maxDeleteText)
                            .keyboardType(.numberPad)
                    }
                }
                if let validationMessage {
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
        .sheet(item: $browseTarget) { _ in
            LocalPathBrowserSheet(initialPath: local) { local = $0 }
        }
    }

    private var destructive: Bool { direction == "bisync" || mode == "sync" }

    private var validationMessage: String? {
        let cleanName = name.trimmingCharacters(in: .whitespacesAndNewlines)
        if cleanName.isEmpty || cleanName.count > 80 || cleanName.contains("/") || cleanName.contains("\\") {
            return "Bitte einen gültigen Namen ohne Schrägstriche eingeben."
        }
        if !local.hasPrefix("/") { return "Der lokale Pfad muss absolut sein und mit / beginnen." }
        if !(remote.hasPrefix("/") || remote.contains(":")) {
            return "Das Ziel muss ein absoluter Pfad oder ein rclone-Remote wie cloud:Ordner sein."
        }
        if destructive && allowDelete && Int(maxDeleteText) == nil {
            return "Für freigegebene Löschungen ist eine begrenzte maximale Anzahl nötig."
        }
        return nil
    }

    private func commit() {
        let finalMode = direction == "bisync" ? "bisync" : mode
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
                sentinelFile: sentinelFile
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
                sentinelFile: sentinelFile
            )
        }
        save(updated)
        dismiss()
    }
}

private enum BrowseTarget: String, Identifiable {
    case local
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
        _schedule = State(initialValue: definition?.schedule ?? "manual")
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
                    TextField("Zeitplan oder manual", text: $schedule)
                        .textInputAutocapitalization(.never).autocorrectionDisabled()
                    Text("Cron: Minute Stunde Tag Monat Wochentag, z. B. 0 3 * * *")
                        .font(.caption).foregroundStyle(.secondary)
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
