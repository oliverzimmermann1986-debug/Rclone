import SwiftUI

struct RecoveryCenterView: View {
    @EnvironmentObject private var model: AppModel
    @State private var recoveryPass: RecoveryPassResponse?
    @State private var calendar: RecoveryCalendarResponse?
    @State private var policies: [RecoveryPolicyProfile] = []
    @State private var isLoading = false
    @State private var errorMessage: String?
    @State private var exportURL: URL?
    @State private var showingHandover = false

    var body: some View {
        List {
            if let recoveryPass {
                protectionHeader(recoveryPass)
                if recoveryPass.quarantine.active > 0 {
                    quarantineSection(recoveryPass.quarantine)
                }
                Section("Datenwege") {
                    ForEach(recoveryPass.dataPaths) { path in
                        NavigationLink {
                            RecoveryDataPathDetail(dataPath: path)
                        } label: {
                            RecoveryPathRow(path: path)
                        }
                    }
                }
                calendarSection
                policySection
                emergencySection(recoveryPass)
                exportSection
            } else if isLoading {
                LoadingSection(label: "Recovery-Nachweise werden geladen …")
            } else if let errorMessage {
                LoadFailureView(title: "Recovery Center nicht geladen", message: errorMessage) {
                    Task { await load() }
                }
            }
        }
        .navigationTitle("Recovery Center")
        .navigationBarTitleDisplayMode(.inline)
        .refreshable { await load() }
        .task { await load() }
        .sheet(isPresented: $showingHandover) {
            NavigationStack { RecoveryHandoverView() }
        }
    }

    private func protectionHeader(_ pass: RecoveryPassResponse) -> some View {
        Section {
            VStack(alignment: .leading, spacing: 16) {
                HStack(spacing: 14) {
                    ZStack {
                        Circle().fill(scoreColor(pass.protection.score).opacity(0.14))
                        Image(systemName: "lifepreserver.fill")
                            .font(.title2)
                            .foregroundStyle(scoreColor(pass.protection.score))
                    }
                    .frame(width: 52, height: 52)
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Recovery-Pass").font(.headline)
                        Text("\(pass.hostname) · \(AppFormat.relative(pass.generatedAt))")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Text("\(pass.protection.score)")
                        .font(.title.monospacedDigit().bold())
                }
                ProgressView(value: Double(pass.protection.score), total: 100)
                    .tint(scoreColor(pass.protection.score))
                HStack(spacing: 0) {
                    ForEach(pass.protection.components) { component in
                        VStack(spacing: 2) {
                            Text("\(component.points)/\(component.maximum)")
                                .font(.caption.monospacedDigit().weight(.semibold))
                            Text(componentLabel(component.key))
                                .font(.caption2).foregroundStyle(.secondary)
                                .lineLimit(1).minimumScaleFactor(0.65)
                        }
                        .frame(maxWidth: .infinity)
                    }
                }
            }
            .padding(.vertical, 6)
            .accessibilityElement(children: .combine)
        } footer: {
            Text("Der Wert entsteht aus realen Jobs, Zeitplänen, Aktualität, Restore-Prüfungen und Löschschutz.")
        }
    }

