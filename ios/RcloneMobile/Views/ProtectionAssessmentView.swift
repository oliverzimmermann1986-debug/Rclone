import SwiftUI

struct ProtectionAssessment {
    struct Component: Identifiable {
        let id: String
        let title: String
        let points: Int
        let maximum: Int
        let detail: String

        var fraction: Double {
            guard maximum > 0 else { return 0 }
            return Double(points) / Double(maximum)
        }
    }

    let score: Int
    let components: [Component]

    init(overview: OverviewResponse, storage: StorageOverview?, config: ConfigSnapshot?) {
        let total = max(overview.pairs.total, 0)
        let enabled = max(overview.pairs.enabled, 0)

        let activePoints = Self.weighted(overview.pairs.enabled, of: total, maximum: 15)
        let scheduledPoints = Self.weighted(overview.pairs.scheduled, of: enabled, maximum: 15)

        let enabledHealth = overview.pairs.health.filter { health in
            config?.backup.pairs.first(where: { $0.name == health.name })?.enabled ?? true
        }
        let freshCount = enabledHealth.filter {
            $0.overdue != true && ["ok", "success", "successful"].contains($0.lastStatus?.lowercased() ?? "")
        }.count
        let freshPoints = Self.weighted(freshCount, of: enabled, maximum: 25)

        let passedRestores = storage?.pairs.filter { $0.restoreEvidence?.state == "passed" }.count ?? 0
        let restorePoints = Self.weighted(passedRestores, of: total, maximum: 30)

        let enabledPairs = config?.backup.pairs.filter { $0.enabled } ?? []
        let shieldUnits = enabledPairs.reduce(0.0) { partial, pair in
            partial + Self.shieldCoverage(for: pair)
        }
        let shieldPoints: Int
        if enabledPairs.isEmpty {
            shieldPoints = 0
        } else {
            shieldPoints = Int((shieldUnits / Double(enabledPairs.count) * 15).rounded())
        }

        let resolvedComponents = [
            Component(
                id: "active",
                title: "Aktive Datenwege",
                points: activePoints,
                maximum: 15,
                detail: total == 0 ? "Noch kein Datenweg eingerichtet." : "\(enabled) von \(total) Datenwegen sind aktiv."
            ),
            Component(
                id: "scheduled",
                title: "Automatisierung",
                points: scheduledPoints,
                maximum: 15,
                detail: enabled == 0 ? "Keine aktiven Datenwege." : "\(overview.pairs.scheduled) von \(enabled) aktiven Datenwegen sind eingeplant."
            ),
            Component(
                id: "freshness",
                title: "Frische erfolgreiche Läufe",
                points: freshPoints,
                maximum: 25,
                detail: enabled == 0 ? "Noch kein Lauf bewertbar." : "\(freshCount) von \(enabled) Datenwegen sind aktuell und ohne letzten Fehler."
            ),
            Component(
                id: "restore",
                title: "Restore-Nachweise",
                points: restorePoints,
                maximum: 30,
                detail: total == 0 ? "Noch kein Nachweis möglich." : "\(passedRestores) von \(total) Datenwegen wurden per Prüfsumme wiederhergestellt."
            ),
            Component(
                id: "shield",
                title: "Schutzschild",
                points: shieldPoints,
                maximum: 15,
                detail: Self.shieldDetail(for: enabledPairs)
            )
        ]
        components = resolvedComponents
        score = resolvedComponents.reduce(0) { $0 + $1.points }
    }

    private static func weighted(_ value: Int, of total: Int, maximum: Int) -> Int {
        guard total > 0 else { return 0 }
        let bounded = min(max(value, 0), total)
        return Int((Double(bounded) / Double(total) * Double(maximum)).rounded())
    }

    private static func shieldCoverage(for pair: PairConfig) -> Double {
        let destructive = pair.direction.lowercased() == "bisync" || pair.mode.lowercased() == "sync"
        guard destructive else { return 1 }
        guard pair.allowDelete else { return 1 }
        guard pair.maxDelete != nil else { return 0 }
        return pair.backupDir.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? 0.75 : 1
    }

    private static func shieldDetail(for pairs: [PairConfig]) -> String {
        guard !pairs.isEmpty else { return "Konfiguration noch nicht verfügbar." }
        let destructive = pairs.filter {
            $0.direction.lowercased() == "bisync" || $0.mode.lowercased() == "sync"
        }
        guard !destructive.isEmpty else { return "Alle aktiven Kopierwege arbeiten ohne automatische Löschungen." }
        let unbounded = destructive.filter { $0.allowDelete && $0.maxDelete == nil }
        let unversioned = destructive.filter {
            $0.allowDelete && $0.maxDelete != nil && $0.backupDir.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        }
        if !unbounded.isEmpty { return "\(unbounded.count) Löschpfad/-pfade besitzen noch kein festes Löschlimit." }
        if !unversioned.isEmpty { return "Löschungen sind begrenzt; \(unversioned.count) Pfad/-e haben noch keine Versionsablage." }
        return "Löschungen sind gesperrt oder durch Limit und Versionsablage abgesichert."
    }
}

struct ProtectionAssessmentView: View {
    @Environment(\.dismiss) private var dismiss
    let assessment: ProtectionAssessment

    var body: some View {
        List {
            Section {
                VStack(alignment: .leading, spacing: 8) {
                    Text("VERTRAUENSSCORE")
                        .font(.caption2.bold())
                        .tracking(1.1)
                        .foregroundStyle(.secondary)
                    Text("\(assessment.score) von 100")
                        .font(.largeTitle.bold())
                        .contentTransition(.numericText())
                    ProgressView(value: Double(assessment.score), total: 100)
                        .tint(scoreColor)
                    Text("Der Wert entsteht ausschließlich aus den unten aufgeführten, messbaren Nachweisen.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                .padding(.vertical, 8)
                .accessibilityElement(children: .combine)
            }

            Section("Zusammensetzung") {
                ForEach(assessment.components) { component in
                    VStack(alignment: .leading, spacing: 7) {
                        HStack {
                            Text(component.title).font(.body.weight(.medium))
                            Spacer()
                            Text("\(component.points)/\(component.maximum)")
                                .font(.subheadline.monospacedDigit().weight(.semibold))
                        }
                        ProgressView(value: component.fraction)
                            .tint(component.fraction >= 0.85 ? .green : component.fraction >= 0.5 ? .orange : .red)
                        Text(component.detail)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    .padding(.vertical, 4)
                    .accessibilityElement(children: .combine)
                }
            }
        }
        .listStyle(.insetGrouped)
        .navigationTitle("Schutznachweis")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .confirmationAction) { Button("Fertig") { dismiss() } }
        }
    }

    private var scoreColor: Color {
        if assessment.score >= 85 { return .green }
        if assessment.score >= 60 { return .orange }
        return .red
    }
}
