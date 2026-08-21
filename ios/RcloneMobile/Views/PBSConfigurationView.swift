import SwiftUI

struct PBSConfigurationView: View {
    @EnvironmentObject private var model: AppModel
    @State private var draft = PBSConfiguration()
    @State private var loaded: PBSConfiguration?
    @State private var replacementPassword = ""
    @State private var currentPassword = ""
    @State private var editor: PBSTargetEditorRequest?
    @State private var localError: String?

    var body: some View {
        Form {
            saveIssueSection
            serverSection
            retentionSection
            targetsSection
            validationSection
        }
        .navigationTitle("PBS konfigurieren")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .confirmationAction) {
                Button("Speichern") { Task { await save(currentPassword: nil) } }
                    .disabled(!isDirty || validationMessage != nil || model.isSavingConfig)
            }
        }
        .refreshable { await reload(discardDirty: false) }
        .sheet(item: $editor) { request in
            PBSTargetEditor(target: request.target) { target in
                if let index = request.index, draft.targets.indices.contains(index) {
                    draft.targets[index] = target
                } else {
                    draft.targets.append(target)
                }
            }
        }
        .task { loadFromModel(force: false) }
        .onChange(of: model.config?.revision) { _, _ in loadFromModel(force: false) }
        .alert("Entwurf behalten", isPresented: Binding(
            get: { localError != nil },
            set: { if !$0 { localError = nil } }
        )) { Button("OK", role: .cancel) {} } message: { Text(localError ?? "") }
    }

    @ViewBuilder
    private var saveIssueSection: some View {
        if let issue = model.configSaveIssue {
            Section("Speichern nicht abgeschlossen") {
                Text(issueText(issue)).foregroundStyle(.orange)
                if case .passwordRequired = issue {
                    SecureField("Aktuelles App-Passwort", text: $currentPassword)
                        .textContentType(.password)
                    Button("Mit Passwort speichern") { Task { await save(currentPassword: currentPassword) } }
                        .disabled(currentPassword.isEmpty || validationMessage != nil)
                }
                Button("Serverstand neu laden") { Task { await reload(discardDirty: true) } }
            }
        }
    }

    private var serverSection: some View {
        Section("PBS-Server") {
            Toggle("PBS-Integration aktiv", isOn: $draft.enabled)
            TextField("user@realm!token@host:datastore", text: $draft.repository)
                .textInputAutocapitalization(.never).autocorrectionDisabled()
            TextField("Namespace (optional)", text: $draft.namespace)
                .textInputAutocapitalization(.never).autocorrectionDisabled()
            TextField("Standard Backup-ID (optional)", text: $draft.backupID)
                .textInputAutocapitalization(.never).autocorrectionDisabled()
            TextField("SHA-256-Fingerprint (optional)", text: $draft.fingerprint)
                .textInputAutocapitalization(.never).autocorrectionDisabled()
            SecureField(
                draft.password == "***SET***" ? "Neues Token/Passwort (leer = unverändert)" : "Token/Passwort",
                text: $replacementPassword
            )
            .textContentType(.password)
            Stepper(
                "Zeitlimit: \(draft.timeoutHours.formatted(.number.precision(.fractionLength(1)))) h",
                value: $draft.timeoutHours,
                in: 0.5...168,
                step: 0.5
            )
        }
    }

    private var retentionSection: some View {
        Section("Aufbewahrung") {
            retentionStepper("Letzte", keyPath: \.last)
            retentionStepper("Täglich", keyPath: \.daily)
            retentionStepper("Wöchentlich", keyPath: \.weekly)
            retentionStepper("Monatlich", keyPath: \.monthly)
            retentionStepper("Jährlich", keyPath: \.yearly)
        }
    }

    private var targetsSection: some View {
        Section("Targets") {
            ForEach(Array(draft.targets.enumerated()), id: \.element.id) { index, target in
                Button { editor = PBSTargetEditorRequest(index: index, target: target) } label: {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(target.name.isEmpty ? "Unbenanntes Target" : target.name).font(.headline)
                        Text("\(target.paths.count) Pfade · \(target.schedule == "manual" ? "manuell" : target.schedule)")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }
                .foregroundStyle(.primary)
            }
            .onDelete { draft.targets.remove(atOffsets: $0) }
            Button { editor = PBSTargetEditorRequest(index: nil, target: nil) } label: {
                Label("Target hinzufügen", systemImage: "plus")
            }
        } footer: {
            Text("Mehrere Targets brauchen jeweils eine eigene Backup-ID. Pfade beziehen sich auf den Server, nicht auf das iPhone.")
        }
    }

    @ViewBuilder
    private var validationSection: some View {
        if let validationMessage {
            Section { Label(validationMessage, systemImage: "exclamationmark.triangle").foregroundStyle(.red) }
        }
    }

    private var isDirty: Bool {
        guard let loaded else { return false }
        return draft != loaded || !replacementPassword.isEmpty
    }

    private var validationMessage: String? {
        if draft.enabled && draft.repository.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return "Ein aktives PBS braucht ein Repository."
        }
        let names = draft.targets.map { $0.name.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() }
        if names.contains("") { return "Jedes Target braucht einen Namen." }
        if Set(names).count != names.count { return "Target-Namen müssen eindeutig sein." }
        if draft.targets.contains(where: { $0.paths.isEmpty || $0.paths.contains(where: { !$0.hasPrefix("/") }) }) {
            return "Jedes Target braucht mindestens einen absoluten Serverpfad."
        }
        if draft.targets.count > 1 && draft.targets.contains(where: { $0.backupID.isEmpty }) {
            return "Bei mehreren Targets braucht jedes Target eine eigene Backup-ID."
        }
        return nil
    }

    private func retentionStepper(
        _ title: String,
        keyPath: WritableKeyPath<PBSKeepConfiguration, Int>
    ) -> some View {
        let value = Binding(
            get: { draft.keep[keyPath: keyPath] },
            set: { draft.keep[keyPath: keyPath] = $0 }
        )
        return Stepper("\(title): \(value.wrappedValue)", value: value, in: 0...3650)
    }

    private func loadFromModel(force: Bool) {
        guard force || !isDirty, let configuration = model.config?.pbsConfiguration else { return }
        loaded = configuration
        draft = configuration
        replacementPassword = ""
        currentPassword = ""
    }

    private func reload(discardDirty: Bool) async {
        if isDirty && !discardDirty {
            localError = "Ungespeicherte PBS-Änderungen wurden nicht verworfen. Speichere sie zuerst oder lade den Serverstand bewusst neu."
            return
        }
        await model.reloadConfiguration()
        loadFromModel(force: true)
    }

    private func save(currentPassword: String?) async {
        var candidate = draft
        if !replacementPassword.isEmpty { candidate.password = replacementPassword }
        if await model.savePBSConfiguration(candidate, currentPassword: currentPassword) {
            loadFromModel(force: true)
        }
    }

    private func issueText(_ issue: ConfigSaveIssue) -> String {
        switch issue {
        case let .conflict(message), let .passwordRequired(message): message
        case let .validation(errors): errors.joined(separator: "\n")
        }
    }
}

