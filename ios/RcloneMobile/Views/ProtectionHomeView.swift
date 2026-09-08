import SwiftUI

struct ProtectionHomeView: View {
    var body: some View {
        List {
            Section {
                NavigationLink { DeviceVaultView() } label: {
                    Label { VStack(alignment: .leading, spacing: 4) {
                        Text("Fotos & Dateien sichern")
                        Text("Vom Gerät übertragen und die Zielkopie zurücklesen.")
                            .font(.caption).foregroundStyle(.secondary)
                    } } icon: { Image(systemName: "square.and.arrow.up") }
                }
            }
            Section("Schutz einrichten") {
                NavigationLink { ProtectionSetupView() } label: {
                    Label("Geführter Schutzassistent", systemImage: "checkmark.shield")
                }
                NavigationLink { RecoveryCenterView() } label: {
                    Label("Sicherungsbelege prüfen", systemImage: "checkmark.seal")
                }
            }
        }
        .navigationTitle("Sichern")
    }
}

struct ProtectionSetupView: View {
    @EnvironmentObject private var model: AppModel
    @State private var profiles: [RecoveryPolicyProfile] = []
    @State private var selectedProfile = "family_photos"
    @State private var selectedPair = ""
    @State private var baseConfig: ConfigSnapshot?
    @State private var password = ""
    @State private var snapshots = false
    @State private var confirm = false
    @State private var isWorking = false
    @State private var message: String?
    @State private var applied = false

    private var profile: RecoveryPolicyProfile? { profiles.first { $0.id == selectedProfile } }
    private var pair: PairConfig? { baseConfig?.backup.pairs.first { $0.id == selectedPair } }

    var body: some View {
        Form {
            Section("1 · Datenweg & Ziel") {
                Picker("Datenweg", selection: $selectedPair) {
                    Text("Bitte wählen").tag("")
                    ForEach(baseConfig?.backup.pairs.filter { $0.direction == "push" } ?? []) { pair in
                        Text(pair.name).tag(pair.id)
                    }
                }
                if let pair {
                    LabeledContent("Quelle", value: pair.local)
                    LabeledContent("Ziel", value: pair.remote)
                } else {
                    Text("Noch kein Push-Datenweg? Unter Mehr → Datenwege zuerst Quelle und Ziel anlegen.")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            Section("2 · Schutz auswählen") {
                Picker("Profil", selection: $selectedProfile) {
                    ForEach(profiles) { Text($0.name).tag($0.id) }
                }
                if let profile {
                    Text(profile.description).font(.subheadline)
                    ForEach(profile.pair.keys.sorted(), id: \.self) { key in
                        LabeledContent(settingLabel(key), value: settingValue(profile.pair[key]))
                    }
                    if let pair, baseConfig?.backup.jobs.contains(where: { $0.dataPathIDs.contains(pair.id) }) == true {
                        Text("Bestehende Job-Zuordnungen und Zeitpläne bleiben unverändert.").font(.caption)
                    } else {
                        LabeledContent("Neuer Job (Server-Zeitzone)", value: settingValue(profile.job["schedule"]))
                    }
                    Text("Restore-Empfehlung: \(settingValue(profile.restore["schedule"])). Der Assistent startet eine erste Stichprobe; automatische Restore-Termine werden dadurch nicht umgestellt.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Toggle("Vollständige Stände nach Sicherung", isOn: $snapshots)
                Text("Optional: zusätzliche vollständige Kopie auf dem Server, bis 5 GB je Stand. Benötigt freien Speicher; kein externer Schutz vor Serververlust. Größere Stände manuell in Wiederherstellen erstellen.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section("3 · Bewusst übernehmen") {
                SecureField("Aktuelles Server-Passwort", text: $password).textContentType(.password)
                Button(isWorking ? "Wird übernommen …" : "Einstellungen prüfen & übernehmen") { confirm = true }
                    .disabled(pair == nil || profile == nil || isWorking || model.isDemoMode || applied)
                Text("Verändert nur den gewählten Datenweg. Profile mit Sync können bei späteren Jobs Dateien am Ziel löschen. Mount und Schutzdatei müssen bereits existieren.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            if let message { Section { Text(message).textSelection(.enabled) } }
            if applied {
                Section("4 · Nachweis") {
                    NavigationLink("Prüfung und Sicherungsbeleg öffnen") { RecoveryCenterView() }
                    Text("Die erste Stichprobe prüft das vorhandene Sicherungsziel. Ein Erfolg belegt nur die angezeigten geprüften Dateien, keine Vollprüfung.").font(.caption)
                }
            }
        }
        .navigationTitle("Schutzassistent")
        .navigationBarTitleDisplayMode(.inline)
        .task {
            baseConfig = model.config
            do { profiles = try await model.withCurrentClient { try await $0.getRecoveryPolicies() }.profiles }
            catch { message = error.localizedDescription }
        }
        .confirmationDialog("Profil für \(pair?.name ?? "Datenweg") übernehmen?", isPresented: $confirm, titleVisibility: .visible) {
            Button("Übernehmen & erste Stichprobe starten", role: profile?.pair["allow_delete"] == .bool(true) ? .destructive : nil) {
                Task { await apply() }
            }
            Button("Abbrechen", role: .cancel) {}
        }
    }

    private func apply() async {
        guard let pair, let profile, let baseConfig else { return }
        isWorking = true
        defer { isWorking = false; password = "" }
        do {
            let updated = try ProtectionSetup.pair(pair, profile: profile, snapshots: snapshots)
            let pairs = baseConfig.backup.pairs.map { $0.id == pair.id ? updated : $0 }
            let jobs = ProtectionSetup.jobs(baseConfig.backup.jobs, pair: updated, profile: profile)
            guard await model.saveConfiguration(pairs: pairs, definitions: jobs,
                baseRevision: baseConfig.revision, currentPassword: password.isEmpty ? nil : password) else {
                switch model.configSaveIssue {
                case .conflict(let text), .passwordRequired(let text): message = text
                case .validation(let issues): message = issues.joined(separator: "\n")
                case nil: message = model.errorMessage ?? "Nicht gespeichert. Bitte erneut laden und prüfen."
                }
                return
            }
            applied = true
            let started = await model.runRestoreTest(pair: updated.name)
            message = started ? "Gespeichert. Die Stichprobe läuft; der Beleg erscheint nach Abschluss." : "Gespeichert. Die Stichprobe konnte nicht starten – bitte den Befund prüfen."
        } catch { message = error.localizedDescription }
    }

    private func settingLabel(_ key: String) -> String {
        ["mode": "Verfahren", "allow_delete": "Löschen erlaubt", "max_delete": "Löschgrenze",
         "backup_dir": "Versionsablage", "min_local_files": "Mindestdateien Quelle",
         "require_mountpoint": "Mount-Prüfung", "sentinel_file": "Schutzdatei"][key] ?? key
    }
    private func settingValue(_ value: JSONValue?) -> String {
        switch value {
        case .string(let text): return text
        case .number(let number): return String(Int(number))
        case .bool(let enabled): return enabled ? "Ja" : "Nein"
        default: return "–"
        }
    }
}
