import SwiftUI

struct QuickSyncView: View {
    @EnvironmentObject private var model: AppModel
    @State private var local = ""
    @State private var remote = ""
    @State private var direction = "push"
    @State private var mode = "copy"
    @State private var dryRun = true
    @State private var allowDelete = false
    @State private var maxDelete = "100"
    @State private var minLocalFiles = 1
    @State private var browseTarget: QuickBrowseTarget?
    @State private var confirmProductive = false

    var body: some View {
        Form {
            Section("Quelle und Ziel") {
                HStack {
                    TextField("Lokaler Pfad", text: $local)
                        .textInputAutocapitalization(.never).autocorrectionDisabled()
                    Button { browseTarget = .local } label: { Image(systemName: "folder") }
                        .accessibilityLabel("Lokalen Ordner auswählen")
                }
                HStack {
                    TextField("Remote oder lokaler Zielpfad", text: $remote)
                        .textInputAutocapitalization(.never).autocorrectionDisabled()
                    Button { browseTarget = .remote } label: { Image(systemName: "folder") }
                        .accessibilityLabel("Lokalen Zielordner auswählen")
                }
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
                Toggle("Nur Probelauf", isOn: $dryRun)
            }
            Section("Schutz") {
                Stepper("Mindestens \(minLocalFiles) lokale Dateien", value: $minLocalFiles, in: 0...1_000_000)
                if destructive && !dryRun {
                    Toggle("Löschungen ausdrücklich freigeben", isOn: $allowDelete).tint(.orange)
                    TextField("Maximale Löschungen", text: $maxDelete).keyboardType(.numberPad)
                    Label("Ein produktiver Sync kann Dateien löschen.", systemImage: "exclamationmark.shield")
                        .foregroundStyle(.orange)
                }
            }
            if let validationMessage {
                Section { Label(validationMessage, systemImage: "exclamationmark.triangle") }
                    .foregroundStyle(.red)
            }
            Section {
                Button(dryRun ? "Probelauf starten" : "Quick Sync starten") {
                    if dryRun { start() } else { confirmProductive = true }
                }
                .disabled(validationMessage != nil)
            }
        }
        .navigationTitle("Quick Sync")
        .navigationBarTitleDisplayMode(.inline)
        .sheet(item: $browseTarget) { target in
            LocalPathBrowserSheet(initialPath: target == .local ? local : remote) {
                if target == .local { local = $0 } else { remote = $0 }
            }
        }
        .confirmationDialog("Produktiven Quick Sync starten?", isPresented: $confirmProductive, titleVisibility: .visible) {
            Button("Mit Löschschutz starten", role: .destructive, action: start)
            Button("Abbrechen", role: .cancel) {}
        } message: {
            Text("Quelle, Ziel, Richtung und Löschlimit wurden geprüft. Der Lauf verändert Dateien.")
        }
        .onChange(of: direction) { _, value in if value == "bisync" { mode = "bisync" } else if mode == "bisync" { mode = "copy" } }
    }

    private var destructive: Bool { direction == "bisync" || mode == "sync" }

    private var validationMessage: String? {
        if !local.hasPrefix("/") { return "Wähle einen absoluten lokalen Pfad." }
        if !(remote.hasPrefix("/") || remote.contains(":")) { return "Gib einen absoluten Zielpfad oder ein rclone-Remote ein." }
        if destructive && !dryRun && (!allowDelete || Int(maxDelete) == nil) {
            return "Produktive Sync-Läufe benötigen Löschfreigabe und ein begrenztes Löschlimit."
        }
        return nil
    }

    private func start() {
        let request = QuickSyncRequest(
            remote: remote.trimmingCharacters(in: .whitespacesAndNewlines),
            local: local.trimmingCharacters(in: .whitespacesAndNewlines),
            direction: direction,
            mode: direction == "bisync" ? "bisync" : mode,
            dryRun: dryRun,
            allowDelete: destructive && !dryRun && allowDelete,
            maxDelete: destructive && !dryRun ? Int(maxDelete) : nil,
            minLocalFiles: minLocalFiles
        )
        Task { _ = await model.runQuickSync(request) }
    }
}

private enum QuickBrowseTarget: String, Identifiable {
    case local, remote
    var id: String { rawValue }
}

