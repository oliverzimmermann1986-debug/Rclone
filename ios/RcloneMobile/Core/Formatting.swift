import Foundation
import SwiftUI

enum AppFormat {
    static let bytes: ByteCountFormatter = {
        let formatter = ByteCountFormatter()
        formatter.allowedUnits = [.useKB, .useMB, .useGB, .useTB]
        formatter.countStyle = .file
        return formatter
    }()

    static let relative: RelativeDateTimeFormatter = {
        let formatter = RelativeDateTimeFormatter()
        formatter.locale = Locale(identifier: "de_DE")
        formatter.unitsStyle = .full
        return formatter
    }()

    static let date: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "de_DE")
        formatter.dateStyle = .medium
        formatter.timeStyle = .short
        return formatter
    }()

    static func bytes(_ value: Int64?) -> String {
        guard let value else { return "–" }
        return bytes.string(fromByteCount: value)
    }

    static func count(_ value: Int?) -> String {
        guard let value else { return "–" }
        return value.formatted(.number.locale(Locale(identifier: "de_DE")))
    }

    static func date(_ timestamp: Double?) -> String {
        guard let timestamp else { return "Noch nie" }
        return date.string(from: Date(timeIntervalSince1970: timestamp))
    }

    static func relative(_ timestamp: Double?) -> String {
        guard let timestamp else { return "Noch nie" }
        return relative.localizedString(for: Date(timeIntervalSince1970: timestamp), relativeTo: Date())
    }

    static func duration(start: Double, end: Double?) -> String {
        guard let end else { return "Läuft" }
        return elapsed(max(0, end - start))
    }

    static func elapsed(_ seconds: Double) -> String {
        let total = max(0, Int(seconds.rounded()))
        let days = total / 86_400
        let hours = (total % 86_400) / 3_600
        let minutes = (total % 3_600) / 60
        let remainingSeconds = total % 60
        if days > 0 { return "\(days) T \(hours) Std" }
        if hours > 0 { return "\(hours) Std \(minutes) Min" }
        if minutes > 0 { return "\(minutes) Min \(remainingSeconds) Sek" }
        return "\(remainingSeconds) Sek"
    }
}

enum StatusStyle {
    static func color(for value: String?) -> Color {
        switch value?.lowercased() {
        case "ok", "active", "healthy": .green
        case "running": .blue
        case "warn", "warning", "stale", "overdue": .orange
        case "error", "failed", "inactive": .red
        case "cancelled", "skipped": .secondary
        default: .secondary
        }
    }

    static func label(for value: String?) -> String {
        switch value?.lowercased() {
        case "ok": "Erfolgreich"
        case "running": "Läuft"
        case "error", "failed": "Fehler"
        case "stale": "Veraltet"
        case "cancelled": "Abgebrochen"
        case "skipped": "Übersprungen"
        case "active": "Aktiv"
        case "inactive": "Inaktiv"
        case let value?: value.capitalized
        case nil: "Unbekannt"
        }
    }
}
