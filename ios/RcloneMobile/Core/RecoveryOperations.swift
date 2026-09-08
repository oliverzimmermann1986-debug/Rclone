import Foundation

struct FullSnapshotRequest: Encodable {
    let identity: String
    let maxTotalMB: Int
    enum CodingKeys: String, CodingKey { case identity; case maxTotalMB = "max_total_mb" }
}

struct RecoveryRescueRequest: Encodable {
    let envelope: [String: JSONValue]
    let passphrase: String
    var currentPassword: String? = nil
    var mappings: [String: String]? = nil
    enum CodingKeys: String, CodingKey {
        case envelope, passphrase, mappings
        case currentPassword = "current_password"
    }
}

struct RecoveryRescueTarget: Decodable, Identifiable {
    var id: String { identity }
    let identity: String
    let name: String
    let target: String?
    let targetHint: String?
    enum CodingKeys: String, CodingKey { case identity, name, target; case targetHint = "target_hint" }
}

struct RecoveryRescuePreview: Decodable {
    let records: Int
    let totalBytes: Int64
    let dataPaths: [RecoveryRescueTarget]
    let availableTargets: [RecoveryRescueTarget]
    enum CodingKeys: String, CodingKey {
        case records
        case totalBytes = "total_bytes"
        case dataPaths = "data_paths"
        case availableTargets = "available_targets"
    }
}

struct RecoveryRescueImport: Decodable {
    let ok: Bool
    let imported: Int
    let alreadyPresent: Int
    enum CodingKeys: String, CodingKey { case ok, imported; case alreadyPresent = "already_present" }
}
