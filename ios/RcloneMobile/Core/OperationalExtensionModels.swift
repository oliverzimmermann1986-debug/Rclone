import Foundation

// MARK: - Operational Extensions & Security Parity Models

struct LockdownStatusResponse: Decodable, Equatable {
    let active: Bool
    var since: Double? = nil
    var actor: String? = nil
    var reason: String? = nil
}

struct LockdownActivateRequest: Encodable {
    let password: String
    let reason: String
}

struct LockdownReleaseRequest: Encodable {
    let password: String
}

struct LockdownActivationResponse: Decodable, Equatable {
    var sessionsInvalidated: Bool? = nil

    enum CodingKeys: String, CodingKey {
        case sessionsInvalidated = "sessions_invalidated"
    }
}

struct LockdownReleaseResponse: Decodable, Equatable {
    let released: Bool
    let manualSteps: [String]

    enum CodingKeys: String, CodingKey {
        case released
        case manualSteps = "manual_steps"
    }
}

struct ObjectLockReportResponse: Decodable, Equatable {
    let generatedAt: Double?
    let buckets: [ObjectLockBucketItem]
    let error: String?

    enum CodingKeys: String, CodingKey {
        case buckets, error
        case generatedAt = "generated_at"
    }
}

struct ObjectLockBucketItem: Decodable, Identifiable, Equatable {
    var id: String { "\(remote):\(bucket)" }
    let remote: String
    let bucket: String
    let level: String
    let label: String
    let hint: String
    let versioning: Bool?
    let error: String?
}

struct BitrotScrubStatusResponse: Decodable {
    let lastJob: JobRecord?
    let due: Bool

    enum CodingKeys: String, CodingKey {
        case due
        case lastJob = "last_job"
    }
}

struct BootDrillStatusResponse: Decodable {
    let running: Bool
    let last: JobRecord?
    let due: Bool
}

struct BootDrillStartResponse: Decodable, Equatable {
    let jobID: Int

    enum CodingKeys: String, CodingKey {
        case jobID = "job_id"
    }
}

struct PairConflictsResponse: Decodable, Equatable {
    let pair: String
    let side1: String
    let side2: String
    let groups: [ConflictGroupItem]
    let truncated: Bool
    let skipped: Int
}

struct ConflictGroupItem: Decodable, Identifiable, Equatable {
    var id: String { basePath }
    let basePath: String
    let side1: [ConflictCopyEntry]
    let side2: [ConflictCopyEntry]
    let sideResolvable: Bool

    enum CodingKeys: String, CodingKey {
        case side1, side2
        case basePath = "base_path"
        case sideResolvable = "side_resolvable"
    }
}

struct ConflictCopyEntry: Decodable, Identifiable, Equatable {
    var id: String { "\(name)-\(conflictNumber)" }
    let name: String
    let size: Int64?
    let modtime: String?
    let conflictNumber: Int
    let origin: String?

    enum CodingKeys: String, CodingKey {
        case name, size, modtime, origin
        case conflictNumber = "conflict_n"
    }
}

struct ConflictResolveRequest: Encodable {
    let basePath: String
    let choice: String
    let password: String?

    enum CodingKeys: String, CodingKey {
        case choice, password
        case basePath = "base_path"
    }
}

struct ConflictResolveResponse: Decodable, Equatable {
    let pair: String
    let basePath: String
    let choice: String
    let completed: Bool

    enum CodingKeys: String, CodingKey {
        case pair, choice, completed
        case basePath = "base_path"
    }
}

struct TextDiffResponse: Decodable, Equatable {
    var recoveryID: String? = nil
    var pair: String? = nil
    let path: String
    var fromPoint: String? = nil
    var toPoint: String? = nil
    let rows: [TextDiffRow]
    let stats: TextDiffStats
    let truncated: Bool

    enum CodingKeys: String, CodingKey {
        case pair, path, rows, stats, truncated
        case recoveryID = "recovery_id"
        case fromPoint = "from_point"
        case toPoint = "to_point"
    }
}

struct TextDiffRow: Decodable, Identifiable, Equatable {
    var id: String { "\(leftNo ?? -1)-\(rightNo ?? -1)-\(type)" }
    let type: String
    let leftNo: Int?
    let left: String?
    let rightNo: Int?
    let right: String?

    enum CodingKeys: String, CodingKey {
        case type, left, right
        case leftNo = "left_no"
        case rightNo = "right_no"
    }
}

struct TextDiffStats: Decodable, Equatable {
    let equal: Int
    let insert: Int
    let delete: Int
    let replace: Int
}

