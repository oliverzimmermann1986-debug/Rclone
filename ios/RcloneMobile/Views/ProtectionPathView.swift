import SwiftUI
import Charts

struct RestoreEvidenceBadge: View {
    let evidence: RestoreEvidence?
    let isRunning: Bool

    var body: some View {
        Label(title, systemImage: symbol)
            .font(.caption2.weight(.semibold))
            .foregroundStyle(color)
            .labelStyle(.titleAndIcon)
    }

    private var title: String {
        if isRunning { return "Restore wird geprüft" }
        switch evidence?.state {
        case "passed": return "Restore geprüft"
        case "failed": return "Restore fehlgeschlagen"
        case "never": return "Restore offen"
        default: return "Nachweis unbekannt"
        }
    }

    private var symbol: String {
        if isRunning { return "arrow.clockwise.circle.fill" }
        switch evidence?.state {
        case "passed": return "checkmark.seal.fill"
        case "failed": return "xmark.octagon.fill"
        case "never": return "clock.badge.exclamationmark"
        default: return "questionmark.circle"
        }
    }

    private var color: Color {
        if isRunning { return .blue }
        switch evidence?.state {
        case "passed": return .green
        case "failed": return .red
        case "never": return .orange
        default: return .secondary
        }
    }
}

struct ProtectionPathDetailView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss
    let pair: StoragePair
    @State private var confirmRestore = false
    @State private var successFeedback = 0

    var body: some View {
        List {
            Section {
                HStack(spacing: 14) {
                    ZStack {
                        Circle().fill(evidenceColor.opacity(0.13))
                        Image(systemName: evidenceSymbol)
                            .font(.title2.weight(.semibold))
                            .foregroundStyle(evidenceColor)
                    }
                    .frame(width: 52, height: 52)
                    VStack(alignment: .leading, spacing: 3) {
                        Text("RESTORE-NACHWEIS")
                            .font(.caption2.bold())
                            .tracking(1.1)
                            .foregroundStyle(.secondary)
                        Text(evidenceTitle)
                            .font(.title3.weight(.semibold))
                        Text(evidenceSubtitle)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
                .padding(.vertical, 5)
                .accessibilityElement(children: .combine)
            }

            Section("Schutzpfad") {
                pathNode(
                    title: sourceIsLocal ? "Lokale Quelle" : "Cloud-Quelle",
                    path: pair.source,
                    symbol: sourceIsLocal ? "folder.fill" : "icloud.fill"
                )
                routeConnector
                pathNode(
                    title: targetIsLocal ? "Lokales Ziel" : "Cloud-Ziel",
                    path: pair.target,
                    symbol: targetIsLocal ? "folder.fill" : "icloud.fill"
                )
                if !assignedJobs.isEmpty {
                    Label(assignedJobs.joined(separator: " · "), systemImage: "calendar.badge.clock")
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                }
            }

            StorageStatisticsView(pair: pair)

            Section("Beleg") {
                LabeledContent("Letzte Prüfung", value: AppFormat.date(pair.restoreEvidence?.lastAttemptAt))
                LabeledContent("Letzter Erfolg", value: AppFormat.date(pair.restoreEvidence?.lastSuccessAt))
                LabeledContent(
                    "Prüfsumme",
                    value: pair.restoreEvidence?.checksumVerified == true ? "Bestätigt" : "Nicht bestätigt"
                )
                LabeledContent("Stichprobe", value: sampleDescription)
                if isRestoreTestRunning {
                    Label("Stichprobe wird zurückgeholt und geprüft.", systemImage: "hourglass")
                        .font(.subheadline)
                        .foregroundStyle(.blue)
                } else if let error = pair.restoreEvidence?.error, !error.isEmpty {
                    Label(error, systemImage: "exclamationmark.triangle.fill")
                        .font(.subheadline)
                        .foregroundStyle(.red)
                }
            }

            Section("Schutzschild") {
                Label(shieldTitle, systemImage: shieldSymbol)
                    .foregroundStyle(shieldColor)
                Text(shieldDetail)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }

            Section {
                Button { confirmRestore = true } label: {
                    Label(
                        isRestoreTestRunning
                            ? "Restore-Test läuft …"
                            : pair.restoreEvidence?.state == "passed" ? "Nachweis erneuern" : "Restore-Test starten",
                        systemImage: isRestoreTestRunning ? "hourglass" : "arrow.counterclockwise.circle.fill"
                    )
                }
                .disabled(isRestoreTestRunning)
            } footer: {
                Text("Der Test lädt eine begrenzte Stichprobe in ein temporäres Verzeichnis, vergleicht Prüfsummen und entfernt die Kopien anschließend.")
            }
        }
        .listStyle(.insetGrouped)
        .navigationTitle(pair.name)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .confirmationAction) { Button("Fertig") { dismiss() } }
        }
        .confirmationDialog("Wiederherstellbarkeit prüfen?", isPresented: $confirmRestore, titleVisibility: .visible) {
            Button("Restore-Test starten") {
                Task {
                    if await model.runRestoreTest(pair: pair.name) { successFeedback += 1 }
                }
            }
            Button("Abbrechen", role: .cancel) {}
        } message: {
            Text("Originaldateien werden nicht verändert. Der Server prüft eine sichere Stichprobe gegen die Quelle.")
        }
        .sensoryFeedback(.success, trigger: successFeedback)
    }

    private var sourceIsLocal: Bool { pair.source == pair.local }
    private var targetIsLocal: Bool { pair.target == pair.local }

    private var assignedJobs: [String] {
        guard let configuredPair = model.config?.backup.pairs.first(where: { $0.name == pair.name }) else { return [] }
        return model.jobDefinitions
            .filter { $0.dataPathIDs.contains(configuredPair.id) }
            .map(\.name)
    }

    private var configuredPair: PairConfig? {
        model.config?.backup.pairs.first(where: { $0.name == pair.name })
    }

    private var shieldTitle: String {
        guard let configuredPair else { return "Status nicht verfügbar" }
        let destructive = configuredPair.direction.lowercased() == "bisync" || configuredPair.mode.lowercased() == "sync"
        if !destructive { return "Keine automatischen Löschungen" }
        if !configuredPair.allowDelete { return "Produktive Löschungen gesperrt" }
        if configuredPair.maxDelete == nil { return "Löschlimit fehlt" }
        if configuredPair.backupDir.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { return "Löschungen begrenzt" }
        return "Begrenzt und versioniert"
    }

    private var shieldDetail: String {
        guard let configuredPair else { return "Konfiguration neu laden, um die Schutzregeln zu prüfen." }
        let destructive = configuredPair.direction.lowercased() == "bisync" || configuredPair.mode.lowercased() == "sync"
        if !destructive { return "Dieser Datenweg kopiert Dateien, ohne entfernte Quelldateien automatisch am Ziel zu löschen." }
        if !configuredPair.allowDelete { return "Der Server blockiert produktive Sync-Läufe, bis Löschungen ausdrücklich freigegeben werden." }
        guard let maximum = configuredPair.maxDelete else { return "Vor dem nächsten produktiven Lauf ein festes maximales Löschlimit setzen." }
        if configuredPair.backupDir.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return "Pro Lauf sind höchstens \(maximum) Löschungen erlaubt. Eine Versionsablage schützt zusätzlich vor Überschreiben."
        }
        return "Pro Lauf sind höchstens \(maximum) Löschungen erlaubt; ersetzte Dateien landen in der Versionsablage."
    }

    private var shieldColor: Color {
        guard let configuredPair else { return .secondary }
        let destructive = configuredPair.direction.lowercased() == "bisync" || configuredPair.mode.lowercased() == "sync"
        if !destructive || !configuredPair.allowDelete { return .green }
        return configuredPair.maxDelete == nil ? .red : configuredPair.backupDir.isEmpty ? .orange : .green
    }

    private var shieldSymbol: String {
        guard let configuredPair else { return "questionmark.circle" }
        let destructive = configuredPair.direction.lowercased() == "bisync" || configuredPair.mode.lowercased() == "sync"
        if destructive && configuredPair.allowDelete && configuredPair.maxDelete == nil {
            return "exclamationmark.shield.fill"
        }
        if destructive && configuredPair.allowDelete && configuredPair.backupDir.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return "shield.lefthalf.filled"
        }
        return "checkmark.shield.fill"
    }

    private var evidenceTitle: String {
        if isRestoreTestRunning { return "Prüfung läuft" }
        switch pair.restoreEvidence?.state {
        case "passed": return "Wiederherstellbar"
        case "failed": return "Prüfung fehlgeschlagen"
        case "never": return "Noch nicht nachgewiesen"
        default: return "Nachweis nicht verfügbar"
        }
    }

    private var evidenceSubtitle: String {
        if isRestoreTestRunning { return "Stichprobe wird zurückgeholt und per Prüfsumme verglichen." }
        if let success = pair.restoreEvidence?.lastSuccessAt {
            return "Zuletzt bestätigt \(AppFormat.relative(success))"
        }
        return "Starte einen Restore-Test für diesen Datenweg."
    }

    private var evidenceColor: Color {
        if isRestoreTestRunning { return .blue }
        switch pair.restoreEvidence?.state {
        case "passed": return .green
        case "failed": return .red
        default: return .orange
        }
    }

    private var evidenceSymbol: String {
        if isRestoreTestRunning { return "arrow.clockwise.circle.fill" }
        switch pair.restoreEvidence?.state {
        case "passed": return "checkmark.seal.fill"
        case "failed": return "xmark.octagon.fill"
        default: return "clock.badge.exclamationmark"
        }
    }

    private var sampleDescription: String {
        guard let verified = pair.restoreEvidence?.verifiedFiles,
              let sampled = pair.restoreEvidence?.sampleSize else { return "Noch kein Beleg" }
        return "\(verified) von \(sampled) Dateien"
    }

    private var isRestoreTestRunning: Bool {
        model.isRestoreTestRunning(for: pair.name)
    }

    private func pathNode(title: String, path: String, symbol: String) -> some View {
        HStack(spacing: 12) {
            Image(systemName: symbol)
                .foregroundStyle(.green)
                .frame(width: 28, height: 28)
                .background(.green.opacity(0.12), in: Circle())
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                Text(path).font(.body).lineLimit(2).truncationMode(.middle)
            }
        }
        .accessibilityElement(children: .combine)
    }

    private var routeConnector: some View {
        HStack(spacing: 12) {
            Capsule()
                .fill(.green.opacity(0.55))
                .frame(width: 3, height: 30)
                .frame(width: 28)
            VStack(alignment: .leading, spacing: 2) {
                Text("Sicherungsregel")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
                Text(pair.direction.uppercased())
                    .font(.caption.monospaced().weight(.medium))
            }
        }
        .accessibilityElement(children: .combine)
    }
}

