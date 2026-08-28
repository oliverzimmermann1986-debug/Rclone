import SwiftUI

struct DashboardView: View {
    @EnvironmentObject private var model: AppModel
    @Binding var showingSettings: Bool
    @State private var confirmRunAll = false
    @State private var confirmCancel = false
    @State private var successFeedback = 0
    @State private var selectedProtectionPath: StoragePair?
    @State private var showingAssessment = false
    @State private var showingIncidents = false

    var body: some View {
        List {
            if let overview = model.overview {
                Section {
                    statusSummary(overview)
                }

                if model.progress?.running == true {
                    Section("Aktiver Lauf") {
                        liveProgress
                    }
                }

                if model.batchIsRunning || !model.batchDefinitions.isEmpty {
                    Section(model.batchIsRunning ? "Job-Batch läuft" : "Letzter Job-Batch") {
                        ForEach(model.batchDefinitions) { definition in
                            HStack {
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(definition.definitionName)
                                    if let jobID = definition.jobID {
                                        Text("Lauf #\(jobID)")
                                            .font(.caption)
                                            .foregroundStyle(.secondary)
                                    }
                                }
                                Spacer()
                                StatusBadge(status: definition.state)
                            }
                            .accessibilityElement(children: .combine)
                        }
                    }
                }

                if !protectionIncidents(for: overview).isEmpty {
                    Section("Vorfälle") {
                        Button { showingIncidents = true } label: {
                            IncidentCenterRow(incidents: protectionIncidents(for: overview))
                        }
                        .buttonStyle(.plain)
                    }
                }

                Section {
                    if let pairs = model.storage?.pairs, !pairs.isEmpty {
                        ForEach(pairs) { pair in
                            Button { selectedProtectionPath = pair } label: {
                                CopyListRow(pair: pair, isMeasuring: model.storageSizesAreLoading)
                            }
                            .buttonStyle(.plain)
                            .accessibilityHint("Öffnet Statistiken, Schutzpfad und Restore-Nachweis.")
                        }
                    } else if let configured = model.config?.backup.pairs, !configured.isEmpty {
                        ForEach(configured) { pair in
                            ConfiguredCopyListRow(pair: pair, isMeasuring: model.storageSizesAreLoading)
                        }
                        if case .failed = model.storageState {
                            Label("Dateizahlen und Größen sind vorübergehend nicht verfügbar.", systemImage: "info.circle")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                    } else {
                        switch model.storageState {
                        case .loaded:
                            ContentUnavailableView(
                                "Keine Datenwege",
                                systemImage: "arrow.left.arrow.right",
                                description: Text("Eingerichtete Kopien erscheinen hier.")
                            )
                        case let .failed(message):
                            LoadFailureView(title: "Kopien nicht geladen", message: message) {
                                Task { await model.refresh() }
                            }
                        default:
                            LoadingSection(label: "Kopien werden geladen …")
                        }
                    }
                    StorageMeasurementStateView(state: model.storageSizeState)
                } header: {
                    HStack {
                        Text("Kopien")
                        Spacer()
                        if model.storageSizesAreLoading {
                            ProgressView()
                                .controlSize(.small)
                                .accessibilityLabel("Dateizahlen und Größen werden gemessen")
                        } else {
                            Button {
                                Task { await model.refreshStorageSizes() }
                            } label: {
                                Label("Größen neu messen", systemImage: "arrow.clockwise")
                                    .labelStyle(.iconOnly)
                            }
                            .disabled(model.isRefreshing)
                            .accessibilityLabel("Dateizahlen und Größen neu messen")
                        }
                    }
                } footer: {
                    if model.storage?.pairs.isEmpty == false {
                        Text("Datenweg antippen, um Größenvergleich und Dateitypen zu sehen.")
                    }
                }

                if let last = overview.jobs.last {
                    Section("Letzter Lauf") {
                        NavigationLink { RunDetailView(job: last) } label: {
                            HStack(spacing: 12) {
                                Image(systemName: "clock.arrow.circlepath")
                                    .font(.title3)
                                    .foregroundStyle(StatusStyle.color(for: last.status))
                                    .frame(width: 28)
                                VStack(alignment: .leading, spacing: 3) {
                                    Text(AppFormat.date(last.startedAt))
                                        .font(.body.weight(.medium))
                                    Text("Lauf #\(last.id)")
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                }
                                Spacer()
                                StatusBadge(status: last.status)
                            }
                        }
                    }
                }
            } else {
                switch model.overviewState {
                case let .failed(message):
                    LoadFailureView(title: "Lage nicht geladen", message: message) {
                        Task { await model.refresh() }
                    }
                default:
                    LoadingSection(label: "Lage wird geladen …")
                }
            }
        }
        .listStyle(.insetGrouped)
        .navigationTitle("Lage")
        .toolbar {
            ToolbarItem(placement: .topBarLeading) {
                SettingsButton(showingSettings: $showingSettings)
            }
            ToolbarItem(placement: .topBarTrailing) {
                Button { confirmRunAll = true } label: {
                    Image(systemName: "play.fill")
                }
                .accessibilityLabel("Alle Jobs starten")
            }
        }
        .refreshable { await model.refresh() }
        .confirmationDialog("Alle Jobs jetzt starten?", isPresented: $confirmRunAll, titleVisibility: .visible) {
            Button("Sicherung starten") {
                Task { if await model.runAllJobDefinitions() { successFeedback += 1 } }
            }
            Button("Abbrechen", role: .cancel) {}
        } message: {
            Text("Alle aktivierten Jobs werden nach ihren Schutzregeln ausgeführt.")
        }
        .confirmationDialog("Lauf abbrechen?", isPresented: $confirmCancel, titleVisibility: .visible) {
            Button("Abbruch anfordern", role: .destructive) {
                Task { _ = await model.cancelBackup() }
            }
            Button("Weiterlaufen lassen", role: .cancel) {}
        } message: {
            Text("Bereits übertragene Dateien bleiben bestehen.")
        }
        .sensoryFeedback(.success, trigger: successFeedback)
        .sheet(item: $selectedProtectionPath) { pair in
            NavigationStack { ProtectionPathDetailView(pair: pair) }
                .presentationDetents([.medium, .large])
                .presentationDragIndicator(.visible)
        }
        .sheet(isPresented: $showingAssessment) {
            if let overview = model.overview {
                NavigationStack { ProtectionAssessmentView(assessment: assessment(for: overview)) }
                    .presentationDetents([.medium, .large])
                    .presentationDragIndicator(.visible)
            }
        }
        .sheet(isPresented: $showingIncidents) {
            if let overview = model.overview {
                NavigationStack { IncidentCenterView(incidents: protectionIncidents(for: overview)) }
                    .presentationDetents([.medium, .large])
                    .presentationDragIndicator(.visible)
            }
        }
        .task {
            while !Task.isCancelled {
                await model.refreshProgress()
                try? await Task.sleep(for: .seconds(5))
            }
        }
    }