private struct PBSTargetEditorRequest: Identifiable {
    let id = UUID()
    let index: Int?
    let target: PBSTargetConfiguration?
}

private struct PBSTargetEditor: View {
    @Environment(\.dismiss) private var dismiss
    let original: PBSTargetConfiguration?
    let save: (PBSTargetConfiguration) -> Void
    @State private var target: PBSTargetConfiguration
    @State private var pathsText: String

    init(target: PBSTargetConfiguration?, save: @escaping (PBSTargetConfiguration) -> Void) {
        original = target
        self.save = save
        _target = State(initialValue: target ?? PBSTargetConfiguration())
        _pathsText = State(initialValue: (target?.paths ?? []).joined(separator: "\n"))
    }

    var body: some View {
        NavigationStack {
            Form {
                Section("Target") {
                    TextField("Name", text: $target.name)
                    TextField("Zeitplan oder manual", text: $target.schedule)
                        .textInputAutocapitalization(.never).autocorrectionDisabled()
                    TextField("Backup-ID", text: $target.backupID)
                        .textInputAutocapitalization(.never).autocorrectionDisabled()
                    TextField("Namespace (optional)", text: $target.namespace)
                        .textInputAutocapitalization(.never).autocorrectionDisabled()
                }
                Section("Serverpfade – einer pro Zeile") {
                    TextEditor(text: $pathsText)
                        .font(.system(.body, design: .monospaced))
                        .frame(minHeight: 150)
                }
                Section("Schutz") {
                    Stepper("Mindestens \(target.minFiles) Dateien", value: $target.minFiles, in: 0...10_000_000)
                    Toggle("Mountpoint verlangen", isOn: $target.requireMountpoint)
                    if target.requireMountpoint {
                        TextField("Mountpoint", text: $target.mountpoint)
                            .textInputAutocapitalization(.never).autocorrectionDisabled()
                    }
                    TextField("Sentinel-Datei (optional, relativ)", text: $target.sentinelFile)
                        .textInputAutocapitalization(.never).autocorrectionDisabled()
                }
                if let validationMessage {
                    Section { Label(validationMessage, systemImage: "exclamationmark.triangle").foregroundStyle(.red) }
                }
            }
            .navigationTitle(original == nil ? "PBS-Target hinzufügen" : "PBS-Target bearbeiten")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Abbrechen") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) { Button("Übernehmen") { commit() }.disabled(validationMessage != nil) }
            }
        }
    }

    private var cleanPaths: [String] {
        pathsText.split(whereSeparator: { $0.isNewline }).map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { !$0.isEmpty }
    }

    private var validationMessage: String? {
        if target.name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { return "Gib einen Namen ein." }
        if target.name.contains(",") { return "Der Name darf kein Komma enthalten." }
        if cleanPaths.isEmpty || cleanPaths.contains(where: { !$0.hasPrefix("/") }) { return "Gib mindestens einen absoluten Serverpfad ein." }
        if target.schedule.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { return "Gib einen Zeitplan oder manual ein." }
        if target.requireMountpoint && !target.mountpoint.hasPrefix("/") { return "Der Mountpoint muss absolut sein." }
        if target.sentinelFile.hasPrefix("/") || target.sentinelFile.split(separator: "/").contains("..") { return "Die Sentinel-Datei muss relativ und sicher sein." }
        return nil
    }

    private func commit() {
        target.name = target.name.trimmingCharacters(in: .whitespacesAndNewlines)
        target.schedule = target.schedule.trimmingCharacters(in: .whitespacesAndNewlines)
        target.paths = cleanPaths
        target.backupID = target.backupID.trimmingCharacters(in: .whitespacesAndNewlines)
        target.namespace = target.namespace.trimmingCharacters(in: .whitespacesAndNewlines)
        target.mountpoint = target.mountpoint.trimmingCharacters(in: .whitespacesAndNewlines)
        target.sentinelFile = target.sentinelFile.trimmingCharacters(in: .whitespacesAndNewlines)
        save(target)
        dismiss()
    }
}
