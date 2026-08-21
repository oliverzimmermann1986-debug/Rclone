import SwiftUI

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

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.red)
            Text(message).font(.subheadline).frame(maxWidth: .infinity, alignment: .leading)
            Button(action: dismiss) { Image(systemName: "xmark") }
                .buttonStyle(.plain)
                .accessibilityLabel("Meldung schließen")
        }
        .padding(14)
        .background(.red.opacity(0.1), in: RoundedRectangle(cornerRadius: 14, style: .continuous))
    }
}
