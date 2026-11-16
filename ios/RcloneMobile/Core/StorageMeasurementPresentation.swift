import Foundation

/// Presentation of existing size responses, including servers without measurement_status.
/// This describes a measurement, never whether a backup has succeeded.
struct StorageMeasurementPresentation {
    enum Issue: Equatable {
        case directoryMissing, rateLimited, storageFull, accessDenied, timeout, sharedGoogleClient, unknown
    }

    enum Freshness: Equatable {
        case fresh, cached, outdated, partial, unmeasured, measuring
    }

    struct Context {
        let isLocal: Bool
        let isSource: Bool
        let isBidirectional: Bool

        init(path: String, pair: StoragePair) {
            isLocal = path.hasPrefix("/")
            isSource = path == pair.source
            isBidirectional = pair.direction.lowercased() == "bisync"
        }

        init(isLocal: Bool, isSource: Bool, isBidirectional: Bool = false) {
            self.isLocal = isLocal
            self.isSource = isSource
            self.isBidirectional = isBidirectional
        }
    }

    let count: Int?
    let bytes: Int64?
    let measuredAt: Double?
    let issue: Issue?
    let freshness: Freshness
    let message: String?
    let technicalDetails: String?

    var isWarning: Bool { issue != nil || freshness == .outdated || freshness == .partial }
    var hasDetails: Bool { isWarning || technicalDetails != nil }

    var freshnessLabel: String {
        switch freshness {
        case .fresh: "Neu gemessen"
        case .cached: "Letzter Messstand"
        case .outdated: "Nicht frisch"
        case .partial: "Nicht frisch"
        case .unmeasured: "Nicht gemessen"
        case .measuring: "Messung läuft"
        }
    }

    static func missingSuccessNote(lastSync: Double?) -> String? {
        lastSync == nil ? "Für diesen Datenweg ist noch keine erfolgreiche Sicherung belegt." : nil
    }

    init(size: PathSize?, context: Context, isMeasuring: Bool = false) {
        count = size?.count
        bytes = size?.bytes
        measuredAt = size?.measuredAt
        var originals: [String] = []
        for text in [size?.measurementError, size?.error].compactMap({ $0 }) {
            if !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, !originals.contains(text) {
                originals.append(text)
            }
        }
        technicalDetails = originals.isEmpty ? nil : originals.joined(separator: "\n\n")
        issue = Self.classify(originals)
        let status = size?.measurementStatus?.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() ?? ""
        let hasValues = count != nil || bytes != nil
        let incompleteValues = hasValues && (count == nil || bytes == nil)
        let failed = ["failed", "error", "timeout", "stale"].contains(status)
        let unknownStatus = !["", "fresh", "cached", "partial", "failed", "error", "timeout", "stale", "loading", "pending"].contains(status)

        if status == "partial" || incompleteValues {
            freshness = .partial
        } else if issue != nil || failed || unknownStatus {
            freshness = .outdated
        } else if status == "fresh", hasValues {
            freshness = .fresh
        } else if hasValues {
            // A timestamp can document a cached measurement, not a new measurement.
            freshness = measuredAt == nil ? .outdated : .cached
        } else {
            freshness = isMeasuring || ["loading", "pending"].contains(status) ? .measuring : .unmeasured
        }

        if let issue {
            let warning = originals.joined(separator: "\n").lowercased()
            let expiring = ["will stop", "stop working", "deprecated", "expires", "expire", "läuft aus", "discontinued", "sunset"].contains { warning.contains($0) }
            message = issue == .sharedGoogleClient && expiring
                ? "Geteilte Google-Anbindung läuft aus. Google-Zugang aktualisieren."
                : Self.message(for: issue, context: context)
        } else {
            switch freshness {
            case .partial:
                message = "Die Messung ist unvollständig. Angezeigte Werte sind nicht frisch."
            case .outdated:
                message = hasValues
                    ? "Die angezeigten Werte sind nicht frisch. Bitte erneut messen."
                    : "Die Messung ist nicht verfügbar. Bitte später erneut versuchen."
            default:
                message = nil
            }
        }
    }

