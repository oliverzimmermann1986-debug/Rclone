import Foundation

/// Presents persisted job findings, including the per-path results of backup runs.
enum JobFindings {
    /// A completed backup can retain a warning, without changing its stored outcome.
    static func severity(for job: JobRecord) -> String {
        let status = rawStatus(for: job)
        if status == "timeout" { return "error" }
        guard ["ok", "success", "done"].contains(status) else { return status }
        let summary = job.summary ?? [:]
        if !findings(in: summary, preferWarning: true, includeErrors: false).isEmpty { return "warning" }
        if case let .array(pairs)? = summary["pairs"] {
            for case let .object(pair) in pairs {
                if !findings(in: pair, preferWarning: true, includeErrors: false).isEmpty { return "warning" }
            }
        }
        return status
    }

    static func message(for job: JobRecord) -> String? {
        let status = severity(for: job)
        guard ["error", "failed", "timeout", "warning", "warn", "partial", "cancelled", "stale"].contains(status) else {
            // Successful/running jobs can contain obsolete diagnostic fields.
            return nil
        }
        let isWarning = ["warning", "warn", "partial"].contains(status)
        let includeErrors = !["ok", "success", "done"].contains(rawStatus(for: job))
        let summary = job.summary ?? [:]
        var pathFindings: [(body: String, rendered: String)] = []
        if case let .array(pairs)? = summary["pairs"] {
            for case let .object(pair) in pairs {
                let name = text(pair["name"])
                let pairStatus = text(pair["status"])?.lowercased()
                let succeeded = pair["ok"] == .bool(true)
                    || (pair["ok"] != .bool(false) && ["ok", "success", "done"].contains(pairStatus ?? ""))
                var messages = findings(in: pair, preferWarning: isWarning, includeErrors: includeErrors && !succeeded)
                // The restore aggregate is not an additional failed data path.
                let isRestoreAggregate = job.kind == "restoretest" && name == "restore-drill"
                    && pair["pairs_tested"] != nil && pair["sample_status"] == nil
                if messages.isEmpty, !isWarning, !succeeded, !isRestoreAggregate,
                   pair["ok"] == .bool(false) || ["error", "failed", "timeout"].contains(pairStatus ?? "") {
                    messages = ["Kein Fehlergrund übermittelt. Vollständiges Protokoll öffnen."]
                }
                for message in messages {
                    let rendered: String
                    if let name, !message.hasPrefix("\(name):") {
                        rendered = "\(name): \(message)"
                    } else {
                        rendered = message
                    }
                    pathFindings.append((message, rendered))
                }
            }
        }

        var seen = Set<String>()
        var messages: [String] = []
        for finding in pathFindings {
            if seen.insert(key(finding.rendered)).inserted { messages.append(finding.rendered) }
        }
        let pathBodies = Set(pathFindings.map { key($0.body) })
        for message in findings(in: summary, preferWarning: isWarning, includeErrors: includeErrors) {
            // Keep the path-qualified copy when the same message is also aggregated.
            if !pathBodies.contains(key(message)), seen.insert(key(message)).inserted {
                messages.append(message)
            }
        }
        if !messages.isEmpty { return messages.joined(separator: "\n\n") }

        switch status {
        case "warning", "warn":
            return "Der Lauf enthält Hinweise. Vollständiges Protokoll öffnen."
        case "partial":
            return "Der Prüfumfang ist begrenzt. Vollständiges Protokoll öffnen."
        case "cancelled":
            return "Der Lauf wurde abgebrochen. Vollständiges Protokoll öffnen."
        case "stale":
            return "Für diesen Lauf liegt kein aktuelles Ergebnis vor. Vollständiges Protokoll öffnen."
        default:
            return "Der Lauf ist fehlgeschlagen, aber es wurde kein Fehlergrund übermittelt. Vollständiges Protokoll öffnen."
        }
    }

    private static func rawStatus(for job: JobRecord) -> String {
        (job.displayStatus ?? job.status).trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
    }

    private static func findings(in fields: [String: JSONValue], preferWarning: Bool, includeErrors: Bool) -> [String] {
        let errors = includeErrors ? [text(fields["error"])].compactMap { $0 } : []
        var warnings = [text(fields["warning"])].compactMap { $0 }
        if case let .array(values)? = fields["warnings"] {
            warnings.append(contentsOf: values.compactMap { text($0) })
        }
        return preferWarning ? warnings + errors : errors + warnings
    }

    private static func text(_ value: JSONValue?) -> String? {
        guard case let .string(raw)? = value else { return nil }
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }

    private static func key(_ message: String) -> String {
        message.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
    }
}