    private func statusSummary(_ overview: OverviewResponse) -> some View {
        let level = protectionLevel(for: overview)
        let assessment = assessment(for: overview)
        return Button { showingAssessment = true } label: {
            ProtectionStatusCard(
                level: level,
                score: assessment.score,
                hostname: overview.system.hostname,
                generatedAt: overview.generatedAt,
                activePaths: overview.pairs.enabled,
                totalPaths: overview.pairs.total,
                scheduledPaths: overview.pairs.scheduled,
                restoreProof: restoreProofMetric,
                nextAction: nextProtectionAction(for: overview, level: level)
            )
        }
        .buttonStyle(.plain)
        .accessibilityHint("Öffnet die nachvollziehbare Zusammensetzung des Vertrauensscores.")
        .listRowInsets(EdgeInsets())
        .listRowBackground(Color.clear)
    }

    private func assessment(for overview: OverviewResponse) -> ProtectionAssessment {
        ProtectionAssessment(overview: overview, storage: model.storage, config: model.config)
    }

    private func protectionIncidents(for overview: OverviewResponse) -> [ProtectionIncident] {
        ProtectionIncident.collect(overview: overview, storage: model.storage)
    }

    private func protectionLevel(for overview: OverviewResponse) -> ProtectionLevel {
        if overview.alerts.contains(where: { $0.level.lowercased() == "error" }) { return .error }
        if model.storage?.pairs.contains(where: { $0.restoreEvidence?.state == "failed" }) == true { return .error }
        if overview.pairs.health.contains(where: { ["error", "failed", "timeout"].contains($0.lastStatus?.lowercased() ?? "") }) {
            return .error
        }
        if overview.alerts.contains(where: { ["warn", "warning"].contains($0.level.lowercased()) }) { return .warning }
        if overview.pairs.health.contains(where: { $0.overdue == true }) { return .warning }
        if model.storage?.pairs.contains(where: { $0.restoreEvidence?.state == "never" }) == true { return .warning }
        if overview.pairs.total == 0 || overview.pairs.scheduled == 0 || overview.jobs.lastSuccess == nil { return .warning }
        return .ok
    }

