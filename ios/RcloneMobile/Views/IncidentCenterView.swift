import SwiftUI

struct ProtectionIncident: Identifiable {
    let id: String
    let severity: String
    let category: String
    let message: String
    let recommendation: String
    var pairName: String? = nil
    var jobID: Int? = nil

    var color: Color { severity == "error" ? .red : .orange }
    var symbol: String { severity == "error" ? "exclamationmark.octagon.fill" : "exclamationmark.triangle.fill" }
}

extension ProtectionIncident {
    static func collect(overview: OverviewResponse, storage: StorageOverview?) -> [ProtectionIncident] {
        var incidents: [ProtectionIncident] = []

        func append(message: String, severity: String, pairName: String? = nil, jobID: Int? = nil, recommendation: String? = nil) {
            let normalized = message.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !normalized.isEmpty else { return }
            let classified = classify(normalized)
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
            if severity == "warning", classified.category == "Prüfumfang", let jobID {
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
                    category: classified.category, message: useDetailedMessage ? normalized : previous.message,
                    recommendation: recommendation ?? previous.recommendation,
                    pairName: previous.pairName ?? pairName, jobID: previous.jobID ?? jobID
                )
                return
            }
            incidents.append(ProtectionIncident(
                id: "\(incidents.count):\(normalized)",
                severity: severity,
                category: classified.category,
                message: normalized,
                recommendation: recommendation ?? classified.recommendation,
                pairName: pairName,
                jobID: jobID
            ))
        }

        for alert in overview.alerts where ["error", "warn", "warning"].contains(alert.level.lowercased()) {
            var jobID = alert.jobID
            if jobID == nil,
               ["Der letzte Job ist fehlgeschlagen", "Der letzte Job ist fehlgeschlagen."].contains(
                   alert.message.trimmingCharacters(in: .whitespacesAndNewlines)),
               let last = overview.jobs.last, ["error", "failed", "timeout"].contains(last.status.lowercased()) {
                jobID = last.id
            }
            append(message: alert.message, severity: alert.level.lowercased() == "error" ? "error" : "warning", jobID: jobID)
        }
        for pair in overview.pairs.health {
            let error = pair.error?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            let status = pair.lastStatus?.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() ?? ""
            let failed = ["error", "failed", "timeout", "stale"].contains(status)
            if failed || (!["ok", "success", "successful", "passed", "cancelled"].contains(status) && !error.isEmpty) {
                let message = error.isEmpty
                    ? "Der letzte Lauf dieses Datenwegs ist fehlgeschlagen. Protokoll prüfen."
                    : error
                append(message: "\(pair.name): \(message)", severity: "error", pairName: pair.name, jobID: pair.jobID)
            }
            if pair.overdue == true { append(message: "\(pair.name): Sicherung ist überfällig.", severity: "warning", pairName: pair.name, jobID: pair.jobID) }
        }
        for pair in storage?.pairs ?? [] where pair.restoreEvidence?.isCurrent != true {
            let evidence = pair.restoreEvidence
            if let scope = evidence?.sampleScope, scope.isPartial {
                append(message: "\(pair.name): \(scope.message)", severity: "warning",
                    pairName: pair.name, jobID: evidence?.jobID, recommendation: scope.recommendation)
                continue
            }
            let error = evidence?.error?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            let fallback = evidence?.state == "failed"
                ? "Restore-Prüfung fehlgeschlagen. Prüfprotokoll öffnen."
                : "Kein aktuell gültiger Restore-Nachweis. Stichprobe prüfen."
            append(
                message: "\(pair.name): \(error.isEmpty ? fallback : error)",
                severity: evidence?.state == "failed" ? "error" : "warning",
                pairName: pair.name, jobID: evidence?.jobID
            )
        }
        return incidents.sorted { ($0.severity == "error" ? 0 : 1) < ($1.severity == "error" ? 0 : 1) }
    }

    private static func classify(_ message: String) -> (category: String, recommendation: String) {
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

    var body: some View {
        List {
            if incidents.isEmpty {
                ContentUnavailableView(
                    "Keine offenen Vorfälle",
                    systemImage: "checkmark.shield.fill",
                    description: Text("Derzeit liegt kein klassifizierter Fehler oder überfälliger Datenweg vor.")
                )
            } else {
                Section {
                    ForEach(incidents) { incident in
                        VStack(alignment: .leading, spacing: 9) {
                            Label(incident.category, systemImage: incident.symbol)
                                .font(.headline)
                                .foregroundStyle(incident.color)
                            Text(incident.message)
                                .font(.subheadline)
                                .textSelection(.enabled)
                            Divider()
                            Label("Empfohlener nächster Schritt", systemImage: "arrow.turn.down.right")
                                .font(.caption.weight(.semibold))
                                .foregroundStyle(.secondary)
                            Text(incident.recommendation)
                                .font(.subheadline)
                            if let name = incident.pairName {
                                NavigationLink("Betroffenen Datenweg prüfen") { IncidentDataPathView(pairName: name) }
                            }
                            if let id = incident.jobID {
                                Button("Zugehörigen Lauf öffnen") {
                                    dismiss()
                                    model.requestRunNavigation(id: id)
                                }
                            }
                        }
                        .padding(.vertical, 6)
                        .accessibilityElement(children: .combine)
                    }
                } footer: {
                    Text("Empfehlungen ändern keine Daten. Produktive Wiederholungen bleiben eine bewusste Nutzeraktion.")
                }
            }
        }
        .listStyle(.insetGrouped)
        .navigationTitle("Incident Center")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .confirmationAction) { Button("Fertig") { dismiss() } }
        }
    }
}

private struct IncidentDataPathView: View {
    @EnvironmentObject private var model: AppModel
    let pairName: String
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
                    Button(busy ? "Startet …" : "Sicheren Probelauf starten") { confirm = true }.disabled(busy || model.isDemoMode)
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
