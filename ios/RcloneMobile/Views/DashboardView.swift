import SwiftUI

struct DashboardView: View {
    @EnvironmentObject private var model: AppModel
    @Binding var showingSettings: Bool
    @State private var confirmRunAll = false
    @State private var confirmCancel = false
    @State private var successFeedback = 0

    var body: some View {
        List {
            if let error = model.errorMessage {
                ErrorBanner(message: error, dismiss: model.dismissMessages)
                    .listRowBackground(Color.clear)
                    .listRowInsets(EdgeInsets(top: 4, leading: 16, bottom: 4, trailing: 16))
            }

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

                Section("Kopien") {
                    if let pairs = model.storage?.pairs, !pairs.isEmpty {
                        ForEach(pairs) { pair in
                            CopyListRow(pair: pair)
                        }
                    } else {
                        ContentUnavailableView(
                            "Keine Datenwege",
                            systemImage: "arrow.left.arrow.right",
                            description: Text("Eingerichtete Kopien erscheinen hier.")
                        )
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
                LoadingSection(label: "Lage wird geladen …")
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
                Task { if await model.runBackup() { successFeedback += 1 } }
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
        let hasError = overview.alerts.contains { $0.level == "error" }
        return HStack(spacing: 14) {
            ZStack {
                Circle()
                    .fill((hasError ? Color.red : Color.green).opacity(0.14))
                Image(systemName: hasError ? "exclamationmark" : "checkmark")
                    .font(.title2.bold())
                    .foregroundStyle(hasError ? Color.red : Color.green)
            }
            .frame(width: 50, height: 50)
            .accessibilityHidden(true)

            VStack(alignment: .leading, spacing: 4) {
                Text(hasError ? "Aufmerksamkeit nötig" : "Alles in Ordnung")
                    .font(.title3.weight(.semibold))
                Text("\(overview.system.hostname) · \(AppFormat.relative(overview.generatedAt))")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 5)
        .accessibilityElement(children: .combine)
    }

    private var liveProgress: some View {
        VStack(alignment: .leading, spacing: 13) {
            HStack {
                Label("Sicherung läuft", systemImage: "arrow.triangle.2.circlepath")
                    .font(.headline)
                    .foregroundStyle(.blue)
                Spacer()
                Text(AppFormat.elapsed(Double(model.progress?.elapsedSeconds ?? 0)))
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(.secondary)
            }

            let total = max(model.progress?.totalPairs ?? 0, 1)
            let done = model.progress?.donePairs ?? 0
            ProgressView(value: Double(done), total: Double(total))
                .tint(.blue)

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
            }
        }
        .accessibilityElement(children: .combine)
    }
}