struct CloudQuotaResponse: Decodable, Equatable {
    let generatedAt: Double?
    let remotes: [CloudRemoteQuotaItem]
    let budget: TransferBudgetUsage

    enum CodingKeys: String, CodingKey {
        case remotes, budget
        case generatedAt = "generated_at"
    }
}

struct CloudRemoteQuotaItem: Decodable, Identifiable, Equatable {
    var id: String { remote }
    let remote: String
    let ok: Bool
    let totalBytes: Int64?
    let usedBytes: Int64?
    let freeBytes: Int64?
    let percentUsed: Double?
    let dailyUploadBytes: Int64?
    let daysUntilFull: Double?
    let error: String?

    enum CodingKeys: String, CodingKey {
        case remote, ok, error
        case totalBytes = "total_bytes"
        case usedBytes = "used_bytes"
        case freeBytes = "free_bytes"
        case percentUsed = "percent_used"
        case dailyUploadBytes = "daily_upload_bytes"
        case daysUntilFull = "days_until_full"
    }
}

struct TransferBudgetUsage: Decodable, Equatable {
    let enabled: Bool
    let uploadBytes: Int64
    let egressBytes: Int64
    let uploadLimitBytes: Int64
    let egressLimitBytes: Int64
    let uploadPercent: Double?
    let egressPercent: Double?
    let uploadWarning: Bool
    let egressWarning: Bool
    let uploadExceeded: Bool
    let egressExceeded: Bool
    let throttleOptionalActive: Bool

    enum CodingKeys: String, CodingKey {
        case enabled
        case uploadBytes = "upload_bytes"
        case egressBytes = "egress_bytes"
        case uploadLimitBytes = "upload_limit_bytes"
        case egressLimitBytes = "egress_limit_bytes"
        case uploadPercent = "upload_percent"
        case egressPercent = "egress_percent"
        case uploadWarning = "upload_warning"
        case egressWarning = "egress_warning"
        case uploadExceeded = "upload_exceeded"
        case egressExceeded = "egress_exceeded"
        case throttleOptionalActive = "throttle_optional_active"
    }
}

struct RansomwareStatusResponse: Decodable, Equatable {
    let pairs: [RansomwarePairStatus]
}

struct RansomwarePairStatus: Decodable, Identifiable, Equatable {
    var id: String { name }
    let name: String
    let enabled: Bool
    let canary: CanaryCheckStatus
}

struct CanaryCheckStatus: Decodable, Equatable {
    let configured: Bool
    let ok: Bool
    var canaryFile: String? = nil
    var reason: String? = nil
    var error: String? = nil

    enum CodingKeys: String, CodingKey {
        case configured, ok, reason, error
        case canaryFile = "canary_file"
    }
}

struct CanaryInitResponse: Decodable, Equatable {
    let ok: Bool
    let pair: String
    let canaryFile: String
    let canarySha256: String

    enum CodingKeys: String, CodingKey {
        case ok, pair
        case canaryFile = "canary_file"
        case canarySha256 = "canary_sha256"
    }
}

struct TimeTravelBrowseResponse: Decodable, Equatable {
    let pair: String
    let pointID: String
    let path: String
    let items: [TimeTravelItem]
    let truncated: Bool

    enum CodingKeys: String, CodingKey {
        case pair, path, items, truncated
        case pointID = "point_id"
    }
}

struct TimeTravelItem: Decodable, Identifiable, Equatable {
    var id: String { "\(path)@\(sourcePoint)" }
    let name: String
    let path: String
    let isDirectory: Bool
    let size: Int64?
    let sourcePoint: String
    let statusAtPoint: String

    enum CodingKeys: String, CodingKey {
        case name, path, size
        case isDirectory = "is_dir"
        case sourcePoint = "source_point"
        case statusAtPoint = "status_at_point"
    }
}

struct TimeTravelHistoryResponse: Decodable, Equatable {
    let pair: String
    let path: String
    let versions: [TimeTravelVersionEntry]
}

struct TimeTravelVersionEntry: Decodable, Identifiable, Equatable {
    var id: String { pointID }
    let pointID: String
    let label: String
    let kind: String
    let createdAt: Double?
    let size: Int64?

    enum CodingKeys: String, CodingKey {
        case label, kind, size
        case pointID = "point_id"
        case createdAt = "created_at"
    }
}


struct BitrotScrubRunRequest: Encodable {
    let pairs: [String]?
}

struct CanaryInitRequest: Encodable {
    let filename: String?
    let currentPassword: String?

    enum CodingKeys: String, CodingKey {
        case filename
        case currentPassword = "current_password"
    }
}