    private func nextProtectionAction(for overview: OverviewResponse, level: ProtectionLevel) -> String {
        if let alert = overview.alerts.first(where: { $0.level.lowercased() == "error" }) {
            return alert.message
        }
        if let pair = model.storage?.pairs.first(where: { $0.restoreEvidence?.state == "failed" }) {
            return "Restore-Nachweis für \(pair.name) fehlgeschlagen. Befund öffnen und erneut prüfen."
        }
        if let pair = overview.pairs.health.first(where: {
            ["error", "failed", "timeout"].contains($0.lastStatus?.lowercased() ?? "")
        }) {
            return "Datenweg \(pair.name) prüfen und den fehlgeschlagenen Lauf erneut starten."
        }
        if let alert = overview.alerts.first(where: { ["warn", "warning"].contains($0.level.lowercased()) }) {
            return alert.message
        }
        if let pair = overview.pairs.health.first(where: { $0.overdue == true }) {
            return "Datenweg \(pair.name) ist überfällig. Zeitplan oder Serverzustand prüfen."
        }
        if let pair = model.storage?.pairs.first(where: { $0.restoreEvidence?.state == "never" }) {
            return "Wiederherstellbarkeit von \(pair.name) erstmals mit einer Stichprobe nachweisen."
        }
        if overview.pairs.total == 0 { return "Ersten Datenweg zwischen Quelle und Ziel anlegen." }
        if overview.pairs.scheduled == 0 { return "Einen Job mit Zeitplan anlegen, damit der Schutz automatisch läuft." }
        if overview.jobs.lastSuccess == nil { return "Ersten Sicherungslauf starten und das Ergebnis verifizieren." }
        if level == .ok { return "Schutz aktiv. Die eingerichteten Datenwege werden planmäßig überwacht." }
        return "Hinweise prüfen, bevor der nächste Sicherungslauf startet."
    }

    private var restoreProofMetric: String {
        guard let pairs = model.storage?.pairs else { return "–" }
        let evidence = pairs.compactMap(\.restoreEvidence)
        guard !evidence.isEmpty else { return "–" }
        return "\(evidence.filter { $0.state == "passed" }.count)/\(evidence.count)"
    }

    private var liveProgress: some View {
        VStack(alignment: .leading, spacing: 13) {
            HStack {
                Label(
                    model.progressIsStale ? "Status veraltet" : "Sicherung läuft",
                    systemImage: model.progressIsStale ? "wifi.exclamationmark" : "arrow.triangle.2.circlepath"
                )
                    .font(.headline)
                    .foregroundStyle(model.progressIsStale ? .orange : .blue)
                Spacer()
                Text(AppFormat.elapsed(Double(model.progress?.elapsedSeconds ?? 0)))
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(.secondary)
            }

            if model.progressIsStale {
                Text("Der Server antwortet seit mehreren Prüfungen nicht. Die folgenden Werte stammen von der letzten erfolgreichen Abfrage\(progressLastCheckedSuffix).")
                    .font(.caption)
                    .foregroundStyle(.orange)
            }

            let total = max(model.progress?.totalPairs ?? 0, 1)
            let done = model.progress?.donePairs ?? 0
            ProgressView(value: Double(done), total: Double(total))
                .tint(model.progressIsStale ? .orange : .blue)

            ForEach(model.progress?.pairs ?? []) { pair in
                HStack {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(pair.name)
                            .font(.subheadline.weight(.semibold))
                        Text([pair.transferred, pair.speed].compactMap { $0 }.joined(separator: " · "))
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        if let lastProgress = pair.lastProgressAt {
                            Text("Letzter echter Fortschritt: \(AppFormat.relative(lastProgress))")
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                        }
                        if let stallTimeout = pair.stallTimeoutSeconds, stallTimeout > 0 {
                            Text("Stillstands-Watchdog: \(AppFormat.elapsed(Double(stallTimeout)))")
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                        }
                        if let maxRuntime = pair.maxRuntimeSeconds, maxRuntime > 0 {
                            Text("Maximale Laufzeit: \(AppFormat.elapsed(Double(maxRuntime)))")
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                        }
                    }
                    Spacer()
                    if let percent = pair.percent {
                        Text(percent / 100, format: .percent.precision(.fractionLength(0)))
                            .font(.caption.monospacedDigit())
                    } else {
                        StatusBadge(status: pair.status)
                    }
                }
            }

            Button("Lauf abbrechen", role: .destructive) {
                confirmCancel = true
            }
            .font(.subheadline.weight(.medium))
        }
        .padding(.vertical, 4)
    }

    private var progressLastCheckedSuffix: String {
        guard let date = model.progressLastSuccessAt else { return "" }
        return " (\(AppFormat.relative(date.timeIntervalSince1970)))"
    }
}

