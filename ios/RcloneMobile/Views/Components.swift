import SwiftUI
import UIKit

struct StatusBadge: View {
    let status: String?

    var body: some View {
        Label(StatusStyle.label(for: status), systemImage: "circle.fill")
            .font(.caption.weight(.semibold))
            .foregroundStyle(StatusStyle.color(for: status))
            .labelStyle(CompactStatusLabelStyle())
            .padding(.horizontal, 9)
            .padding(.vertical, 5)
            .background(StatusStyle.color(for: status).opacity(0.12), in: Capsule())
            .accessibilityLabel("Status: \(StatusStyle.label(for: status))")
    }
}

private struct CompactStatusLabelStyle: LabelStyle {
    func makeBody(configuration: Configuration) -> some View {
        HStack(spacing: 5) {
            configuration.icon.font(.system(size: 7))
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
