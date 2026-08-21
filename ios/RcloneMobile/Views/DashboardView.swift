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
                            CopyListRow(pair: pair)
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
                } header: {
                    HStack {
                        Text("Kopien")
                        Spacer()
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

private struct CopyListRow: View {
    let pair: StoragePair

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
        guard let size else { return "Noch nicht gemessen" }
        switch size.measurementStatus {
        case "fresh":
            return "Gerade gemessen"
        case "cached":
            return "Gemessen \(AppFormat.relative(size.measuredAt))"
        case "stale":
            return "Veraltet · \(AppFormat.relative(size.measuredAt))"
        case "failed":
            return "Messung fehlgeschlagen"
        default:
            return size.measuredAt.map { "Gemessen \(AppFormat.relative($0))" } ?? "Noch nicht gemessen"
        }
    }

    private func measurementColor(_ size: PathSize?) -> Color {
        guard let status = size?.measurementStatus else { return .secondary }
        return ["stale", "failed"].contains(status) ? .orange : .secondary
    }
}
