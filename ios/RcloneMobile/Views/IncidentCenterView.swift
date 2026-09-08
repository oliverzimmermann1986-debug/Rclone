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
        var seen = Set<String>()
        var incidents: [ProtectionIncident] = []

        func append(message: String, severity: String, pairName: String? = nil, jobID: Int? = nil) {
            let normalized = message.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !normalized.isEmpty, seen.insert(normalized).inserted else { return }
            let classified = classify(normalized)
            incidents.append(ProtectionIncident(
                id: "\(severity):\(normalized)",
                severity: severity,
                category: classified.category,
                message: normalized,
                recommendation: classified.recommendation,
                pairName: pairName,
                jobID: jobID
            ))
        }

        for alert in overview.alerts where ["error", "warn", "warning"].contains(alert.level.lowercased()) {
            append(message: alert.message, severity: alert.level.lowercased() == "error" ? "error" : "warning")
        }
        for pair in overview.pairs.health {
            if let error = pair.error, !error.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                append(message: "\(pair.name): \(error)", severity: "error", pairName: pair.name, jobID: pair.jobID)
            }
            if pair.overdue == true { append(message: "\(pair.name): Sicherung ist überfällig.", severity: "warning", pairName: pair.name, jobID: pair.jobID) }
        }
        for pair in storage?.pairs ?? [] where pair.restoreEvidence?.isCurrent != true {
            append(
                message: "\(pair.name): \(pair.restoreEvidence?.error ?? "Kein aktuell gültiger Restore-Nachweis. Stichprobe prüfen.")",
                severity: pair.restoreEvidence?.state == "failed" ? "error" : "warning",
                pairName: pair.name, jobID: pair.restoreEvidence?.jobID
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
        if value.contains("rückholen fehlgeschlagen") || value.contains("rclone copy exit") || value.contains("restore fehlgeschlagen") {
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
