import SwiftUI

enum RestoreIncidentReason: Equatable {
    case never, expired, configurationChanged, unbound, invalidTimestamp, partial, failed, unverified

    init(evidence: RestoreEvidence?) {
        guard let evidence else { self = .never; return }
        // An incomplete or failed attempt remains visible even if it also has a stale binding.
        if evidence.state == "failed" { self = .failed; return }
        if evidence.state == "partial" { self = .partial; return }
        switch evidence.invalidReason {
        case "expired": self = .expired
        case "configuration_changed": self = .configurationChanged
        case "unbound": self = .unbound
        case "invalid_timestamp": self = .invalidTimestamp
        default:
            if evidence.state == "never" {
                self = .never
            } else if let validUntil = evidence.validUntil,
                      validUntil.isFinite, validUntil > 0, validUntil <= Date().timeIntervalSince1970 {
                self = .expired
            } else {
                self = .unverified
            }
        }
    }

    var message: String {
        switch self {
        case .never: return "Noch kein Restore-Nachweis vorhanden. Eine Stichprobe prüfen."
        case .expired: return "Restore-Nachweis nach sieben Tagen abgelaufen. Eine neue Stichprobe prüfen."
        case .configurationChanged: return "Der Datenweg wurde geändert. Der bisherige Restore-Nachweis gilt nicht für die aktuelle Konfiguration."
        case .unbound: return "Der bisherige Restore-Nachweis ist keinem eindeutig bestätigten Datenweg zugeordnet. Erneut prüfen."
        case .invalidTimestamp: return "Der Zeitstempel des Restore-Nachweises ist ungültig. Serverzeit prüfen und danach erneut testen."
        case .partial: return "Die angeforderte Restore-Stichprobe wurde nicht vollständig geprüft."
        case .failed: return "Restore-Prüfung fehlgeschlagen. Prüfprotokoll öffnen."
        case .unverified: return "Kein aktuell gültiger Restore-Nachweis. Eine Stichprobe prüfen."
        }
    }

    var recommendation: String {
        let prerequisite = self == .invalidTimestamp ? "Zuerst die Serverzeit korrigieren. " : ""
        return prerequisite + "Für diesen Datenweg einen Restore-Test starten: Dateien werden getrennt in ein temporäres Prüfverzeichnis zurückgeholt und per Prüfsumme geprüft. Die Originale bleiben unverändert."
    }
}

struct ProtectionIncident: Identifiable {
    let id: String
    let severity: String
    let category: String
    let message: String
    let recommendation: String
    var pairName: String? = nil
    var jobID: Int? = nil
    var restoreReason: RestoreIncidentReason? = nil

    var restoreTestPairName: String? { restoreReason == nil ? nil : pairName }

    var color: Color { severity == "error" ? .red : .orange }
    var symbol: String { severity == "error" ? "exclamationmark.octagon.fill" : "exclamationmark.triangle.fill" }
}

