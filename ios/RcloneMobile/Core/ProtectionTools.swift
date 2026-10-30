import Foundation
import Combine

/// Preserves unknown server fields when editing a small part of a configuration.
struct ProtectionToolDocument: Codable, Equatable {
    var values: [String: JSONValue] = [:]

    init(_ values: [String: JSONValue] = [:]) { self.values = values }
    init(from decoder: Decoder) throws {
        values = try decoder.singleValueContainer().decode([String: JSONValue].self)
    }
    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        try container.encode(values)
    }
    func string(_ key: String) -> String {
        guard case let .string(value)? = values[key] else { return "" }
        return value
    }
    func number(_ key: String) -> Double? {
        guard case let .number(value)? = values[key], value.isFinite else { return nil }
        return value
    }
    func integer(_ key: String) -> Int? { number(key).flatMap { Int(exactly: $0) } }
    func bool(_ key: String) -> Bool? {
        guard case let .bool(value)? = values[key] else { return nil }
        return value
    }
    func object(_ key: String) -> ProtectionToolDocument {
        guard case let .object(value)? = values[key] else { return .init() }
        return .init(value)
    }
    func objects(_ key: String) -> [ProtectionToolDocument] {
        guard case let .array(values)? = values[key] else { return [] }
        return values.compactMap { value in
            guard case let .object(object) = value else { return nil }
            return .init(object)
        }
    }
    func strings(_ key: String) -> [String] {
        guard case let .array(values)? = values[key] else { return [] }
        return values.compactMap { value in
            guard case let .string(text) = value else { return nil }
            return text
        }
    }
}

enum ProtectionToolRequest {
    case search(query: String, identity: String, cursor: String? = nil)
    case restoreFile(identity: String, pointID: String, path: String, maxTotalMB: Int)
    case forecasts, forecastTarget(identity: String), forecastHistory(identity: String), sampleForecasts(identity: String?)
    case setCapacity(identity: String, capacityBytes: Int64?, revision: String)
    case configuration, saveConfiguration(ProtectionToolDocument, password: String)
    case heartbeat, operationsSettings
    case saveOperations(revision: String, heartbeat: [String: JSONValue]?, report: [String: JSONValue]?, password: String)
    case testHeartbeat(password: String)
    case migrations, migration(id: String)
    case previewMigration(identity: String, target: String, revision: String, maxTotalMB: Int)
    case runMigration(id: String, password: String)
    case switchMigration(id: String, revision: String, password: String)
    case weeklyReports, weeklyHistory, generateReport, sendReport(id: Int, password: String)

    var method: String {
        switch self {
        case .search, .forecasts, .forecastTarget, .forecastHistory, .configuration, .heartbeat,
                .operationsSettings, .migrations, .migration, .weeklyReports, .weeklyHistory: "GET"
        case .saveConfiguration, .saveOperations, .setCapacity: "PUT"
        default: "POST"
        }
    }

    var path: String {
        switch self {
        case let .search(query, identity, cursor):
            Self.query("/api/recovery/files/search", ["query": query, "identity": identity, "limit": "50", "cursor": cursor ?? ""])
        case .restoreFile: "/api/recovery/restore"
        case .forecasts: "/api/storage/forecast"
        case let .forecastTarget(identity): Self.query("/api/storage/forecast", ["identity": identity])
        case let .forecastHistory(identity): Self.query("/api/storage/forecast/history", ["identity": identity])
        case .setCapacity: "/api/storage/forecast/capacity"
        case .sampleForecasts: "/api/storage/forecast/sample"
        case .configuration, .saveConfiguration: "/api/config"
        case .heartbeat: "/api/operations/heartbeat"
        case .operationsSettings, .saveOperations: "/api/operations/settings"
        case .testHeartbeat: "/api/operations/heartbeat/test"
        case .migrations: "/api/migrations"
        case let .migration(id): "/api/migrations/\(Self.component(id))"
        case .previewMigration: "/api/migrations/preview"
        case let .runMigration(id, _): "/api/migrations/\(Self.component(id))/run"
        case let .switchMigration(id, _, _): "/api/migrations/\(Self.component(id))/switch"
        case .weeklyReports: "/api/reports/weekly"
        case .weeklyHistory: "/api/reports/weekly/history"
        case .generateReport: "/api/reports/weekly/generate"
        case let .sendReport(id, _): "/api/reports/weekly/\(id)/send"
        }
    }

    var body: [String: JSONValue]? {
        switch self {
        case let .restoreFile(identity, pointID, path, limit):
            ["identity": .string(identity), "point_id": .string(pointID), "paths": .array([.string(path)]), "max_total_mb": .number(Double(limit))]
        case let .sampleForecasts(identity): identity.map { ["identity": .string($0)] } ?? [:]
        case let .setCapacity(identity, capacity, revision):
            ["identity": .string(identity), "capacity_bytes": capacity.map { .number(Double($0)) } ?? .null, "expected_revision": .string(revision)]
        case let .saveConfiguration(document, password):
            ["config": .object(document.values), "current_password": .string(password)]
        case let .saveOperations(revision, heartbeat, report, password):
            Self.operationsBody(revision: revision, heartbeat: heartbeat, report: report, password: password)
        case let .testHeartbeat(password), let .runMigration(_, password), let .sendReport(_, password):
            ["current_password": .string(password)]
        case let .previewMigration(identity, target, revision, limit):
            ["identity": .string(identity), "target": .string(target), "expected_revision": .string(revision), "max_total_mb": .number(Double(limit))]
        case let .switchMigration(_, revision, password):
            ["expected_revision": .string(revision), "current_password": .string(password)]
        case .generateReport: [:]
        default: nil
        }
    }