private enum ProtectionLevel: Equatable {
    case ok, warning, error

    var title: String {
        switch self {
        case .ok: "Bereit"
        case .warning: "Prüfen"
        case .error: "Handeln"
        }
    }

    var color: Color {
        switch self {
        case .ok: .green
        case .warning: .orange
        case .error: .red
        }
    }

    var symbol: String {
        switch self {
        case .ok: "checkmark"
        case .warning: "exclamationmark"
        case .error: "exclamationmark"
        }
    }
}

private struct ProtectionStatusCard: View {
    let level: ProtectionLevel
    let score: Int
    let hostname: String
    let generatedAt: Double
    let activePaths: Int
    let totalPaths: Int
    let scheduledPaths: Int
    let restoreProof: String
    let nextAction: String

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack(alignment: .center, spacing: 15) {
                DataPathSignatureMark(color: level.color)
                    .frame(width: 66, height: 66)
                    .accessibilityHidden(true)

                VStack(alignment: .leading, spacing: 4) {
                    Text("SCHUTZSTATUS")
                        .font(.caption2.weight(.bold))
                        .tracking(1.3)
                        .foregroundStyle(.secondary)
                    Text(level.title)
                        .font(.largeTitle.bold())
                        .foregroundStyle(level.color)
                    Text("\(hostname) · \(AppFormat.relative(generatedAt))")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
                Spacer(minLength: 0)
                VStack(alignment: .trailing, spacing: 1) {
                    Text("\(score)")
                        .font(.title2.monospacedDigit().bold())
                        .contentTransition(.numericText())
                    Text("VON 100")
                        .font(.caption2.bold())
                        .tracking(0.7)
                        .foregroundStyle(.secondary)
                }
            }

            HStack(spacing: 0) {
                protectionMetric(
                    value: "\(activePaths)/\(totalPaths)",
                    label: "Datenwege",
                    symbol: "point.3.connected.trianglepath.dotted"
                )
                Divider().frame(height: 38)
                protectionMetric(
                    value: "\(scheduledPaths)",
                    label: "Geplant",
                    symbol: "calendar.badge.clock"
                )
                Divider().frame(height: 38)
                protectionMetric(
                    value: restoreProof,
                    label: "Restore",
                    symbol: "arrow.counterclockwise.circle"
                )
            }

            HStack(alignment: .top, spacing: 10) {
                Image(systemName: level.symbol)
                    .font(.caption.bold())
                    .foregroundStyle(level.color)
                    .frame(width: 22, height: 22)
                    .background(level.color.opacity(0.14), in: Circle())
                VStack(alignment: .leading, spacing: 3) {
                    Text(level == .ok ? "Nächster Check" : "Nächster Schritt")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.secondary)
                    Text(nextAction)
                        .font(.subheadline.weight(.medium))
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .padding(20)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 26, style: .continuous))
        .overlay {
            RoundedRectangle(cornerRadius: 26, style: .continuous)
                .stroke(level.color.opacity(0.16), lineWidth: 1)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Schutzstatus \(level.title). Vertrauensscore \(score) von 100. \(activePaths) von \(totalPaths) Datenwegen aktiv. \(nextAction)")
    }