extension ProtectionIncident {
    static func sanitizeMessage(_ raw: String) -> String {
        var cleaned = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cleaned.isEmpty else { return "" }
        cleaned = cleaned.replacingOccurrences(
            of: #":\s+(?=/\s)"#,
            with: " ",
            options: .regularExpression
        )
        cleaned = cleaned.replacingOccurrences(
            of: #":\s*(?=\))"#,
            with: "",
            options: .regularExpression
        )
        cleaned = cleaned.replacingOccurrences(
            of: #"(?<=\S):\s*$"#,
            with: "",
            options: .regularExpression
        )
        return cleaned.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private static func isGenericJobFailureSummaryText(_ message: String) -> Bool {
        let lowered = message.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        let prefixes = [
            "letzte sicherung ist fehlgeschlagen",
            "letzte sicherung wurde unterbrochen",
            "der letzte job ist fehlgeschlagen",
            "letzter job ist fehlgeschlagen",
            "letzter job wurde unterbrochen",
            "letzter testlauf (ohne änderungen) ist fehlgeschlagen",
            "letzter testlauf (ohne änderungen) wurde unterbrochen",
            "letzter vergleichslauf ist fehlgeschlagen",
            "letzter quick-sync ist fehlgeschlagen"
        ]
        return prefixes.contains { lowered.hasPrefix($0) }
    }

    private static func extractSummaryJobID(from message: String) -> Int? {
        guard isGenericJobFailureSummaryText(message),
              let regex = try? NSRegularExpression(pattern: #"\(Lauf\s*#(\d+)\)"#, options: [.caseInsensitive]),
              let match = regex.firstMatch(in: message, range: NSRange(message.startIndex..<message.endIndex, in: message)),
              let idRange = Range(match.range(at: 1), in: message) else {
            return nil
        }
        return Int(message[idRange])
    }

    static func collect(overview: OverviewResponse, storage: StorageOverview?) -> [ProtectionIncident] {
        var incidents: [ProtectionIncident] = []

        func append(message: String, severity: String, pairName: String? = nil, jobID: Int? = nil,
                    recommendation: String? = nil, category: String? = nil, restoreReason: RestoreIncidentReason? = nil) {
            let normalized = sanitizeMessage(message)
            guard !normalized.isEmpty else { return }
            let classified = classify(normalized)
            let incidentCategory = category ?? classified.category
            let exactMatches = incidents.indices.filter { position in
                let existing = incidents[position]
                return existing.message == normalized
                    && (existing.jobID == nil || jobID == nil || existing.jobID == jobID)
                    && (existing.pairName == nil || pairName == nil || existing.pairName == pairName)
            }
            let sameContext = exactMatches.first { position in
                incidents[position].jobID == jobID && incidents[position].pairName == pairName
            }
            // Missing context may be filled only when the matching finding is unambiguous.
            let exactPosition = sameContext ?? (exactMatches.count == 1 ? exactMatches.first : nil)
            var relatedPosition: Int?
            if severity == "warning", incidentCategory == "Prüfumfang", let jobID {
                let matches = incidents.indices.filter { position in
                    let existing = incidents[position]
                    return existing.severity == "warning" && existing.category == "Prüfumfang"
                        && existing.jobID == jobID
                        && ((existing.pairName == nil) != (pairName == nil))
                }
                // Only a global limited-sample summary and its one pair detail are equivalent.
                if matches.count == 1 { relatedPosition = matches.first }
            }
            if let position = exactPosition ?? relatedPosition {
                let previous = incidents[position]
                let useDetailedMessage = previous.pairName == nil && pairName != nil
                // A second source can add the missing navigation context or a more severe finding.
                incidents[position] = ProtectionIncident(
                    id: previous.id,
                    severity: previous.severity == "error" || severity == "error" ? "error" : "warning",
                    category: incidentCategory, message: useDetailedMessage ? normalized : previous.message,
                    recommendation: recommendation ?? previous.recommendation,
                    pairName: previous.pairName ?? pairName, jobID: previous.jobID ?? jobID,
                    restoreReason: restoreReason ?? previous.restoreReason
                )
                return
            }
            incidents.append(ProtectionIncident(
                id: "\(incidents.count):\(normalized)",
                severity: severity,
                category: incidentCategory,
                message: normalized,
                recommendation: recommendation ?? classified.recommendation,
                pairName: pairName,
                jobID: jobID,
                restoreReason: restoreReason
            ))
        }

        for alert in overview.alerts where ["error", "warn", "warning"].contains(alert.level.lowercased()) {
            var jobID = alert.jobID ?? extractSummaryJobID(from: alert.message)
            if jobID == nil,
               ["Der letzte Job ist fehlgeschlagen", "Der letzte Job ist fehlgeschlagen."].contains(
                   alert.message.trimmingCharacters(in: .whitespacesAndNewlines)),
               let last = overview.jobs.last, ["error", "failed", "timeout"].contains(last.status.lowercased()) {
                jobID = last.id
            }
            append(message: alert.message, severity: alert.level.lowercased() == "error" ? "error" : "warning", jobID: jobID)
        }
        for pair in overview.pairs.health {
            let error = sanitizeMessage(pair.error ?? "")
            if pair.hasFailure {
                let message = error.isEmpty
                    ? "Der letzte Lauf dieses Datenwegs ist fehlgeschlagen. Protokoll prüfen."
                    : error
                append(message: "\(pair.name): \(message)", severity: "error", pairName: pair.name, jobID: pair.jobID)
            }
            if pair.overdue == true { append(message: "\(pair.name): Sicherung ist überfällig.", severity: "warning", pairName: pair.name, jobID: pair.jobID) }
        }
        for pair in storage?.pairs ?? [] where pair.restoreEvidence?.isCurrent != true {
            let evidence = pair.restoreEvidence
            let reason = RestoreIncidentReason(evidence: evidence)
            if let scope = evidence?.sampleScope, scope.isPartial {
                append(message: "\(pair.name): \(scope.message)", severity: "warning",
                    pairName: pair.name, jobID: evidence?.jobID, recommendation: scope.recommendation,
                    category: "Prüfumfang", restoreReason: reason)
                continue
            }
            let error = sanitizeMessage(evidence?.error ?? "")
            let failed = reason == .failed
            let failureClassification = classify(error.isEmpty ? reason.message : error)
            let failureRecommendation = failureClassification.category == "Betrieb"
                ? "Restore-Protokoll öffnen und die Fehlerursache prüfen. Nach der Korrektur den Restore-Test für diesen Datenweg erneut starten."
                : failureClassification.recommendation
            append(
                message: "\(pair.name): \(failed && !error.isEmpty ? error : reason.message)",
                severity: failed ? "error" : "warning",
                pairName: pair.name, jobID: evidence?.jobID,
                recommendation: failed ? failureRecommendation : reason.recommendation,
                category: failed ? (failureClassification.category == "Betrieb" ? "Restore-Test" : failureClassification.category) : "Restore-Nachweis",
                restoreReason: reason
            )
        }

        let pairErrorJobIDs = Set(
            incidents.compactMap { $0.pairName != nil && $0.severity == "error" ? $0.jobID : nil }
        )
        incidents.removeAll { incident in
            incident.pairName == nil
                && incident.severity == "error"
                && isGenericJobFailureSummaryText(incident.message)
                && incident.jobID.map(pairErrorJobIDs.contains) == true
        }

        return incidents.sorted { ($0.severity == "error" ? 0 : 1) < ($1.severity == "error" ? 0 : 1) }
    }

    static func classify(_ message: String) -> (category: String, recommendation: String) {
        let value = message.lowercased()
        if value.contains("permission") || value.contains("berechtigung") || value.contains("access denied") {
            return ("Berechtigung", "Eigentümer und Zugriffsrechte des betroffenen Ordners auf dem Server prüfen. Danach zuerst einen Testlauf starten.")
        }
        if value.contains("timeout") || value.contains("stillstand") || value.contains("deadline") {
            return ("Zeitlimit", "Verbindung und Übertragungsrate prüfen. Einen Testlauf starten und das Laufzeitlimit nur bei echtem Fortschritt erhöhen.")
        }
        if value.contains("cleanup") || value.contains("aufräumen fehlgeschlagen") || value.contains("bereinigung fehlgeschlagen") {
            return ("Restore-Test", "Prüfprotokoll öffnen und die Bereinigung des temporären Prüfverzeichnisses auf dem Server kontrollieren. Erst nach der Korrektur erneut prüfen.")
        }
        if (value.contains("prüfumfang begrenzt") || value.contains("teil-stichprobe"))
            && !value.contains("fehlgeschlagen") && !value.contains("checksum") && !value.contains("prüfsumme") {
            return ("Prüfumfang", "Angeforderte Dateizahl beibehalten und Dateiauswahl sowie Prüfprotokoll prüfen. Das Datenlimit nur bewusst erhöhen, wenn es die Stichprobe begrenzt; danach erneut prüfen.")
        }
        if value.contains("api-limit") || value.contains("rate_limit") || value.contains("rate limit")
            || value.contains("too many requests") || value.contains("http 429") {
            return ("API-Limit", "Der Cloud-Anbieter drosselt Anfragen vorübergehend. Bei Google Drive eine eigene OAuth-Client-ID hinterlegen oder den Zeitplan entzerren, danach einen Probelauf starten.")
        }
        if value.contains("oauth") || value.contains("token") || value.contains("client_id")
            || value.contains("refresh_token") || value.contains("invalid_grant") {
            return ("Cloud-Anmeldung", "Cloud-Zugangstoken oder Client-ID ist ungültig oder abgelaufen. Das betroffene Remote unter „Neues Remote einbinden“ neu autorisieren und die Verbindung prüfen.")
        }
        if value.contains("exit=3") || value.contains("exit=4") || value.contains("directory not found") {
            return ("Zielpfad fehlt", "Der konfigurierte Ziel- oder Quellordner existiert auf dem Remote nicht. Pfad im Datenweg kontrollieren oder den Zielordner im Browser anlegen.")
        }
        if (value.contains("pre-check") || value.contains("precheck"))
            && value.contains("remote: ok") && value.contains("local:") {
            return ("Lokaler Quellpfad", "Die lokale Vorabprüfung hat den Lauf gestoppt. Lokalen Ordner, Mountpoint, Mindestdateizahl und Sentinel-Datei auf dem Server prüfen.")
        }
        if value.contains("pre-check") || value.contains("precheck") || value.contains("lsjson") {
            return ("Cloud-Vorabprüfung", "Das Cloud-Ziel hat die Vorabprüfung (lsjson) abgelehnt. Cloud-Anmeldung, Remote-Konfiguration und Erreichbarkeit prüfen und danach „Datenweg prüfen“ starten.")
        }
        if value.contains("exit=1") {
            return ("Remote-Zugriff", "rclone meldet einen Verbindungs-, Konfigurations- oder Zugriffsabbruch (exit=1). Prüfprotokoll des Laufs und Zugangsdaten kontrollieren, bevor ein Probelauf startet.")
        }
        if value.contains("mount") || value.contains("sentinel") || value.contains("directory") || value.contains("ordner") {
            return ("Speicherpfad", "Mount und Schutzdatei auf dem Server kontrollieren. Nicht blind neu starten, solange das Ziel fehlt.")
        }
        if value.contains("unauthorized") || value.contains("forbidden") || value.contains("auth") || value.contains("anmeldung") {
            return ("Anmeldung", "Server- oder Cloud-Anmeldung erneuern und anschließend die Verbindung testen.")
        }
        if value.contains("quota") || value.contains("no space") || value.contains("speicher") || value.contains("disk") {
            return ("Speicherplatz", "Freien Platz auf Quelle, Ziel und Anwendungsdatenträger prüfen.")
        }
        if value.contains("network") || value.contains("connection") || value.contains("not found") || value.contains("server") {
            return ("Verbindung", "Serveradresse, Port, TLS und Erreichbarkeit prüfen. Danach die Lage neu laden.")
        }
        if value.contains("rückholen fehlgeschlagen") || value.contains("rclone copy exit") || value.contains("restore fehlgeschlagen") || value.contains("restore-prüfung fehlgeschlagen") {
            return (
                "Restore-Test",
                "Restore-Protokoll öffnen und Sicherungsziel prüfen. Nach der Korrektur die Notfallübung erneut starten."
            )
        }
        if value.contains("prüfsumme") || value.contains("checksum") {
            return (
                "Datenintegrität",
                "Abweichende Datei im Restore-Protokoll prüfen und Quelle sowie Sicherung vor einem produktiven Lauf vergleichen."
            )
        }
        if value.contains("überfällig") || value.contains("scheduler") || value.contains("zeitplan") {
            return ("Zeitplan", "Schedulerzustand und nächste Ausführung prüfen; bei Bedarf einen sicheren manuellen Lauf starten.")
        }
        return ("Betrieb", "Befund und zugehörigen Lauf öffnen. Vor einem produktiven Neustart einen Testlauf verwenden.")
    }
}

struct IncidentCenterView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss
    let incidents: [ProtectionIncident]

    private var currentIncidents: [ProtectionIncident] {
        guard let overview = model.overview, let storage = model.storage else { return incidents }
        return ProtectionIncident.collect(overview: overview, storage: storage)
    }

    var body: some View {
        List {
            if currentIncidents.isEmpty {
                ContentUnavailableView(
                    "Keine offenen Vorfälle",
                    systemImage: "checkmark.shield.fill",
                    description: Text("Derzeit liegt kein klassifizierter Fehler oder überfälliger Datenweg vor.")
                )
            } else {
                ForEach(Array(currentIncidents.enumerated()), id: \.element.id) { index, incident in
                    Section {
                        VStack(alignment: .leading, spacing: 10) {
                            HStack(alignment: .firstTextBaseline) {
                                Label(incident.category, systemImage: incident.symbol)
                                    .font(.headline)
                                    .foregroundStyle(incident.color)
                                Spacer()
                                if let id = incident.jobID {
                                    Text("Lauf #\(id)")
                                        .font(.caption.monospacedDigit().weight(.medium))
                                        .foregroundStyle(.secondary)
                                }
                            }
                            Text(incident.message)
                                .font(.subheadline)
                                .textSelection(.enabled)
                            VStack(alignment: .leading, spacing: 4) {
                                Label("Empfohlener nächster Schritt", systemImage: "arrow.turn.down.right")
                                    .font(.caption.weight(.semibold))
                                    .foregroundStyle(.secondary)
                                Text(incident.recommendation)
                                    .font(.subheadline)
                                    .foregroundStyle(.primary)
                            }
                            .padding(10)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .background(
                                Color.secondary.opacity(0.08),
                                in: RoundedRectangle(cornerRadius: 10, style: .continuous)
                            )
                        }
                        .padding(.vertical, 4)
                        .accessibilityElement(children: .combine)

                        if let name = incident.restoreTestPairName {
                            IncidentRestoreTestButton(pairName: name)
                        }
                        if let name = incident.pairName {
                            NavigationLink {
                                IncidentDataPathView(pairName: name, isRestoreIncident: incident.restoreReason != nil)
                            } label: {
                                Label("Betroffenen Datenweg prüfen", systemImage: "point.3.connected.trianglepath.dotted")
                            }
                            .accessibilityLabel("Betroffenen Datenweg \(name) prüfen")
                        }
                        if let id = incident.jobID {
                            Button {
                                dismiss()
                                model.requestRunNavigation(id: id)
                            } label: {
                                Label("Zugehörigen Lauf öffnen", systemImage: "doc.text.magnifyingglass")
                            }
                            .accessibilityLabel("Zugehörigen Lauf Nummer \(id) öffnen")
                        }
                    } footer: {
                        if index == currentIncidents.count - 1 {
                            Text("Empfehlungen ändern keine Daten. Produktive Wiederholungen bleiben eine bewusste Nutzeraktion.")
                        }
                    }
                }
            }
        }
        .listStyle(.insetGrouped)
        .navigationTitle("Vorfallszentrale")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .confirmationAction) { Button("Fertig") { dismiss() } }
        }
    }
}