private struct StorageStatisticsView: View {
    @EnvironmentObject private var model: AppModel
    let pair: StoragePair
    @State private var selectedSide = "source"
    @State private var composition: StorageCompositionResponse?
    @State private var isLoading = false
    @State private var errorMessage: String?

    private let categoryColors: [Color] = [.green, .blue, .orange, .purple, .teal, .gray, .brown]

    var body: some View {
        Section("Bestandsvergleich") {
            if comparison.isEmpty {
                Label("Dateizahlen und Größen werden noch ermittelt.", systemImage: "chart.bar.xaxis")
                    .foregroundStyle(.secondary)
            } else {
                Chart(comparison) { item in
                    BarMark(
                        x: .value("Größe", item.bytes),
                        y: .value("Speicherort", item.label)
                    )
                    .foregroundStyle(item.color.gradient)
                    .cornerRadius(4)
                }
                .chartXAxis(.hidden)
                .chartLegend(.hidden)
                .frame(height: 92)
                .accessibilityHidden(true)

                ForEach(comparison) { item in
                    LabeledContent {
                        Text("\(AppFormat.count(item.count)) · \(AppFormat.bytes(item.bytes))")
                    } label: {
                        Label(item.label, systemImage: item.symbol)
                            .foregroundStyle(item.color)
                    }
                    .accessibilityLabel("\(item.label), \(AppFormat.count(item.count)) Dateien, \(AppFormat.bytes(item.bytes))")
                }
            }
        }

        Section {
            Picker("Speicherort", selection: $selectedSide) {
                Text("Quelle").tag("source")
                Text("Ziel").tag("target")
            }
            .pickerStyle(.segmented)

            if isLoading && composition == nil {
                HStack(spacing: 12) {
                    ProgressView()
                    Text("Dateitypen werden analysiert …")
                        .foregroundStyle(.secondary)
                }
                .accessibilityElement(children: .combine)
            } else if let composition {
                compositionCharts(composition)
                measurementNote(composition)
            } else if let errorMessage {
                ContentUnavailableView {
                    Label("Statistik nicht verfügbar", systemImage: "chart.pie.fill")
                } description: {
                    Text(errorMessage)
                } actions: {
                    Button("Erneut versuchen") { Task { await loadComposition(forceRefresh: true) } }
                }
            }
        } header: {
            HStack {
                Text("Dateitypen")
                Spacer()
                if isLoading && composition != nil { ProgressView().controlSize(.small) }
                Button {
                    Task { await loadComposition(forceRefresh: true) }
                } label: {
                    Image(systemName: "arrow.clockwise")
                }
                .disabled(isLoading)
                .accessibilityLabel("Dateityp-Statistik neu messen")
            }
        } footer: {
            Text("Die Auswertung wird erst beim Öffnen geladen. Dateinamen bleiben auf dem Server; die App erhält nur zusammengefasste Typen, Anzahlen und Größen.")
        }
        .task(id: selectedSide) {
            composition = nil
            errorMessage = nil
            await loadComposition(forceRefresh: false)
        }
    }