struct LocalPathBrowserSheet: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss
    let initialPath: String
    let select: (String) -> Void
    @State private var result: BrowseResponse?
    @State private var isLoading = false
    @State private var errorMessage: String?

    var body: some View {
        NavigationStack {
            List {
                if let errorMessage {
                    Label(errorMessage, systemImage: "exclamationmark.triangle").foregroundStyle(.red)
                }
                if let result {
                    Section {
                        Button { choose(result.path) } label: {
                            Label("Diesen Ordner wählen", systemImage: "checkmark.circle.fill")
                        }
                        .disabled(result.path == "/" || result.path.isEmpty)
                        if let parent = result.parent {
                            Button { Task { await load(parent) } } label: { Label("Übergeordnet", systemImage: "arrow.up") }
                        }
                    }
                    Section("Ordner") {
                        ForEach(result.entries) { entry in
                            Button { Task { await load(entry.path) } } label: {
                                HStack { Label(entry.name, systemImage: "folder"); Spacer(); Image(systemName: "chevron.right") }
                            }
                            .foregroundStyle(.primary)
                        }
                        if result.entries.isEmpty { Text("Keine Unterordner").foregroundStyle(.secondary) }
                    }
                    if result.truncated == true { Text("Weitere Einträge wurden aus Sicherheitsgründen ausgeblendet.").font(.caption) }
                } else if isLoading {
                    ProgressView("Ordner werden geladen …")
                }
            }
            .navigationTitle("Lokalen Ordner wählen")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Abbrechen") { dismiss() } } }
            .task { await load(initialPath.hasPrefix("/") ? initialPath : "") }
        }
    }

    private func load(_ path: String) async {
        isLoading = true
        errorMessage = nil
        do { result = try await model.withCurrentClient { try await $0.browseLocal(path: path) } }
        catch is CancellationError {}
        catch { errorMessage = error.localizedDescription }
        isLoading = false
    }

    private func choose(_ path: String) {
        select(path)
        dismiss()
    }
}

struct OperationsHubView: View {
    var body: some View {
        List {
            Section("Betrieb") {
                NavigationLink { AuditEventsView() } label: { Label("Audit-Protokoll", systemImage: "list.clipboard") }
                NavigationLink { SnapshotManagementView() } label: { Label("Konfigurations-Snapshots", systemImage: "clock.arrow.circlepath") }
                NavigationLink { DatabaseMaintenanceView() } label: { Label("Datenbank", systemImage: "cylinder") }
                NavigationLink { LogsAndSupportView() } label: { Label("Logs und Support-Bundle", systemImage: "doc.text.magnifyingglass") }
            }
            Section("Konfiguration") {
                NavigationLink { FilterFileView() } label: { Label("Filter-Datei", systemImage: "line.3.horizontal.decrease.circle") }
                NavigationLink { WebhookManagementView() } label: { Label("Webhooks", systemImage: "bell.badge") }
                NavigationLink { PasswordChangeView() } label: { Label("Passwort ändern", systemImage: "key") }
            }
        }
        .navigationTitle("Betrieb & Wartung")
        .navigationBarTitleDisplayMode(.inline)
    }
}

private struct AuditEventsView: View {
    @EnvironmentObject private var model: AppModel
    @State private var events: [AuditEvent] = []
    @State private var errorMessage: String?

    var body: some View {
        List {
            if let errorMessage { Label(errorMessage, systemImage: "exclamationmark.triangle").foregroundStyle(.red) }
            ForEach(events) { event in
                VStack(alignment: .leading, spacing: 4) {
                    Text(event.eventType).font(.headline)
                    Text("\(event.actor) · \(AppFormat.date(event.createdAt))").font(.caption).foregroundStyle(.secondary)
                }
            }
            if events.isEmpty && errorMessage == nil { ProgressView("Audit-Protokoll wird geladen …") }
        }
        .navigationTitle("Audit-Protokoll")
        .refreshable { await load() }
        .task { await load() }
    }

    private func load() async {
        do { events = try await model.withCurrentClient { try await $0.getAuditEvents(limit: 200) }.events; errorMessage = nil }
        catch is CancellationError {}
        catch { errorMessage = error.localizedDescription }
    }
}

private struct SnapshotManagementView: View {
    @EnvironmentObject private var model: AppModel
    @State private var snapshots: [ConfigSnapshotEntry] = []
    @State private var maxSnapshots = 0
    @State private var restore: ConfigSnapshotEntry?
    @State private var errorMessage: String?