    private static func classify(_ originals: [String]) -> Issue? {
        guard !originals.isEmpty else { return nil }
        let text = originals.joined(separator: "\n").lowercased()
        func containsAny(_ tokens: [String]) -> Bool { tokens.contains { text.contains($0) } }
        func matches(_ pattern: String) -> Bool { text.range(of: pattern, options: .regularExpression) != nil }

        if containsAny(["directory not found", "folder not found", "path not found", "directory does not exist", "no such file or directory", "verzeichnis nicht gefunden"]) {
            return .directoryMissing
        }
        // Generic "Quota exceeded" is ambiguous. Storage quota must not be called an API rate limit.
        if containsAny(["storagequotaexceeded", "storage quota exceeded", "no space left on device", "disk quota exceeded", "insufficient storage"]) {
            return .storageFull
        }
        if containsAny(["ratelimitexceeded", "rate_limit_exceeded", "rate limit exceeded", "too many requests"])
            || matches(#"\b(?:http(?:/[\d.]+)?\s*|error\s*|status(?:\s+code)?\s*[:=]?\s*)429\b|"code"\s*:\s*429\b|\b(?:queries|requests)\b[^\n]*\bper\s+minute\b"#) {
            return .rateLimited
        }
        if containsAny(["quota exceeded", "quotaexceeded"]) { return .unknown }
        if containsAny(["permission denied", "access denied", "accessdenied", "unauthorized", "unauthenticated", "invalid_grant", "invalid_token", "invalid credentials", "authentication failed", "failed to refresh token", "token expired", "token has expired", "insufficientpermissions", "insufficient permissions", "forbidden"])
            || matches(#"\b(?:http(?:/[\d.]+)?\s*|error\s*|status(?:\s+code)?\s*[:=]?\s*)(?:401|403)\b"#) {
            return .accessDenied
        }
        if containsAny(["timeout", "timed out", "deadline exceeded", "zeitüberschreitung"]) {
            return .timeout
        }
        let lines = originals.flatMap { $0.components(separatedBy: .newlines) }
            .filter { !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
        let sharedNotice: (String) -> Bool = { line in
            let value = line.lowercased()
            return (value.contains("client_id") || value.contains("client id"))
                && (value.contains("google") || value.contains("drive"))
                && (value.contains("shared") || value.contains("geteilt"))
        }
        // Unknown text beside a notice may be the actual failure. Keep it a warning.
        if !lines.isEmpty, lines.allSatisfy(sharedNotice) { return .sharedGoogleClient }
        return .unknown
    }

    private static func message(for issue: Issue, context: Context) -> String {
        switch issue {
        case .directoryMissing:
            if context.isBidirectional {
                return "Ordner nicht gefunden. Die Pfade auf beiden Seiten prüfen."
            }
            if context.isSource {
                return context.isLocal
                    ? "Quellordner nicht gefunden. Pfad und Einbindung prüfen."
                    : "Cloud-Quelle nicht gefunden. Pfad und Zugriff prüfen."
            }
            return context.isLocal
                ? "Lokales Ziel nicht gefunden. Pfad und Einbindung prüfen."
                : "Cloud-Ziel nicht gefunden. Zielpfad und Zugriff prüfen."
        case .rateLimited:
            return "Cloud-Anfragelimit erreicht. Bitte später erneut messen."
        case .storageFull:
            return context.isLocal
                ? "Lokaler Speicherplatz ausgeschöpft. Freien Platz auf dem Server prüfen."
                : "Cloud-Speicherplatz ausgeschöpft. Freien Platz beim Speicheranbieter prüfen."
        case .accessDenied:
            return context.isLocal
                ? "Zugriff auf lokalen Ordner verweigert. Ordnerberechtigungen auf dem Server prüfen."
                : "Zugriff nicht möglich. Anmeldung und Berechtigungen prüfen."
        case .timeout:
            return "Die Messung hat zu lange gedauert. Bitte später erneut versuchen."
        case .sharedGoogleClient:
            return "Google-Zugang aktualisieren. rclone warnt vor der geteilten Google-Anbindung."
        case .unknown:
            return "Messung nicht verfügbar. Technische Details prüfen."
        }
    }
}
