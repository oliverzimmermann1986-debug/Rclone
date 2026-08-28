import SwiftUI

struct ProtectionIncident: Identifiable {
    let id: String
    let severity: String
    let category: String
    let message: String
    let recommendation: String

    var color: Color { severity == "error" ? .red : .orange }
    var symbol: String { severity == "error" ? "exclamationmark.octagon.fill" : "exclamationmark.triangle.fill" }
}

extension ProtectionIncident {
    static func collect(overview: OverviewResponse, storage: StorageOverview?) -> [ProtectionIncident] {
        var seen = Set<String>()
        var incidents: [ProtectionIncident] = []

        func append(message: String, severity: String) {
            let normalized = message.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !normalized.isEmpty, seen.insert(normalized).inserted else { return }
            let classified = classify(normalized)
            incidents.append(ProtectionIncident(
                id: "\(severity):\(normalized)",
                severity: severity,
                category: classified.category,
                message: normalized,
                recommendation: classified.recommendation
            ))
        }

        for alert in overview.alerts where ["error", "warn", "warning"].contains(alert.level.lowercased()) {
            append(message: alert.message, severity: alert.level.lowercased() == "error" ? "error" : "warning")
        }
        for pair in overview.pairs.health {
            if let error = pair.error { append(message: "\(pair.name): \(error)", severity: "error") }
            if pair.overdue == true { append(message: "\(pair.name): Sicherung ist überfällig.", severity: "warning") }
        }
        for pair in storage?.pairs ?? [] where pair.restoreEvidence?.state == "failed" {
            append(
                message: "\(pair.name): \(pair.restoreEvidence?.error ?? "Restore-Nachweis fehlgeschlagen.")",
                severity: "error"
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
        if value.contains("überfällig") || value.contains("scheduler") || value.contains("zeitplan") {
            return ("Zeitplan", "Schedulerzustand und nächste Ausführung prüfen; bei Bedarf einen sicheren manuellen Lauf starten.")
        }
        return ("Betrieb", "Befund und zugehörigen Lauf öffnen. Vor einem produktiven Neustart einen Testlauf verwenden.")
    }
}

struct IncidentCenterView: View {
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