    private func quarantineSection(_ quarantine: RecoveryQuarantineResponse) -> some View {
        Section("Sicherheitsstopps") {
            ForEach(quarantine.items) { item in
                NavigationLink {
                    QuarantineDetailView(item: item) { await load() }
                } label: {
                    VStack(alignment: .leading, spacing: 5) {
                        Label(item.pair, systemImage: "hand.raised.fill")
                            .font(.headline).foregroundStyle(.red)
                        Text("Quelle unerwartet geschrumpft · \(AppFormat.relative(item.detectedAt))")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }
            }
        }
    }

    @ViewBuilder
    private var calendarSection: some View {
        if let days = calendar?.days, !days.isEmpty {
            Section {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 9) {
                        ForEach(days.prefix(45).reversed()) { day in
                            VStack(spacing: 5) {
                                Circle()
                                    .fill(calendarColor(day.state))
                                    .frame(width: 12, height: 12)
                                Text(shortDay(day.date))
                                    .font(.caption2.monospacedDigit())
                                if day.restoreTests > 0 {
                                    Image(systemName: "arrow.counterclockwise.circle.fill")
                                        .font(.caption2).foregroundStyle(.green)
                                } else {
                                    Color.clear.frame(width: 10, height: 10)
                                }
                            }
                            .accessibilityElement(children: .ignore)
                            .accessibilityLabel("\(day.date), \(day.failed > 0 ? "Fehler" : day.successful > 0 ? "erfolgreich" : "kein Lauf")")
                        }
                    }
                    .padding(.vertical, 4)
                }
            } header: {
                Text("Schutzkalender")
            } footer: {
                Text("Grün: erfolgreich · Orange: abgebrochen · Rot: Fehler · Ring: Restore-Prüfung")
            }
        }
    }

    @ViewBuilder
    private var policySection: some View {
        if !policies.isEmpty {
            Section {
                ForEach(policies) { policy in
                    DisclosureGroup {
                        Text(policy.description).font(.subheadline).foregroundStyle(.secondary)
                    } label: {
                        Label(policy.name, systemImage: policySymbol(policy.id))
                    }
                }
            } header: {
                Text("Schutzprofile")
            } footer: {
                Text("Profile sind sichere Ausgangspunkte. Übernahme erfolgt bewusst im Job-Editor, nie automatisch.")
            }
        }
    }

    private func emergencySection(_ pass: RecoveryPassResponse) -> some View {
        Section {
            Label("Letzten Recovery-Pass auf diesem iPhone gesichert", systemImage: "iphone.and.arrow.forward")
                .foregroundStyle(.green)
            LabeledContent("Stand", value: AppFormat.date(pass.generatedAt))
            DisclosureGroup("Notfallablauf") {
                VStack(alignment: .leading, spacing: 9) {
                    Text("1. Automatische Jobs pausieren.")
                    Text("2. Incident und Sicherungsziel prüfen.")
                    Text("3. Benötigte Dateien nur ins Recovery-Staging holen.")
                    Text("4. Prüfsummen bestätigen, erst danach produktiv übernehmen.")
                }
                .font(.subheadline)
            }
        } header: {
            Text("Offline-Notfallkarte")
        } footer: {
            Text("Die Karte enthält standardmäßig keine Serverpfade, Passwörter oder Cloud-Schlüssel.")
        }
    }

    private var exportSection: some View {
        Section("Übergabe") {
            Button {
                Task { await exportPass() }
            } label: {
                Label("Recovery-Pass exportieren", systemImage: "square.and.arrow.up")
            }
            if let exportURL {
                ShareLink(item: exportURL) {
                    Label("Fertigen Recovery-Pass teilen", systemImage: "checkmark.circle.fill")
                        .foregroundStyle(.green)
                }
            }
            Button { showingHandover = true } label: {
                Label("Verschlüsseltes Übergabepaket", systemImage: "lock.doc")
            }
            .disabled(model.isDemoMode)
            if model.isDemoMode {
                Text("Die lokale Vorschau zeigt Aufbau und Nachweise. Ein Übergabepaket benötigt einen eigenen Server.")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
    }

    private func load() async {
        if model.isDemoMode {
            loadDemo()
            return
        }
        isLoading = recoveryPass == nil
        defer { isLoading = false }
        do {
            async let passTask = model.withCurrentClient { try await $0.getRecoveryPass(includePaths: false) }
            async let calendarTask = model.withCurrentClient { try await $0.getRecoveryCalendar(days: 90) }
            async let policiesTask = model.withCurrentClient { try await $0.getRecoveryPolicies() }
            let (freshPass, freshCalendar, freshPolicies) = try await (passTask, calendarTask, policiesTask)
            recoveryPass = freshPass
            calendar = freshCalendar
            policies = freshPolicies.profiles
            if let data = try? JSONEncoder().encode(freshPass) {
                UserDefaults.standard.set(data, forKey: "offlineRecoveryPass")
            }
            ProtectionWidgetSnapshot(
                score: freshPass.protection.score,
                state: freshPass.protection.state,
                hostname: freshPass.hostname,
                generatedAt: freshPass.generatedAt,
                activePaths: freshPass.dataPaths.filter(\.enabled).count,
                totalPaths: freshPass.dataPaths.count,
                quarantines: freshPass.quarantine.active
            ).save()
            errorMessage = nil
        } catch is CancellationError {
        } catch {
            if recoveryPass == nil,
               let data = UserDefaults.standard.data(forKey: "offlineRecoveryPass"),
               let cached = try? JSONDecoder().decode(RecoveryPassResponse.self, from: data) {
                recoveryPass = cached
                errorMessage = "Offline-Stand vom \(AppFormat.date(cached.generatedAt))"
            } else {
                errorMessage = error.localizedDescription
            }
        }
    }

    private func exportPass() async {
        do {
            if model.isDemoMode, let recoveryPass {
                let target = FileManager.default.temporaryDirectory
                    .appendingPathComponent("recovery-pass-demo.json")
                let data = try JSONEncoder().encode(recoveryPass)
                try data.write(to: target, options: [.atomic, .completeFileProtection])
                exportURL = target
            } else {
                exportURL = try await model.withCurrentClient { try await $0.downloadRecoveryPass(includePaths: false) }
            }
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func loadDemo() {
        let now = Date().timeIntervalSince1970
        let paths = (model.storage?.pairs ?? []).map { pair in
            RecoveryDataPath(
                name: pair.name,
                direction: pair.direction,
                enabled: true,
                source: "***REDACTED***",
                target: "***REDACTED***",
                lastSyncAt: pair.lastSync,
                rpoSeconds: pair.lastSync.map { max(0, Int(now - $0)) },
                restore: RecoveryRestoreProof(
                    state: pair.restoreEvidence?.state ?? "passed",
                    lastAttemptAt: pair.restoreEvidence?.lastAttemptAt ?? now - 86_400,
                    lastSuccessAt: pair.restoreEvidence?.lastSuccessAt ?? now - 86_400,
                    jobID: pair.restoreEvidence?.jobID,
                    verifiedFiles: pair.restoreEvidence?.verifiedFiles ?? 20,
                    sampleSize: pair.restoreEvidence?.sampleSize ?? 20,
                    checksumVerified: pair.restoreEvidence?.checksumVerified ?? true,
                    error: pair.restoreEvidence?.error,
                    durationSeconds: 18
                )
            )
        }
        recoveryPass = RecoveryPassResponse(
            schema: "rclone-recovery-pass-v1",
            generatedAt: now,
            appVersion: model.overview?.app.version ?? "Demo",
            hostname: model.overview?.system.hostname ?? "demo-backup",
            protection: RecoveryProtectionScore(
                score: 92,
                state: "ready",
                components: [
                    RecoveryScoreComponent(key: "active", points: 15, maximum: 15),
                    RecoveryScoreComponent(key: "scheduled", points: 15, maximum: 15),
                    RecoveryScoreComponent(key: "freshness", points: 25, maximum: 25),
                    RecoveryScoreComponent(key: "restore", points: 25, maximum: 30),
                    RecoveryScoreComponent(key: "shield", points: 12, maximum: 15)
                ]
            ),
            dataPaths: paths,
            quarantine: RecoveryQuarantineResponse(active: 0, items: []),
            pathsIncluded: false
        )
        calendar = RecoveryCalendarResponse(
            days: [
                RecoveryCalendarDay(date: "2026-08-23", total: 1, successful: 1, failed: 0, cancelled: 0, restoreTests: 0, state: "ok"),
                RecoveryCalendarDay(date: "2026-08-24", total: 2, successful: 2, failed: 0, cancelled: 0, restoreTests: 1, state: "ok"),
                RecoveryCalendarDay(date: "2026-08-25", total: 1, successful: 1, failed: 0, cancelled: 0, restoreTests: 0, state: "ok"),
                RecoveryCalendarDay(date: "2026-08-26", total: 1, successful: 1, failed: 0, cancelled: 0, restoreTests: 0, state: "ok"),
                RecoveryCalendarDay(date: "2026-08-27", total: 1, successful: 1, failed: 0, cancelled: 0, restoreTests: 0, state: "ok")
            ],
            timezone: "Europe/Berlin"
        )
        policies = [
            RecoveryPolicyProfile(id: "family_photos", name: "Familienfotos", description: "Tägliche Kopie ohne automatische Löschungen und mit monatlichem Restore-Nachweis.", pair: [:], job: [:], restore: [:]),
            RecoveryPolicyProfile(id: "documents", name: "Dokumente", description: "Tägliche Sicherung mit Versionsablage und eng begrenzten Löschungen.", pair: [:], job: [:], restore: [:]),
            RecoveryPolicyProfile(id: "critical", name: "Kritische Daten", description: "Engmaschige Sicherung mit niedriger Löschgrenze und wöchentlicher Notfallübung.", pair: [:], job: [:], restore: [:])
        ]
        isLoading = false
        errorMessage = nil
    }

    private func scoreColor(_ score: Int) -> Color { score >= 85 ? .green : score >= 60 ? .orange : .red }
    private func calendarColor(_ state: String) -> Color { state == "ok" ? .green : state == "warning" ? .orange : state == "error" ? .red : .gray.opacity(0.25) }
    private func shortDay(_ date: String) -> String { String(date.suffix(5)).replacingOccurrences(of: "-", with: ".") }
    private func componentLabel(_ key: String) -> String {
        ["active": "Aktiv", "scheduled": "Plan", "freshness": "Frisch", "restore": "Restore", "shield": "Schutz"][key] ?? key
    }
    private func policySymbol(_ key: String) -> String {
        ["family_photos": "photo.on.rectangle.angled", "documents": "doc.text", "archive": "archivebox", "critical": "cross.case.fill"][key] ?? "shield"
    }
}

private struct RecoveryPathRow: View {
    let path: RecoveryDataPath

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: path.restore.state == "passed" ? "checkmark.shield.fill" : "arrow.counterclockwise.circle")
                .foregroundStyle(path.restore.state == "passed" ? .green : .orange)
                .frame(width: 28)
            VStack(alignment: .leading, spacing: 3) {
                Text(path.name).font(.headline)
                Text(path.rpoSeconds.map { "RPO aktuell: \(AppFormat.elapsed(Double($0)))" } ?? "Noch kein erfolgreicher Lauf")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            Text(path.restore.state == "passed" ? "Geprüft" : "Offen")
                .font(.caption.weight(.semibold))
                .foregroundStyle(path.restore.state == "passed" ? .green : .orange)
        }
    }
}

private struct RecoveryDataPathDetail: View {
    @EnvironmentObject private var model: AppModel
    let dataPath: RecoveryDataPath
    @State private var confirmDrill = false
    @State private var isStarting = false

    var body: some View {
        List {
            Section("Nachweis") {
                LabeledContent("RPO") {
                    Text(dataPath.rpoSeconds.map { AppFormat.elapsed(Double($0)) } ?? "Nicht belegt")
                        .foregroundStyle(dataPath.rpoSeconds == nil ? .orange : .primary)
                }
                LabeledContent("RTO-Stichprobe") {
                    Text(dataPath.restore.durationSeconds.map { AppFormat.elapsed($0) } ?? "Noch nicht gemessen")
                        .foregroundStyle(dataPath.restore.durationSeconds == nil ? .orange : .primary)
                }
                LabeledContent("Prüfsumme", value: dataPath.restore.checksumVerified ? "Bestätigt" : "Offen")
                if let date = dataPath.restore.lastSuccessAt {
                    LabeledContent("Letzter Restore", value: AppFormat.date(date))
                }
                if let error = dataPath.restore.error {
                    Label(error, systemImage: "exclamationmark.triangle.fill")
                        .font(.subheadline).foregroundStyle(.orange)
                }
            }
            Section {
                VStack(alignment: .leading, spacing: 10) {
                    drillStep(1, "Stichprobe am Sicherungsziel auswählen")
                    drillStep(2, "Getrennt in einen privaten Temp-Ordner holen")
                    drillStep(3, "Mit Prüfsummen gegen die Quelle bestätigen")
                    drillStep(4, "Temp-Daten automatisch vollständig löschen")
                }
                Button { confirmDrill = true } label: {
                    if isStarting { ProgressView() } else { Label("Notfallübung starten", systemImage: "figure.run.circle") }
                }
                .disabled(isStarting || model.isDemoMode)
            } header: {
                Text("Geführte Notfallübung")
            } footer: {
                Text("Die Übung verändert keine Produktivdaten. Cloud-Anbieter können für das Rückholen Egress berechnen.")
            }
            Section {
                NavigationLink { SelectiveRecoveryBrowser(dataPath: dataPath) } label: {
                    Label("Dateien ins Recovery-Staging holen", systemImage: "folder.badge.plus")
                }
                .disabled(model.isDemoMode)
            } header: {
                Text("Gezielte Wiederherstellung")
            } footer: {
                Text("Ausgewählte Dateien bleiben getrennt, bis du sie nach der Prüfung bewusst weiterverarbeitest.")
            }
        }
        .navigationTitle(dataPath.name)
        .navigationBarTitleDisplayMode(.inline)
        .confirmationDialog("Notfallübung für \(dataPath.name) starten?", isPresented: $confirmDrill) {
            Button("Übung starten") { Task { await startDrill() } }
            Button("Abbrechen", role: .cancel) {}
        }
    }

    private func drillStep(_ number: Int, _ text: String) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Text("\(number)").font(.caption.bold()).frame(width: 24, height: 24).background(.green.opacity(0.14), in: Circle())
            Text(text).font(.subheadline)
        }
    }

    private func startDrill() async {
        isStarting = true
        defer { isStarting = false }
        let ok = await model.runRestoreTest(pair: dataPath.name)
        if ok { model.actionMessage = "Notfallübung läuft. RPO und RTO werden nach Abschluss im Recovery-Pass aktualisiert." }
    }
}