private struct IncidentRestoreTestButton: View {
    @EnvironmentObject private var model: AppModel
    let pairName: String
    @State private var confirm = false
    @State private var isStarting = false
    @State private var result: String?

    private var isRestoreTesting: Bool { isStarting || model.isRestoreTestRunning(for: pairName) }
    private var canStart: Bool {
        !isRestoreTesting && model.activeRestoreTestPairs.isEmpty && !model.isDemoMode
            && model.progress?.running != true && !model.batchIsRunning
            && model.config?.backup.pairs.contains(where: { $0.name == pairName }) == true
    }

    var body: some View {
        Button {
            confirm = true
        } label: {
            Label(
                isRestoreTesting ? "Restore-Test läuft …" : "Restore-Test starten",
                systemImage: "arrow.uturn.backward.circle"
            )
        }
        .disabled(!canStart)
        .accessibilityLabel(
            isRestoreTesting
                ? "Restore-Test für \(pairName) läuft"
                : "Restore-Test für \(pairName) starten"
        )
        .confirmationDialog("Restore-Test für \(pairName) starten?", isPresented: $confirm, titleVisibility: .visible) {
            Button("Restore-Test starten") {
                // Recheck after confirmation: another action may have started in the meantime.
                guard canStart else { return }
                isStarting = true
                Task {
                    defer { isStarting = false }
                    let ok = await model.runRestoreTest(pair: pairName)
                    result = ok
                        ? "Restore-Test gestartet. Der Nachweis wird nach Abschluss aktualisiert."
                        : "Restore-Test konnte nicht gestartet werden. Bitte die Fehlermeldung prüfen."
                }
            }
            Button("Abbrechen", role: .cancel) {}
        } message: {
            Text("Eine Stichprobe aus der Sicherung wird in ein temporäres Prüfverzeichnis zurückgeholt, per Prüfsumme geprüft und anschließend gelöscht. Die Originale bleiben unverändert.")
        }
        if let result { Text(result).font(.caption).foregroundStyle(.secondary) }
    }
}