    @ViewBuilder
    private func compositionCharts(_ value: StorageCompositionResponse) -> some View {
        let categories = (value.categories ?? []).filter { $0.count > 0 }
        let extensions = Array((value.extensions ?? []).filter { $0.count > 0 }.prefix(7))
        if categories.isEmpty {
            ContentUnavailableView(
                "Keine Dateien gefunden",
                systemImage: "doc",
                description: Text("Für diesen Speicherort konnten keine Dateitypen ermittelt werden.")
            )
        } else {
            ZStack {
                Chart(categories) { bucket in
                    SectorMark(
                        angle: .value("Anteil", chartValue(bucket)),
                        innerRadius: .ratio(0.62),
                        angularInset: 2
                    )
                    .cornerRadius(3)
                    .foregroundStyle(categoryColor(for: bucket.key))
                }
                .chartLegend(.hidden)
                .accessibilityHidden(true)
                VStack(spacing: 1) {
                    Text(AppFormat.count(value.count))
                        .font(.title3.weight(.semibold))
                    Text("Dateien")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
            }
            .frame(height: 205)

            ForEach(categories) { bucket in
                HStack(spacing: 9) {
                    Circle()
                        .fill(categoryColor(for: bucket.key))
                        .frame(width: 9, height: 9)
                    Text(bucket.label)
                    Spacer()
                    Text("\(AppFormat.count(bucket.count)) · \(AppFormat.bytes(bucket.bytes))")
                        .foregroundStyle(.secondary)
                }
                .font(.subheadline)
                .accessibilityElement(children: .combine)
            }

            if !extensions.isEmpty {
                Text("Größte Formate")
                    .font(.subheadline.weight(.semibold))
                    .padding(.top, 8)
                Chart(extensions) { bucket in
                    BarMark(
                        x: .value("Größe", bucket.bytes),
                        y: .value("Dateiendung", bucket.label)
                    )
                    .foregroundStyle(.green.gradient)
                    .cornerRadius(3)
                }
                .chartXAxis(.hidden)
                .frame(height: CGFloat(max(120, extensions.count * 28)))
                .accessibilityLabel(extensionAccessibility(extensions))
            }
        }
    }

    @ViewBuilder
    private func measurementNote(_ value: StorageCompositionResponse) -> some View {
        if value.truncated == true {
            Label("Sehr großer Bestand: Die Verteilung basiert auf den ersten \(AppFormat.count(value.count)) Dateien.", systemImage: "info.circle")
                .font(.caption)
                .foregroundStyle(.orange)
        } else if let error = value.error, !error.isEmpty {
            Label("Letzter nutzbarer Stand · \(error)", systemImage: "clock.badge.exclamationmark")
                .font(.caption)
                .foregroundStyle(.orange)
        } else {
            Label("Gemessen \(AppFormat.relative(value.measuredAt))", systemImage: "checkmark.circle")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    private var comparison: [EndpointComparison] {
        [
            EndpointComparison(label: "Quelle", symbol: sourceSymbol, color: .green, size: pair.sourceSize),
            EndpointComparison(label: "Ziel", symbol: targetSymbol, color: .blue, size: pair.targetSize)
        ].compactMap { $0.hasMeasurement ? $0 : nil }
    }

    private var sourceSymbol: String { pair.source == pair.local ? "folder.fill" : "icloud.fill" }
    private var targetSymbol: String { pair.target == pair.local ? "folder.fill" : "icloud.fill" }

    private func chartValue(_ bucket: StorageCompositionBucket) -> Double {
        bucket.bytes > 0 ? Double(bucket.bytes) : Double(bucket.count)
    }

    private func categoryColor(for key: String) -> Color {
        let keys = ["images", "videos", "documents", "audio", "archives", "other", "without_extension"]
        return categoryColors[(keys.firstIndex(of: key) ?? categoryColors.count - 1) % categoryColors.count]
    }

    private func extensionAccessibility(_ buckets: [StorageCompositionBucket]) -> String {
        buckets.map { "\($0.label): \(AppFormat.count($0.count)) Dateien, \(AppFormat.bytes($0.bytes))" }
            .joined(separator: "; ")
    }

    @MainActor
    private func loadComposition(forceRefresh: Bool) async {
        let requestedSide = selectedSide
        isLoading = true
        defer {
            if selectedSide == requestedSide { isLoading = false }
        }
        do {
            let loaded = try await model.storageComposition(
                for: pair,
                side: requestedSide,
                forceRefresh: forceRefresh
            )
            guard !Task.isCancelled, selectedSide == requestedSide else { return }
            if loaded.status == "failed" {
                composition = nil
                errorMessage = loaded.error ?? "Der Server konnte den Bestand nicht auswerten."
            } else {
                composition = loaded
                errorMessage = nil
            }
        } catch is CancellationError {
        } catch {
            guard !Task.isCancelled, selectedSide == requestedSide else { return }
            composition = nil
            errorMessage = error.localizedDescription
        }
    }
}

private struct EndpointComparison: Identifiable {
    let label: String
    let symbol: String
    let color: Color
    let size: PathSize?

    var id: String { label }
    var count: Int { size?.count ?? 0 }
    var bytes: Int64 { size?.bytes ?? 0 }
    var hasMeasurement: Bool { size?.count != nil || size?.bytes != nil }
}