private struct QuarantineDetailView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss
    let item: RecoveryQuarantineItem
    let onResolved: () async -> Void
    @State private var password = ""
    @State private var confirm = false
    @State private var isWorking = false
    @State private var errorMessage: String?

    var body: some View {
        Form {
            Section("Befund") {
                LabeledContent("Datenweg", value: item.pair)
                LabeledContent("Dateien", value: percent(item.fileDropPercent))
                LabeledContent("Datenmenge", value: percent(item.sizeDropPercent))
                Text("Der destruktive Lauf wurde vor dem Start gestoppt. Es wurden dadurch keine Zieldaten gelöscht.")
                    .font(.subheadline).foregroundStyle(.secondary)
            }
            Section {
                SecureField("Aktuelles Passwort", text: $password)
                    .textContentType(.password)
                Button("Quarantäne nach Prüfung aufheben", role: .destructive) { confirm = true }
                    .disabled(password.isEmpty || isWorking)
            } header: {
                Text("Bewusste Freigabe")
            } footer: {
                Text("Vorher Mount, Quellpfad und tatsächliche Dateimenge außerhalb der App prüfen.")
            }
            if let errorMessage { Text(errorMessage).foregroundStyle(.red) }
        }
        .navigationTitle("Sicherheitsstopp")
        .navigationBarTitleDisplayMode(.inline)
        .confirmationDialog("Quarantäne wirklich aufheben?", isPresented: $confirm) {
            Button("Nach externer Prüfung aufheben", role: .destructive) { Task { await acknowledge() } }
            Button("Abbrechen", role: .cancel) {}
        }
    }

    private func acknowledge() async {
        isWorking = true
        defer { isWorking = false }
        do {
            _ = try await model.withCurrentClient {
                try await $0.acknowledgeRecoveryQuarantine(identity: item.pair, currentPassword: password)
            }
            await onResolved()
            dismiss()
        } catch { errorMessage = error.localizedDescription }
    }

    private func percent(_ value: Double?) -> String { value.map { String(format: "−%.1f %%", $0) } ?? "–" }
}