private struct IncidentDataPathView: View {
    @EnvironmentObject private var model: AppModel
    let pairName: String
    let isRestoreIncident: Bool
    @State private var confirm = false
    @State private var busy = false
    @State private var result: String?
    @State private var showingSettings = false

    var body: some View {
        List {
            if let pair = model.config?.backup.pairs.first(where: { $0.name == pairName }) {
                Section("Betroffener Datenweg") {
                    LabeledContent("Lokal", value: pair.local)
                    LabeledContent("Cloud / Ziel", value: pair.remote)
                    LabeledContent("Verfahren", value: "\(pair.direction) / \(pair.mode)")
                    LabeledContent("Löschen erlaubt", value: pair.allowDelete ? "Ja" : "Nein")
                }
                Section {
                    if isRestoreIncident {
                        IncidentRestoreTestButton(pairName: pairName)
                    } else {
                        Button(busy ? "Startet …" : "Sicheren Probelauf starten") { confirm = true }.disabled(busy || model.isDemoMode)
                    }
                    NavigationLink("Restore-Nachweis und Notfallübung") { RecoveryCenterView() }
                    NavigationLink("Datenwege bearbeiten") { DataPathsScreen(showingSettings: $showingSettings) }
                    NavigationLink("Läufe & Protokolle") { RunsScreen(showingSettings: $showingSettings) }
                }
                if let result { Text(result) }
            } else {
                Text("Der Datenweg wurde entfernt oder umbenannt. Bitte die Übersicht aktualisieren.")
            }
        }
        .navigationTitle(pairName)
        .sheet(isPresented: $showingSettings) { SettingsView() }
        .confirmationDialog("Probelauf ohne Dateiänderungen starten?", isPresented: $confirm) {
            Button("Probelauf starten") {
                Task {
                    busy = true
                    let ok = await model.runBackup(pair: pairName, dryRun: true)
                    result = ok ? "Probelauf gestartet. Ergebnis unter Läufe & Protokolle öffnen." : "Start fehlgeschlagen. Bitte die Fehlermeldung prüfen."
                    busy = false
                }
            }
            Button("Abbrechen", role: .cancel) {}
        }
    }
}