    var body: some View {
        List {
            if let errorMessage { Label(errorMessage, systemImage: "exclamationmark.triangle").foregroundStyle(.red) }
            Section {
                Button { Task { await create() } } label: { Label("Snapshot erstellen", systemImage: "plus.circle") }
            } footer: { Text(maxSnapshots > 0 ? "Der Server behält maximal \(maxSnapshots) Snapshots." : "Vor riskanten Änderungen einen Snapshot erstellen.") }
            Section("Snapshots") {
                ForEach(snapshots) { snapshot in
                    Button { restore = snapshot } label: {
                        VStack(alignment: .leading, spacing: 4) {
                            Text(AppFormat.date(snapshot.mtime)).font(.headline).foregroundStyle(.primary)
                            Text("\(AppFormat.bytes(snapshot.size)) · \(snapshot.name)").font(.caption).foregroundStyle(.secondary).lineLimit(2)
                        }
                    }
                }
                if snapshots.isEmpty { Text("Keine Snapshots vorhanden").foregroundStyle(.secondary) }
            }
        }
        .navigationTitle("Snapshots")
        .refreshable { await load() }
        .task { await load() }
        .sheet(item: $restore) { SnapshotRestoreSheet(snapshot: $0) { await load() } }
    }

    private func load() async {
        do {
            let response = try await model.withCurrentClient { try await $0.getConfigSnapshots() }
            snapshots = response.snapshots; maxSnapshots = response.maxSnapshots; errorMessage = nil
        } catch is CancellationError {} catch { errorMessage = error.localizedDescription }
    }

    private func create() async {
        do { _ = try await model.withCurrentClient { try await $0.createConfigSnapshot() }; await load() }
        catch is CancellationError {} catch { errorMessage = error.localizedDescription }
    }
}

private struct SnapshotRestoreSheet: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss
    let snapshot: ConfigSnapshotEntry
    let completed: () async -> Void
    @State private var password = ""
    @State private var confirmed = false
    @State private var errorMessage: String?

    var body: some View {
        NavigationStack {
            Form {
                Section("Ausgewählt") {
                    LabeledContent("Zeitpunkt", value: AppFormat.date(snapshot.mtime))
                    Text(snapshot.name).font(.caption).textSelection(.enabled)
                }
                Section("Bestätigung") {
                    SecureField("Aktuelles Passwort", text: $password).textContentType(.password)
                    Toggle("Ich verstehe, dass die aktuelle Konfiguration ersetzt wird", isOn: $confirmed)
                }
                if let errorMessage { Section { Label(errorMessage, systemImage: "exclamationmark.triangle").foregroundStyle(.red) } }
                Section { Button("Snapshot wiederherstellen", role: .destructive) { Task { await restore() } }.disabled(password.isEmpty || !confirmed) }
            }
            .navigationTitle("Snapshot wiederherstellen")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Abbrechen") { dismiss() } } }
        }
    }

    private func restore() async {
        guard let revision = model.config?.revision else { return }
        do {
            let response = try await model.withCurrentClient {
                try await $0.restoreConfigSnapshot(SnapshotRestoreRequest(
                    name: snapshot.name,
                    currentPassword: password,
                    expectedRevision: revision,
                    sha256: snapshot.sha256
                ))
            }
            if response.reauthenticate {
                model.requireFreshLogin("Snapshot wiederhergestellt. Bitte melde dich erneut an.")
            } else {
                await completed(); dismiss()
            }
        } catch APIError.revisionConflict(let message, _) {
            await model.reloadConfiguration()
            errorMessage = "\(message). Der aktuelle Serverstand wurde geladen; prüfe den Snapshot und bestätige erneut."
        } catch let error as APIError { errorMessage = error.localizedDescription }
        catch { errorMessage = error.localizedDescription }
    }
}

private struct DatabaseMaintenanceView: View {
    @EnvironmentObject private var model: AppModel
    @State private var status: DatabaseStatus?
    @State private var confirmPrune = false
    @State private var errorMessage: String?