private struct SelectiveRecoveryBrowser: View {
    @EnvironmentObject private var model: AppModel
    let dataPath: RecoveryDataPath
    @State private var path = ""
    @State private var items: [RecoveryBrowseItem] = []
    @State private var selection: Set<String> = []
    @State private var isLoading = false
    @State private var confirmRestore = false
    @State private var errorMessage: String?

    var body: some View {
        List {
            Section {
                Label("Wiederherstellung erfolgt nur in ein getrenntes Staging.", systemImage: "shield.lefthalf.filled")
                    .font(.subheadline).foregroundStyle(.green)
                if !path.isEmpty {
                    Button { navigateUp() } label: { Label("Übergeordnet", systemImage: "arrow.up") }
                }
            }
            Section(path.isEmpty ? "Sicherungsziel" : path) {
                if isLoading {
                    ProgressView()
                } else {
                    ForEach(items) { item in
                        if item.isDirectory {
                            Button { path = item.path; Task { await load() } } label: {
                                Label(item.name, systemImage: "folder.fill")
                            }
                            .foregroundStyle(.primary)
                        } else {
                            Button { toggle(item.path) } label: {
                                HStack {
                                    Label(item.name, systemImage: "doc")
                                    Spacer()
                                    if let size = item.size { Text(AppFormat.bytes(size)).font(.caption).foregroundStyle(.secondary) }
                                    Image(systemName: selection.contains(item.path) ? "checkmark.circle.fill" : "circle")
                                        .foregroundStyle(selection.contains(item.path) ? .green : .secondary)
                                }
                            }
                            .foregroundStyle(.primary)
                        }
                    }
                }
            }
            if let errorMessage { Text(errorMessage).foregroundStyle(.red) }
        }
        .navigationTitle(dataPath.name)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .confirmationAction) {
                Button("Wiederherstellen") { confirmRestore = true }
                    .disabled(selection.isEmpty)
            }
        }
        .task { await load() }
        .confirmationDialog("\(selection.count) Datei(en) ins Recovery-Staging holen?", isPresented: $confirmRestore) {
            Button("Getrennt wiederherstellen") { Task { await restore() } }
            Button("Abbrechen", role: .cancel) {}
        } message: {
            Text("Produktive Quell- und Zielpfade werden nicht verändert. Das Serverlimit beträgt 512 MB.")
        }
    }

    private func load() async {
        if model.isDemoMode {
            items = [
                RecoveryBrowseItem(name: "Dokumente", path: "Dokumente", isDirectory: true, size: nil, modifiedAt: nil),
                RecoveryBrowseItem(name: "Beispiel.pdf", path: "Beispiel.pdf", isDirectory: false, size: 245_760, modifiedAt: nil)
            ]
            return
        }
        isLoading = true
        defer { isLoading = false }
        do {
            items = try await model.withCurrentClient { try await $0.browseRecovery(identity: dataPath.name, path: path) }.items
            errorMessage = nil
        } catch { errorMessage = error.localizedDescription }
    }

    private func toggle(_ value: String) {
        if selection.contains(value) { selection.remove(value) } else if selection.count < 100 { selection.insert(value) }
    }

    private func navigateUp() {
        path = path.split(separator: "/").dropLast().joined(separator: "/")
        Task { await load() }
    }

    private func restore() async {
        guard !model.isDemoMode else { return }
        do {
            let response = try await model.withCurrentClient {
                try await $0.startSelectiveRestore(
                    SelectiveRestoreRequest(identity: dataPath.name, paths: selection.sorted(), maxTotalMB: 512)
                )
            }
            model.actionMessage = "Recovery #\(response.jobID) läuft. Das geprüfte Ergebnis erscheint unter Läufe."
            selection.removeAll()
        } catch { errorMessage = error.localizedDescription }
    }
}