    private func protectionMetric(value: String, label: String, symbol: String) -> some View {
        VStack(spacing: 3) {
            Image(systemName: symbol)
                .font(.caption)
                .foregroundStyle(level.color)
            Text(value)
                .font(.subheadline.weight(.semibold))
                .lineLimit(1)
                .minimumScaleFactor(0.72)
            Text(label)
                .font(.caption2)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity)
    }
}

private struct IncidentCenterRow: View {
    let incidents: [ProtectionIncident]

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: hasErrors ? "exclamationmark.octagon.fill" : "exclamationmark.triangle.fill")
                .font(.title3)
                .foregroundStyle(hasErrors ? .red : .orange)
                .frame(width: 30)
            VStack(alignment: .leading, spacing: 3) {
                Text("Incident Center")
                    .font(.body.weight(.medium))
                Text(summary)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
            }
            Spacer()
            Text("\(incidents.count)")
                .font(.subheadline.monospacedDigit().weight(.semibold))
                .padding(.horizontal, 9)
                .padding(.vertical, 5)
                .background((hasErrors ? Color.red : Color.orange).opacity(0.13), in: Capsule())
            Image(systemName: "chevron.right")
                .font(.caption.bold())
                .foregroundStyle(.tertiary)
        }
        .accessibilityElement(children: .combine)
        .accessibilityHint("Öffnet Ursachen und sichere nächste Schritte.")
    }

    private var hasErrors: Bool { incidents.contains { $0.severity == "error" } }
    private var summary: String {
        guard let first = incidents.first else { return "Keine offenen Vorfälle" }
        return "\(first.category): \(first.message)"
    }
}

private struct DataPathSignatureMark: View {
    let color: Color

    var body: some View {
        ZStack {
            Circle()
                .fill(color.opacity(0.11))
            DataPathSignatureShape()
                .stroke(
                    .primary.opacity(0.82),
                    style: StrokeStyle(lineWidth: 5, lineCap: .round, lineJoin: .round)
                )
                .padding(12)
            Circle()
                .fill(color)
                .frame(width: 8, height: 8)
                .offset(x: 8, y: 4)
                .shadow(color: color.opacity(0.3), radius: 3)
        }
    }
}

private struct DataPathSignatureShape: Shape {
    func path(in rect: CGRect) -> Path {
        var path = Path()
        func point(_ x: CGFloat, _ y: CGFloat) -> CGPoint {
            CGPoint(x: rect.minX + rect.width * x, y: rect.minY + rect.height * y)
        }

        path.move(to: point(0.84, 0.16))
        path.addLine(to: point(0.47, 0.16))
        path.addCurve(
            to: point(0.47, 0.84),
            control1: point(0.10, 0.16),
            control2: point(0.10, 0.84)
        )
        path.addLine(to: point(0.72, 0.84))

        path.move(to: point(0.84, 0.34))
        path.addLine(to: point(0.52, 0.34))
        path.addCurve(
            to: point(0.52, 0.70),
            control1: point(0.29, 0.34),
            control2: point(0.29, 0.70)
        )
        path.addLine(to: point(0.66, 0.70))

        path.move(to: point(0.78, 0.48))
        path.addLine(to: point(0.58, 0.48))
        path.addCurve(
            to: point(0.58, 0.58),
            control1: point(0.48, 0.48),
            control2: point(0.48, 0.58)
        )
        path.addLine(to: point(0.62, 0.58))
        return path
    }
}

private struct ConfiguredCopyListRow: View {
    let pair: PairConfig
    let isMeasuring: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 11) {
            HStack {
                Text(pair.name).font(.headline)
                Spacer()
                Text(pair.enabled ? "Aktiv" : "Pausiert")
                    .font(.caption)
                    .foregroundStyle(pair.enabled ? .green : .secondary)
            }
            endpoint(symbol: "folder.fill", title: "Lokal", path: pair.local)
            endpoint(symbol: "icloud.fill", title: "Cloud", path: pair.remote)
        }
        .padding(.vertical, 5)
    }

    private func endpoint(symbol: String, title: String, path: String) -> some View {
        HStack(spacing: 10) {
            Image(systemName: symbol).foregroundStyle(.green).frame(width: 20)
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.caption.weight(.medium)).foregroundStyle(.secondary)
                Text(path).font(.subheadline).lineLimit(1).truncationMode(.middle)
            }
            Spacer(minLength: 8)
            VStack(alignment: .trailing, spacing: 2) {
                Text(isMeasuring ? "Dateien werden gezählt" : "– Dateien")
                    .font(.subheadline.weight(.semibold))
                Text(isMeasuring ? "Größe wird berechnet" : "– Größe")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .accessibilityElement(children: .combine)
    }
}

