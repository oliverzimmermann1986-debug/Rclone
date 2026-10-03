import Foundation

struct RestorePlanSettings: Codable, Equatable {
    var enabled: Bool
    var schedule: String
    var sampleFiles: Int
    var maxTotalMB: Int
    var maxScanFiles: Int

    enum CodingKeys: String, CodingKey {
        case enabled, schedule
        case sampleFiles = "sample_files"
        case maxTotalMB = "max_total_mb"
        case maxScanFiles = "max_scan_files"
    }
}

struct RestorePlanUpdate: Encodable {
    let revision: String
    let settings: RestorePlanSettings
}

struct RestorePlanResponse: Decodable {
    let revision: String
    let settings: RestorePlanSettings
    let timezone: String
    let generatedAt: Double
    let nextRuns: [Double]
    let dueNow: Bool
    let dataPaths: [RestorePlanPath]
    let warnings: [String]

    enum CodingKeys: String, CodingKey {
        case revision, settings, timezone, warnings
        case generatedAt = "generated_at"
        case nextRuns = "next_runs"
        case dueNow = "due_now"
        case dataPaths = "data_paths"
    }
}

struct RestorePlanPath: Decodable, Identifiable {
    let id: String
    let name: String
    let evidenceState: String
    let validUntil: Double?
    let coverageGap: Bool

    enum CodingKeys: String, CodingKey {
        case id, name
        case evidenceState = "evidence_state"
        case validUntil = "valid_until"
        case coverageGap = "coverage_gap"
    }
}

enum RestorePlanRhythm: String, CaseIterable, Identifiable {
    case daily, weekly, twiceWeekly, custom
    var id: String { rawValue }
    var title: String {
        switch self {
        case .daily: "Täglich"
        case .weekly: "Wöchentlich"
        case .twiceWeekly: "Sonntag und Mittwoch"
        case .custom: "Eigener Zeitplan"
        }
    }

    static func expression(_ rhythm: Self, hour: Int, minute: Int, weekday: Int) -> String? {
        switch rhythm {
        case .daily: "\(minute) \(hour) * * *"
        case .weekly: "\(minute) \(hour) * * \(weekday)"
        case .twiceWeekly: "\(minute) \(hour) * * 0,3"
        case .custom: nil
        }
    }
}