private struct RecoveryHandoverView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var password = ""
    @State private var passphrase = ""
    @State private var confirmation = ""
    @State private var includePaths = false
    @State private var exportURL: URL?
    @State private var isWorking = false
    @State private var errorMessage: String?

    var body: some View {
        Form {
            Section {
                SecureField("Aktuelles Server-Passwort", text: $password)
                SecureField("Übergabe-Passphrase", text: $passphrase)
                SecureField("Passphrase wiederholen", text: $confirmation)
                Toggle("Serverpfade einschließen", isOn: $includePaths)
            } header: {
                Text("Autorisierung")
            } footer: {
                Text("Das Paket wird mit AES-256-GCM verschlüsselt. Die Passphrase getrennt übermitteln; sie ist nicht wiederherstellbar.")
            }
            Section {
                Button { Task { await create() } } label: {
                    if isWorking { ProgressView() } else { Label("Paket verschlüsseln", systemImage: "lock.doc") }
                }
                .disabled(password.isEmpty || passphrase.count < 12 || passphrase != confirmation || isWorking)
                if let exportURL {
                    ShareLink(item: exportURL) { Label("Übergabepaket teilen", systemImage: "square.and.arrow.up") }
                }
            }
            if let errorMessage { Text(errorMessage).foregroundStyle(.red) }
        }
        .navigationTitle("Sichere Übergabe")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Schließen") { dismiss() } } }
    }

    private func create() async {
        isWorking = true
        defer { isWorking = false }
        do {
            exportURL = try await model.withCurrentClient {
                try await $0.downloadRecoveryHandover(
                    RecoveryHandoverRequest(currentPassword: password, passphrase: passphrase, includePaths: includePaths)
                )
            }
            password = ""
            passphrase = ""
            confirmation = ""
            errorMessage = nil
        } catch { errorMessage = error.localizedDescription }
    }
}
