import Foundation

/// Applies only the displayed profile fields. All unknown configuration fields
/// survive the round trip, and an existing shared job is never rewritten.
enum ProtectionSetup {
    static func pair(_ original: PairConfig, profile: RecoveryPolicyProfile, snapshots: Bool) throws -> PairConfig {
        guard original.direction == "push" else {
            throw APIError.server(status: 422, message: "Der Assistent benötigt einen Push-Datenweg. Pull und Bisync bitte unter Mehr konfigurieren.")
        }
        let encoded = try JSONEncoder().encode(original)
        var fields = try JSONDecoder().decode([String: JSONValue].self, from: encoded)
        for (key, value) in profile.pair { fields[key] = value }
        fields["id"] = .string(original.id)
        fields["recovery_snapshots"] = .bool(snapshots)
        return try JSONDecoder().decode(PairConfig.self, from: JSONEncoder().encode(fields))
    }

    static func jobs(_ original: [JobDefinition], pair: PairConfig, profile: RecoveryPolicyProfile) -> [JobDefinition] {
        guard !original.contains(where: { $0.dataPathIDs.contains(pair.id) }) else { return original }
        let schedule: String
        if case let .string(value)? = profile.job["schedule"] { schedule = value } else { schedule = "manual" }
        let retry: Int
        if case let .number(value)? = profile.job["retry_minutes"] { retry = Int(value) } else { retry = 60 }
        let baseName = "Schutz " + String(pair.name.prefix(60))
        let names = Set(original.map { $0.name.lowercased() })
        var name = baseName
        var suffix = 2
        while names.contains(name.lowercased()) { name = "\(baseName) \(suffix)"; suffix += 1 }
        let id = UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
        return original + [JobDefinition(id: id, name: name,
            dataPathIDs: [pair.id], schedule: schedule, retryMinutes: retry)]
    }
}
