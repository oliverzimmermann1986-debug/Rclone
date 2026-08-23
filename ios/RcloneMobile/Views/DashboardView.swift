import SwiftUI

struct DashboardView: View {
    @EnvironmentObject private var model: AppModel
    @Binding var showingSettings: Bool
    @State private var confirmRunAll = false
    @State private var confirmCancel = false
    @State private var successFeedback = 0

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

                if !overview.alerts.isEmpty {
                    Section("Hinweise") {
                        ForEach(overview.alerts) { alert in
                            Label {
                                Text(alert.message)
                            } icon: {
                                Image(systemName: alert.level == "error" ? "exclamationmark.octagon.fill" : "info.circle.fill")
                                    .foregroundStyle(StatusStyle.color(for: alert.level))
                            }
                            .font(.subheadline)
                        }
                    }
                }

                Section {
                    if let pairs = model.storage?.pairs, !pairs.isEmpty {
                        ForEach(pairs) { pair in
                            CopyListRow(pair: pair, isMeasuring: model.storageSizesAreLoading)
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
        .task {
            while !Task.isCancelled {
                await model.refreshProgress()
                try? await Task.sleep(for: .seconds(5))
            }
        }
    }

    private func statusSummary(_ overview: OverviewResponse) -> some View {
        let level = aggregateLevel(for: overview.alerts)
        let color: Color = level == .error ? .red : level == .warning ? .orange : .green
        let symbol = level == .error ? "exclamationmark" : level == .warning ? "exclamationmark.triangle" : "checkmark"
        let title = level == .error ? "Aufmerksamkeit nötig" : level == .warning ? "Hinweise vorhanden" : "Alles in Ordnung"
        return HStack(spacing: 14) {
            ZStack {
                Circle()
                    .fill(color.opacity(0.14))
                Image(systemName: symbol)
                    .font(.title2.bold())
                    .foregroundStyle(color)
            }
            .frame(width: 50, height: 50)
            .accessibilityHidden(true)

            VStack(alignment: .leading, spacing: 4) {
                Text(title)
                    .font(.title3.weight(.semibold))
                Text("\(overview.system.hostname) · \(AppFormat.relative(overview.generatedAt))")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 5)
        .accessibilityElement(children: .combine)
    }

    private enum AggregateLevel: Equatable { case ok, warning, error }

    private func aggregateLevel(for alerts: [SystemAlert]) -> AggregateLevel {
        if alerts.contains(where: { $0.level.lowercased() == "error" }) { return .error }
        if alerts.contains(where: { ["warn", "warning"].contains($0.level.lowercased()) }) { return .warning }
        return .ok
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
                Text(AppFormat.relative(pair.lastSync))
                    .font(.caption)
                    .foregroundStyle(.secondary)
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
