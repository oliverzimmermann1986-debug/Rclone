import SwiftUI
import UIKit

struct StatusBadge: View {
    let status: String?

    private var isWarning: Bool {
        ["warn", "warning", "partial"].contains(status?.lowercased() ?? "")
    }

    var body: some View {
        Label(StatusStyle.label(for: status), systemImage: StatusStyle.symbol(for: status))
            .font(.caption.weight(.semibold))
            .foregroundStyle(StatusStyle.color(for: status))
            .labelStyle(CompactStatusLabelStyle(isWarning: isWarning))
            .padding(.horizontal, 9)
            .padding(.vertical, 5)
            .background(StatusStyle.color(for: status).opacity(0.12), in: Capsule())
            .accessibilityLabel("Status: \(StatusStyle.label(for: status))")
    }
}

private struct CompactStatusLabelStyle: LabelStyle {
    let isWarning: Bool

    func makeBody(configuration: Configuration) -> some View {
        HStack(spacing: 5) {
            configuration.icon.font(isWarning ? .caption2 : .system(size: 7))
            configuration.title
        }
    }
}

struct LoadingSection: View {
    let label: String

    var body: some View {
        HStack(spacing: 12) {
            ProgressView()
            Text(label).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, minHeight: 120)
    }
}

struct ErrorBanner: View {
    let message: String
    let dismiss: () -> Void
    @AccessibilityFocusState private var isMessageFocused: Bool

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "exclamationmark.triangle.fill")
                .foregroundStyle(.red)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 3) {
                Text("Aktion erforderlich")
                    .font(.caption.weight(.semibold))
                    .accessibilityAddTraits(.isHeader)
                Text(message)
                    .font(.subheadline)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .accessibilityElement(children: .combine)
            .accessibilityLabel("Fehler. \(message)")
            .accessibilityHint("Prüfe die Angaben oder versuche die Aktion erneut.")
            .accessibilityFocused($isMessageFocused)
            Button(action: dismiss) { Image(systemName: "xmark") }
                .buttonStyle(.plain)
                .accessibilityLabel("Meldung schließen")
                .accessibilityHint("Blendet diese Fehlermeldung aus.")
        }
        .padding(14)
        .background(.red.opacity(0.1), in: RoundedRectangle(cornerRadius: 14, style: .continuous))
        .onAppear { announce(message) }
        .onChange(of: message) { _, newMessage in announce(newMessage) }
    }

    private func announce(_ message: String) {
        isMessageFocused = true
        UIAccessibility.post(notification: .announcement, argument: "Fehler. \(message)")
    }
}

struct LoadFailureView: View {
    let title: String
    let message: String
    let retry: () -> Void

    var body: some View {
        ContentUnavailableView {
            Label(title, systemImage: "exclamationmark.triangle")
        } description: {
            Text(message)
        } actions: {
            Button("Erneut versuchen", action: retry)
                .buttonStyle(.borderedProminent)
                .accessibilityIdentifier("retryLoadButton")
        }
    }
}

/// Keeps a confirmed action tied to the configuration the user actually saw.
struct RestoreActionTarget: Identifiable, Equatable {
    let id: String
    let name: String
    let local: String
    let remote: String
    let direction: String

    init(pair: PairConfig) {
        id = pair.id
        name = pair.name
        local = pair.local
        remote = pair.remote
        direction = pair.direction
    }

    func matches(_ pair: PairConfig) -> Bool {
        pair.enabled && id == pair.id && name == pair.name && local == pair.local
            && remote == pair.remote && direction == pair.direction
    }
}

struct RestoreTestActionButton: View {
    @EnvironmentObject private var model: AppModel
    let pairName: String
    let title: String
    @State private var target: RestoreActionTarget?
    @State private var targetServer: String?
    @State private var targetUsername: String?
    @State private var isConfirming = false
    @State private var successFeedback = 0

    private var configuredPair: PairConfig? {
        model.config?.backup.pairs.first { $0.name == pairName && $0.enabled }
    }

    var body: some View {
        Button {
            guard model.canStartRestoreTest, let configuredPair else { return }
            target = RestoreActionTarget(pair: configuredPair)
            targetServer = model.serverAddress
            targetUsername = model.savedUsername
            isConfirming = true
        } label: {
            Label(model.isRestoreTestRunning(for: pairName) ? "\(pairName) wird geprüft …" : title,
                  systemImage: model.isRestoreTestRunning(for: pairName) ? "hourglass" : "arrow.counterclockwise.circle")
        }
        .disabled(!model.canStartRestoreTest || configuredPair == nil)
        .accessibilityIdentifier("restoreTestActionButton")
        .accessibilityHint("Öffnet die Bestätigung für eine Restore-Stichprobe dieses Datenwegs.")
        .confirmationDialog("\(target?.name ?? pairName) jetzt prüfen?", isPresented: $isConfirming,
                            titleVisibility: .visible) {
            Button("Stichprobe starten") {
                guard model.canStartRestoreTest, let target,
                      targetServer == model.serverAddress, targetUsername == model.savedUsername,
                      model.config?.backup.pairs.contains(where: target.matches) == true else { return }
                Task {
                    if await model.runRestoreTest(pair: target.name) { successFeedback += 1 }
                }
            }
            Button("Abbrechen", role: .cancel) {}
        } message: {
            Text("Der Server holt eine begrenzte Stichprobe von \(target?.name ?? pairName) in einen temporären Ordner, prüft sie per Prüfsumme und entfernt die Testkopien anschließend. Originaldateien bleiben erhalten.")
        }
        .sensoryFeedback(.success, trigger: successFeedback)
    }
}

struct AdaptiveMetricGroup<Content: View>: View {
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    private let content: Content
    private let spacing: CGFloat

    init(spacing: CGFloat = 0, @ViewBuilder content: () -> Content) {
        self.spacing = spacing
        self.content = content()
    }

    var body: some View {
        let layout = dynamicTypeSize.isAccessibilitySize
            ? AnyLayout(VStackLayout(alignment: .leading, spacing: 12))
            : AnyLayout(HStackLayout(alignment: .center, spacing: spacing))
        layout { content }
    }
}

extension RecoveryCalendarDay {
    var accessibilitySummary: String {
        "\(date). \(total) Läufe: \(successful) erfolgreich, \(failed) fehlgeschlagen, \(cancelled) abgebrochen. \(restoreTests) Restore-Prüfungen."
    }
}

enum ProtectionPathSelection {
    static func resolve(original: StoragePair, dataPathID: String?, storage: StorageOverview?,
                        config: ConfigSnapshot?) -> StoragePair? {
        if let dataPathID {
            guard let configured = config?.backup.pairs.first(where: { $0.id == dataPathID }) else { return nil }
            return storage?.pairs.first {
                $0.name == configured.name && $0.local == configured.local
                    && $0.remote == configured.remote && $0.direction == configured.direction
            }
        }
        if let config {
            guard config.backup.pairs.contains(where: {
                $0.name == original.name && $0.local == original.local && $0.remote == original.remote
                    && $0.direction == original.direction
            }) else { return nil }
        }
        return storage?.pairs.first {
            $0.name == original.name && $0.local == original.local && $0.remote == original.remote
                && $0.direction == original.direction
        }
    }
}