private struct CopyListRow: View {
    let pair: StoragePair
    let isMeasuring: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 11) {
            HStack {
                Text(pair.name)
                    .font(.headline)
                Spacer()
                VStack(alignment: .trailing, spacing: 3) {
                    Text(AppFormat.relative(pair.lastSync))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    RestoreEvidenceBadge(evidence: pair.restoreEvidence)
                }
            }

            endpoint(symbol: "folder.fill", title: "Lokal", path: pair.local, size: localSize)
            endpoint(symbol: "icloud.fill", title: "Cloud", path: pair.remote ?? "–", size: remoteSize)
        }
        .padding(.vertical, 5)
    }

    private var localSize: PathSize? {
        pair.source == pair.local ? pair.sourceSize : pair.targetSize
    }

    private var remoteSize: PathSize? {
        pair.source == pair.remote ? pair.sourceSize : pair.targetSize
    }

    private func endpoint(symbol: String, title: String, path: String, size: PathSize?) -> some View {
        HStack(spacing: 10) {
            Image(systemName: symbol)
                .foregroundStyle(.green)
                .frame(width: 20)

            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(.caption.weight(.medium))
                    .foregroundStyle(.secondary)
                Text(path)
                    .font(.subheadline)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }

            Spacer(minLength: 8)

            VStack(alignment: .trailing, spacing: 2) {
                Text(AppFormat.count(size?.count))
                    .font(.subheadline.weight(.semibold))
                Text(AppFormat.bytes(size?.bytes))
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Text(measurementLabel(size))
                    .font(.caption2)
                    .foregroundStyle(measurementColor(size))
            }
        }
        .accessibilityElement(children: .combine)
    }

    private func measurementLabel(_ size: PathSize?) -> String {
        guard let size else { return isMeasuring ? "Wird gemessen …" : "Noch nicht gemessen" }
        switch size.measurementStatus {
        case "fresh":
            return "Gerade gemessen"
        case "cached":
            return "Gemessen \(AppFormat.relative(size.measuredAt))"
        case "stale":
            if let issue = size.measurementError ?? size.error, !issue.isEmpty {
                return "Veraltet · \(issue)"
            }
            return "Veraltet · \(AppFormat.relative(size.measuredAt))"
        case "failed":
            return size.measurementError ?? size.error ?? "Messung fehlgeschlagen"
        default:
            return size.measuredAt.map { "Gemessen \(AppFormat.relative($0))" } ?? "Noch nicht gemessen"
        }
    }

    private func measurementColor(_ size: PathSize?) -> Color {
        guard let status = size?.measurementStatus else { return .secondary }
        return ["stale", "failed"].contains(status) ? .orange : .secondary
    }
}

private struct StorageMeasurementStateView: View {
    let state: StorageSizeState

    var body: some View {
        switch state.status {
        case .idle, .loaded:
            EmptyView()
        case .loading:
            Label("Dateizahlen und Größen werden im Hintergrund ermittelt.", systemImage: "hourglass")
                .font(.caption)
                .foregroundStyle(.secondary)
        case .partial, .failed, .stale:
            VStack(alignment: .leading, spacing: 3) {
                Label(title, systemImage: state.status == .failed ? "exclamationmark.triangle" : "clock.badge.exclamationmark")
                    .font(.caption.weight(.medium))
                    .foregroundStyle(.orange)
                if let message = state.message, !message.isEmpty {
                    Text(message)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
                if let lastUpdated = state.lastUpdated {
                    Text("Letzter nutzbarer Stand: \(AppFormat.relative(lastUpdated.timeIntervalSince1970))")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
            }
            .accessibilityElement(children: .combine)
        }
    }

    private var title: String {
        switch state.status {
        case .partial: "Nur ein Teil der Größen konnte gemessen werden."
        case .failed: "Dateizahlen und Größen konnten nicht gemessen werden."
        case .stale: "Die angezeigten Größen sind veraltet."
        default: ""
        }
    }
}