    func encodedBody() throws -> Data? {
        if case let .setCapacity(identity, capacity, revision) = self {
            return try JSONEncoder().encode(ProtectionCapacityUpdate(identity: identity, capacity: capacity, revision: revision))
        }
        return try body.map { try JSONEncoder().encode($0) }
    }

    private static func operationsBody(revision: String, heartbeat: [String: JSONValue]?, report: [String: JSONValue]?, password: String) -> [String: JSONValue] {
        var body: [String: JSONValue] = ["expected_revision": .string(revision), "current_password": .string(password)]
        if let heartbeat { body["heartbeat"] = .object(heartbeat) }
        if let report { body["weekly_report"] = .object(report) }
        return body
    }

    private static func query(_ path: String, _ values: [String: String]) -> String {
        var components = URLComponents()
        components.path = path
        components.queryItems = values.sorted(by: { $0.key < $1.key }).compactMap {
            $0.value.isEmpty ? nil : URLQueryItem(name: $0.key, value: $0.value)
        }
        components.percentEncodedQuery = components.percentEncodedQuery?.replacingOccurrences(of: "+", with: "%2B")
        return components.string ?? path
    }

    private static func component(_ value: String) -> String {
        value.addingPercentEncoding(withAllowedCharacters: .alphanumerics.union(.init(charactersIn: "-_"))) ?? ""
    }
}

private struct ProtectionCapacityUpdate: Encodable {
    let identity: String
    let capacity: Int64?
    let revision: String
    enum CodingKeys: String, CodingKey { case identity, capacity = "capacity_bytes", revision = "expected_revision" }
    func encode(to encoder: Encoder) throws {
        var values = encoder.container(keyedBy: CodingKeys.self)
        try values.encode(identity, forKey: .identity)
        try values.encode(capacity, forKey: .capacity)
        try values.encode(revision, forKey: .revision)
    }
}

struct ProtectionToolServerBinding: Equatable {
    let server: String
    let username: String

    @MainActor init(_ model: AppModel) {
        server = model.serverAddress
        username = model.savedUsername
    }
    @MainActor func matches(_ model: AppModel) -> Bool {
        server == model.serverAddress && username == model.savedUsername && model.phase == .signedIn
    }
}

@MainActor
final class ProtectionToolsSession: ObservableObject {
    @Published private(set) var documents: [String: ProtectionToolDocument] = [:]
    @Published private(set) var isWorking = false
    @Published var errorMessage: String?
    @Published var information: String?
    private(set) var binding: ProtectionToolServerBinding?
    private var generation = UUID()

    subscript(_ key: String) -> ProtectionToolDocument { documents[key] ?? .init() }

    func reset(for model: AppModel) {
        generation = UUID()
        binding = .init(model)
        documents = [:]
        errorMessage = nil
        information = nil
        isWorking = false
    }

    @discardableResult
    func call(_ request: ProtectionToolRequest, key: String = "data", model: AppModel) async -> ProtectionToolDocument? {
        guard !isWorking, binding?.matches(model) == true else { return nil }
        let owner = generation
        isWorking = true
        errorMessage = nil
        defer { if generation == owner { isWorking = false } }
        do {
            let document = try await model.withCurrentClient { try await $0.protectionTool(request) }
            guard owner == generation else { return nil }
            if document.bool("ok") == false {
                throw APIError.server(status: 400, message: document.string("error"))
            }
            documents[key] = document
            return document
        } catch is CancellationError {
            return nil
        } catch {
            if owner == generation { errorMessage = error.localizedDescription }
            return nil
        }
    }
}

enum ProtectionToolLabels {
    static func bytes(_ value: Double?) -> String {
        AppFormat.bytes(value.flatMap { Int64(exactly: $0.rounded(.towardZero)) })
    }
    static func state(_ value: String) -> String {
        switch value {
        case "ok": "In Ordnung"
        case "warning": "Kapazität bald erreicht"
        case "full": "Speicher voll"
        case "stale": "Messung veraltet"
        case "unknown", "unconfigured": "Noch nicht verfügbar"
        case "insufficient_samples": "Messreihe wird aufgebaut"
        case "not_growing": "Kein anhaltendes Wachstum"
        case "uncertain": "Trend noch unsicher"
        case "disabled": "Deaktiviert"
        case "error", "failed": "Fehlgeschlagen"
        case "preview": "Vorschau bereit"
        case "running": "Übertragung läuft"
        case "switching": "Zielübernahme wird geprüft"
        case "verified": "Prüfsummen bestätigt"
        case "switched": "Neues Ziel übernommen"
        case "cancelled": "Abgebrochen"
        case "expired": "Plan abgelaufen"
        case "sent": "Versendet"
        case "retrying": "Erneuter Versuch ausstehend"
        case "sending": "Wird versendet"
        case "pending": "Ausstehend"
        default: value.isEmpty ? "–" : value
        }
    }
}