    var body: some View {
        List {
            if let status {
                Section("Integrität") {
                    LabeledContent("Status") { StatusBadge(status: status.integrity.ok ? "ok" : "error") }
                    LabeledContent("Quick Check", value: status.integrity.quickCheck)
                    LabeledContent("Fremdschlüsselfehler", value: "\(status.integrity.foreignKeyErrors)")
                }
                Section("Bestand") {
                    LabeledContent("Jobs", value: "\(status.stats.jobs)")
                    LabeledContent("Pair-Läufe", value: "\(status.stats.pairRuns)")
                    LabeledContent("Audit-Ereignisse", value: "\(status.stats.auditEvents)")
                    LabeledContent("Größe", value: AppFormat.bytes(status.stats.bytes))
                }
                Section { Button("Alte Datensätze bereinigen", role: .destructive) { confirmPrune = true } }
            } else { ProgressView("Datenbankstatus wird geladen …") }
            if let errorMessage { Label(errorMessage, systemImage: "exclamationmark.triangle").foregroundStyle(.red) }
        }
        .navigationTitle("Datenbank")
        .refreshable { await load() }
        .task { await load() }
        .confirmationDialog("Datenbank bereinigen?", isPresented: $confirmPrune, titleVisibility: .visible) {
            Button("Älter als 180 Tage bereinigen", role: .destructive) { Task { await prune() } }
            Button("Abbrechen", role: .cancel) {}
        } message: { Text("Die 500 neuesten Jobs bleiben erhalten. Laufende Jobs werden nicht gelöscht.") }
    }

    private func load() async {
        do { status = try await model.withCurrentClient { try await $0.getDatabaseStatus() }; errorMessage = nil }
        catch is CancellationError {} catch { errorMessage = error.localizedDescription }
    }

    private func prune() async {
        do { _ = try await model.withCurrentClient { try await $0.pruneDatabase(days: 180, keepLatest: 500) }; await load() }
        catch is CancellationError {} catch { errorMessage = error.localizedDescription }
    }
}

private struct LogsAndSupportView: View {
    @EnvironmentObject private var model: AppModel
    @State private var logs: [MaintenanceLog] = []
    @State private var supportURL: URL?
    @State private var errorMessage: String?

    var body: some View {
        List {
            Section("Support") {
                if let supportURL {
                    ShareLink(item: supportURL) { Label("Support-Bundle teilen", systemImage: "square.and.arrow.up") }
                } else {
                    Button { Task { await prepareBundle() } } label: { Label("Support-Bundle erstellen", systemImage: "shippingbox") }
                }
                Text("Das Bundle ist serverseitig redigiert. Lokale Pfade und Fehlertexte können enthalten sein.").font(.caption).foregroundStyle(.secondary)
            }
            Section("Log-Dateien") {
                ForEach(logs) { log in
                    VStack(alignment: .leading, spacing: 4) {
                        Text(log.path).font(.subheadline).textSelection(.enabled)
                        Text("\(AppFormat.bytes(log.size)) · \(AppFormat.date(log.mtime))").font(.caption).foregroundStyle(.secondary)
                    }
                }
                if logs.isEmpty { Text("Keine Logs gefunden").foregroundStyle(.secondary) }
            }
            if let errorMessage { Label(errorMessage, systemImage: "exclamationmark.triangle").foregroundStyle(.red) }
        }
        .navigationTitle("Logs & Support")
        .refreshable { await load() }
        .task { await load() }
    }

    private func load() async {
        do { logs = try await model.withCurrentClient { try await $0.getMaintenanceLogs(limit: 200) }.logs; errorMessage = nil }
        catch is CancellationError {} catch { errorMessage = error.localizedDescription }
    }

    private func prepareBundle() async {
        do { supportURL = try await model.withCurrentClient { try await $0.downloadSupportBundle() }; errorMessage = nil }
        catch is CancellationError {} catch { errorMessage = error.localizedDescription }
    }
}

private struct FilterFileView: View {
    @EnvironmentObject private var model: AppModel
    @State private var filter: FilterFile?
    @State private var content = ""
    @State private var errorMessage: String?

