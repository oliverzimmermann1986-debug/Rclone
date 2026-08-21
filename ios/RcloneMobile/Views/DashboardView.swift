import SwiftUI

struct DashboardView: View {
    @EnvironmentObject private var model: AppModel
    @Binding var showingSettings: Bool
    @State private var confirmRunAll = false
    @State private var confirmCancel = false
    @State private var successFeedback = 0

    var body: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 22) {
                if let error = model.errorMessage {
                    ErrorBanner(message: error, dismiss: model.dismissMessages)
                }
                if let overview = model.overview {
                    statusHeader(overview)
                    if model.progress?.running == true { liveProgress }
                    alerts(overview.alerts)
                    metrics(overview)
                    copyOverview
                    lastRun(overview.jobs.last)
                } else {
                    LoadingSection(label: "Lagebild wird geladen …")
                }
            }
            .padding(16)
        }
        .background(Color(.systemGroupedBackground))
        .navigationTitle("Lagebild")
        .toolbar {
            ToolbarItem(placement: .topBarLeading) { SettingsButton(showingSettings: $showingSettings) }
            ToolbarItem(placement: .topBarTrailing) {
                Button { confirmRunAll = true } label: { Label("Alle sichern", systemImage: "play.fill") }
            }
        }
        .refreshable { await model.refresh() }
        .confirmationDialog("Alle Datenwege jetzt sichern?", isPresented: $confirmRunAll, titleVisibility: .visible) {
            Button("Sicherung starten") {
                Task { if await model.runBackup() { successFeedback += 1 } }
            }
            Button("Abbrechen", role: .cancel) {}
        } message: {
            Text("Es werden alle aktivierten Datenwege nach den hinterlegten Schutzregeln ausgeführt.")
        }
        .confirmationDialog("Lauf abbrechen?", isPresented: $confirmCancel, titleVisibility: .visible) {
            Button("Abbruch anfordern", role: .destructive) { Task { _ = await model.cancelBackup() } }
            Button("Weiterlaufen lassen", role: .cancel) {}
        } message: {
            Text("Bereits übertragene Dateien bleiben bestehen. Der aktuelle rclone-Prozess wird kontrolliert beendet.")
        }
        .sensoryFeedback(.success, trigger: successFeedback)
        .task {
            while !Task.isCancelled {
                await model.refreshProgress()
                try? await Task.sleep(for: .seconds(5))
            }
        }
    }

    private var liveProgress: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Label("Sicherung läuft", systemImage: "arrow.triangle.2.circlepath")
                    .font(.headline).foregroundStyle(.blue)
                Spacer()
                Text(AppFormat.elapsed(Double(model.progress?.elapsedSeconds ?? 0)))
                    .font(.caption.monospacedDigit()).foregroundStyle(.secondary)
            }
            let total = max(model.progress?.totalPairs ?? 0, 1)
            let done = model.progress?.donePairs ?? 0
            ProgressView(value: Double(done), total: Double(total)).tint(.blue)
            ForEach(model.progress?.pairs ?? []) { pair in
                HStack {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(pair.name).font(.subheadline.weight(.semibold))
                        Text([pair.transferred, pair.speed].compactMap { $0 }.joined(separator: " · "))
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    if let percent = pair.percent {
                        Text(percent / 100, format: .percent.precision(.fractionLength(0))).font(.caption.monospacedDigit())
                    } else {
                        StatusBadge(status: pair.status)
                    }
                }
            }
            Button(role: .destructive) { confirmCancel = true } label: { Label("Lauf abbrechen", systemImage: "stop.circle") }
                .buttonStyle(.bordered)
        }
        .padding(16)
        .background(.blue.opacity(0.09), in: RoundedRectangle(cornerRadius: 20, style: .continuous))
    }

    private func statusHeader(_ overview: OverviewResponse) -> some View {
        HStack(alignment: .top, spacing: 14) {
            ZStack {
                Circle().fill((overview.alerts.contains { $0.level == "error" } ? Color.red : Color.green).opacity(0.14))
                Image(systemName: overview.alerts.contains { $0.level == "error" } ? "exclamationmark" : "checkmark")
                    .font(.title2.bold())
                    .foregroundStyle(overview.alerts.contains { $0.level == "error" } ? Color.red : Color.green)
            }
            .frame(width: 52, height: 52)
            VStack(alignment: .leading, spacing: 5) {
                Text(overview.alerts.contains { $0.level == "error" } ? "Aufmerksamkeit nötig" : "Alles im Blick")
                    .font(.title2.bold())
                Text("\(overview.system.hostname) · aktualisiert \(AppFormat.relative(overview.generatedAt))")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }
        }
    }

    @ViewBuilder
    private func alerts(_ alerts: [SystemAlert]) -> some View {
        if !alerts.isEmpty {
            VStack(spacing: 9) {
                ForEach(alerts) { alert in
                    HStack(alignment: .top, spacing: 10) {
                        Image(systemName: alert.level == "error" ? "exclamationmark.octagon.fill" : "info.circle.fill")
                            .foregroundStyle(StatusStyle.color(for: alert.level))
                        Text(alert.message).font(.subheadline).frame(maxWidth: .infinity, alignment: .leading)
                    }
                    .padding(13)
                    .background(StatusStyle.color(for: alert.level).opacity(0.1), in: RoundedRectangle(cornerRadius: 14))
                }
            }
        }
    }

    private func metrics(_ overview: OverviewResponse) -> some View {
        LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], spacing: 12) {
            MetricTile(title: "Datenwege", value: "\(overview.pairs.enabled)", detail: "\(overview.pairs.scheduled) automatisch", symbol: "point.3.connected.trianglepath.dotted")
            MetricTile(title: "Letzter Lauf", value: StatusStyle.label(for: overview.jobs.last?.status), detail: AppFormat.relative(overview.jobs.last?.startedAt), symbol: "clock.arrow.circlepath", tint: StatusStyle.color(for: overview.jobs.last?.status))
        }
    }

    private var copyOverview: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Kopien").font(.title3.bold())
            if let pairs = model.storage?.pairs, !pairs.isEmpty {
                ForEach(pairs) { pair in
                    CopyRow(pair: pair)
                    if pair.id != pairs.last?.id { Divider() }
                }
            } else {
                Text("Keine Datenwege eingerichtet").foregroundStyle(.secondary)
            }
        }
        .padding(16)
        .background(.background, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
    }

    @ViewBuilder
    private func lastRun(_ job: JobRecord?) -> some View {
        if let job {
            NavigationLink { RunDetailView(job: job) } label: {
                HStack(spacing: 12) {
                    Image(systemName: "clock.arrow.circlepath")
                        .font(.title3).foregroundStyle(StatusStyle.color(for: job.status))
                    VStack(alignment: .leading, spacing: 3) {
                        Text("Letzter Lauf").font(.headline)
                        Text("#\(job.id) · \(AppFormat.date(job.startedAt))").font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    StatusBadge(status: job.status)
                }
                .padding(16)
                .background(.background, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
            }
            .buttonStyle(.plain)
        }
    }
}

private struct CopyRow: View {
    let pair: StoragePair

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text(pair.name).font(.headline)
                Spacer()
                Text(AppFormat.relative(pair.lastSync)).font(.caption).foregroundStyle(.secondary)
            }
            endpoint(symbol: "folder.fill", title: "Lokal", path: pair.local, size: localSize)
            endpoint(symbol: "icloud.fill", title: "Cloud", path: pair.remote ?? "–", size: remoteSize)
        }
        .padding(.vertical, 4)
    }

    private var localSize: PathSize? { pair.source == pair.local ? pair.sourceSize : pair.targetSize }
    private var remoteSize: PathSize? { pair.source == pair.remote ? pair.sourceSize : pair.targetSize }

    private func endpoint(symbol: String, title: String, path: String, size: PathSize?) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 9) {
            Image(systemName: symbol).foregroundStyle(.teal).frame(width: 20)
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                Text(path).font(.subheadline).lineLimit(1).truncationMode(.middle)
            }
            Spacer(minLength: 8)
            VStack(alignment: .trailing, spacing: 2) {
                Text(AppFormat.count(size?.count)).font(.subheadline.weight(.semibold))
                Text(AppFormat.bytes(size?.bytes)).font(.caption).foregroundStyle(.secondary)
            }
        }
        .accessibilityElement(children: .combine)
    }
}