    var body: some View {
        Form {
            if let filter {
                Section("Datei") {
                    Text(filter.path).font(.caption).textSelection(.enabled)
                    TextEditor(text: $content).font(.system(.body, design: .monospaced)).frame(minHeight: 300)
                }
                Section {
                    Button("Speichern") { Task { await save() } }.disabled(!isDirty)
                    Button("Serverstand neu laden") { Task { await load() } }
                } footer: { Text("Einträge werden direkt von rclone ausgewertet. Die Serverrevision schützt vor Überschreiben paralleler Änderungen.") }
            } else { ProgressView("Filter-Datei wird geladen …") }
            if let errorMessage { Section { Label(errorMessage, systemImage: "exclamationmark.triangle").foregroundStyle(.red) } }
        }
        .navigationTitle("Filter-Datei")
        .task { await load() }
    }

    private func load() async {
        do {
            let value = try await model.withCurrentClient { try await $0.getFilterFile() }
            filter = value; content = value.content; errorMessage = nil
        } catch is CancellationError {} catch { errorMessage = error.localizedDescription }
    }

    private func save() async {
        guard let filter else { return }
        do {
            let result = try await model.withCurrentClient {
                try await $0.saveFilterFile(FilterFileSaveRequest(content: content, revision: filter.revision))
            }
            self.filter = FilterFile(path: result.path, exists: true, content: content, revision: result.revision)
            errorMessage = nil
        } catch APIError.revisionConflict(let message, _) {
            errorMessage = "\(message). Lade den Serverstand neu."
        } catch is CancellationError {} catch { errorMessage = error.localizedDescription }
    }

    private var isDirty: Bool { filter?.content != content }
}

private struct WebhookManagementView: View {
    @EnvironmentObject private var model: AppModel
    @State private var hooks: [WebhookConfig] = []
    @State private var editor: WebhookEditorRequest?
    @State private var isDirty = false
    @State private var password = ""

    var body: some View {
        List {
            if let issue = model.configSaveIssue { Section("Speichern") { Text(issueText(issue)).foregroundStyle(.orange); if case .passwordRequired = issue { SecureField("Aktuelles Passwort", text: $password) } } }
            Section("Webhooks") {
                ForEach(Array(hooks.enumerated()), id: \.element.id) { index, hook in
                    Button { editor = WebhookEditorRequest(index: index, hook: hook) } label: {
                        HStack { Label(hook.type.capitalized, systemImage: hook.enabled ? "bell.fill" : "bell.slash"); Spacer(); Text("\(hook.events.count) Events").foregroundStyle(.secondary) }
                    }.foregroundStyle(.primary)
                    .swipeActions(edge: .leading, allowsFullSwipe: false) {
                        Button { Task { await test(hook) } } label: { Label("Test", systemImage: "paperplane") }
                            .tint(.blue)
                            .disabled(isDirty || !hook.enabled)
                    }
                }
                .onDelete { hooks.remove(atOffsets: $0); isDirty = true }
                Button { editor = WebhookEditorRequest(index: nil, hook: nil) } label: { Label("Webhook hinzufügen", systemImage: "plus") }
            }
            Section {
                Button("Speichern") { Task { await save() } }.disabled(!isDirty || model.isSavingConfig)
                if case .passwordRequired? = model.configSaveIssue {
                    Button("Mit Passwort speichern") { Task { await save(password: password) } }.disabled(password.isEmpty)
                }
                Button("Serverstand neu laden") { Task { await reload() } }
            }
        }
        .navigationTitle("Webhooks")
        .task { if !isDirty { hooks = model.config?.webhooks ?? [] } }
        .sheet(item: $editor) { request in
            WebhookEditor(hook: request.hook) { value in
                if let index = request.index { hooks[index] = value } else { hooks.append(value) }
                isDirty = true
            }
        }
    }

    private func save(password: String? = nil) async {
        if await model.saveWebhooks(hooks, currentPassword: password) { isDirty = false; self.password = "" }
    }

    private func reload() async { await model.reloadConfiguration(); hooks = model.config?.webhooks ?? []; isDirty = false }

    private func test(_ hook: WebhookConfig) async {
        do {
            let response = try await model.withCurrentClient { try await $0.testWebhook(id: hook.id) }
            model.actionMessage = response.ok ? "Webhook-Test wurde gesendet." : (response.error ?? "Webhook-Test fehlgeschlagen.")
        } catch { model.errorMessage = error.localizedDescription }
    }

    private func issueText(_ issue: ConfigSaveIssue) -> String {
        switch issue { case let .conflict(value), let .passwordRequired(value): value; case let .validation(values): values.joined(separator: "\n") }
    }
}

private struct WebhookEditorRequest: Identifiable { let id = UUID(); let index: Int?; let hook: WebhookConfig? }

private struct WebhookEditor: View {
    @Environment(\.dismiss) private var dismiss
    let original: WebhookConfig?
    let save: (WebhookConfig) -> Void
    @State private var enabled: Bool
    @State private var type: String
    @State private var url: String
    @State private var events: Set<String>
    private let availableEvents = ["sync_started", "sync_ok", "sync_error", "conflict", "mount_check_failed", "cancelled", "pair_overdue", "restore_test_ok", "restore_test_error"]

    init(hook: WebhookConfig?, save: @escaping (WebhookConfig) -> Void) {
        original = hook; self.save = save
        _enabled = State(initialValue: hook?.enabled ?? true)
        _type = State(initialValue: hook?.type ?? "generic")
        _url = State(initialValue: hook?.url == "***SET***" ? "" : (hook?.url ?? ""))
        _events = State(initialValue: Set(hook?.events ?? ["sync_error"]))
    }

    var body: some View {
        NavigationStack {
            Form {
                Section("Ziel") {
                    Toggle("Aktiv", isOn: $enabled)
                    Picker("Typ", selection: $type) { Text("Generisch").tag("generic"); Text("Discord").tag("discord"); Text("Telegram").tag("telegram") }
                    TextField(original == nil ? "HTTPS-URL" : "Neue HTTPS-URL (leer = unverändert)", text: $url)
                        .textInputAutocapitalization(.never).autocorrectionDisabled().textContentType(.URL)
                }
                Section("Ereignisse") {
                    ForEach(availableEvents, id: \.self) { event in
                        Toggle(event, isOn: Binding(get: { events.contains(event) }, set: { if $0 { events.insert(event) } else { events.remove(event) } }))
                    }
                }
            }
            .navigationTitle(original == nil ? "Webhook hinzufügen" : "Webhook bearbeiten")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Abbrechen") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) { Button("Übernehmen") { commit() }.disabled(!valid) }
            }
        }
    }

    private var valid: Bool {
        let validURL = (original != nil && url.isEmpty) || URL(string: url)?.scheme == "https"
        return validURL && !events.isEmpty
    }
    private func commit() {
        let orderedEvents = availableEvents.filter(events.contains)
        let storedURL = original != nil && url.isEmpty ? "***SET***" : url
        if let original {
            save(original.replacing(enabled: enabled, type: type, url: storedURL, events: orderedEvents))
        } else {
            save(WebhookConfig(
                id: UUID().uuidString.replacingOccurrences(of: "-", with: ""),
                enabled: enabled,
                type: type,
                url: storedURL,
                events: orderedEvents
            ))
        }
        dismiss()
    }
}

private struct PasswordChangeView: View {
    @EnvironmentObject private var model: AppModel
    @State private var current = ""
    @State private var new = ""
    @State private var confirmation = ""
    @State private var confirmChange = false

    var body: some View {
        Form {
            Section("Passwort") {
                SecureField("Aktuelles Passwort", text: $current).textContentType(.password)
                SecureField("Neues Passwort", text: $new).textContentType(.newPassword)
                SecureField("Neues Passwort wiederholen", text: $confirmation).textContentType(.newPassword)
            }
            if let validation { Section { Label(validation, systemImage: "exclamationmark.triangle").foregroundStyle(.red) } }
            Section { Button("Passwort ändern", role: .destructive) { confirmChange = true }.disabled(validation != nil) } footer: {
                Text("Die Änderung beendet alle aktiven Sitzungen. Anschließend ist eine neue Anmeldung nötig.")
            }
        }
        .navigationTitle("Passwort ändern")
        .confirmationDialog("Passwort wirklich ändern?", isPresented: $confirmChange, titleVisibility: .visible) {
            Button("Ändern und abmelden", role: .destructive) { Task { _ = await model.changePassword(current: current, new: new) } }
            Button("Abbrechen", role: .cancel) {}
        }
    }

    private var validation: String? {
        if current.isEmpty { return "Gib dein aktuelles Passwort ein." }
        if new.count < 12 { return "Das neue Passwort braucht mindestens 12 Zeichen." }
        if new != confirmation { return "Die Wiederholung stimmt nicht überein." }
        if new == current { return "Das neue Passwort muss sich unterscheiden." }
        return nil
    }
}
