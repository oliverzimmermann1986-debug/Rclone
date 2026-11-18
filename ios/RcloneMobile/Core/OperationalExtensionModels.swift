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

struct DueStatusDetail: Decodable, Equatable {
    let due: Bool
    var reason: String? = nil
    var nextRun: Double? = nil

    enum CodingKeys: String, CodingKey {
        case due, reason
        case nextRun = "next_run"
    }
}

struct BitrotScrubStatusResponse: Decodable {
    let lastJob: JobRecord?
    let due: Bool
    let dueDetail: DueStatusDetail?

    enum CodingKeys: String, CodingKey {
        case due
        case lastJob = "last_job"
    }

    init(lastJob: JobRecord? = nil, due: Bool, dueDetail: DueStatusDetail? = nil) {
        self.lastJob = lastJob
        self.due = due
        self.dueDetail = dueDetail
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        lastJob = try container.decodeIfPresent(JobRecord.self, forKey: .lastJob)
        if let detail = try? container.decode(DueStatusDetail.self, forKey: .due) {
            due = detail.due
            dueDetail = detail
        } else if let flag = try? container.decode(Bool.self, forKey: .due) {
            due = flag
            dueDetail = DueStatusDetail(due: flag)
        } else {
            due = false
            dueDetail = nil
        }
    }
}

struct BootDrillStatusResponse: Decodable {
    let running: Bool
    let last: JobRecord?
    let due: Bool
    let dueDetail: DueStatusDetail?

    enum CodingKeys: String, CodingKey {
        case running, last, due
    }

    init(running: Bool, last: JobRecord? = nil, due: Bool, dueDetail: DueStatusDetail? = nil) {
        self.running = running
        self.last = last
        self.due = due
        self.dueDetail = dueDetail
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        running = try container.decodeIfPresent(Bool.self, forKey: .running) ?? false
        last = try container.decodeIfPresent(JobRecord.self, forKey: .last)
        if let detail = try? container.decode(DueStatusDetail.self, forKey: .due) {
            due = detail.due
            dueDetail = detail
        } else if let flag = try? container.decode(Bool.self, forKey: .due) {
            due = flag
            dueDetail = DueStatusDetail(due: flag)
        } else {
            due = false
            dueDetail = nil
        }
    }
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

struct SuspectExtensionScanSummary: Decodable, Equatable {
    var blocked: Bool = false
    var suspectCount: Int? = nil
    var error: String? = nil

    enum CodingKeys: String, CodingKey {
        case blocked, error
        case suspectCount = "suspect_count"
    }

    init(blocked: Bool = false, suspectCount: Int? = nil, error: String? = nil) {
        self.blocked = blocked
        self.suspectCount = suspectCount
        self.error = error
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        blocked = try container.decodeIfPresent(Bool.self, forKey: .blocked) ?? false
        suspectCount = try container.decodeIfPresent(Int.self, forKey: .suspectCount)
        error = try container.decodeIfPresent(String.self, forKey: .error)
    }
}

struct CanaryVerifyResponse: Decodable, Equatable {
    let pair: String
    let canary: CanaryCheckStatus
    var extensionScan: SuspectExtensionScanSummary? = nil
    let ok: Bool

    enum CodingKeys: String, CodingKey {
        case pair, canary, ok
        case extensionScan = "extension_scan"
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

struct TimeTravelHistoryResponse: Decodable, Identifiable, Equatable {
    var id: String { "\(pair):\(path)" }
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

// MARK: - Zero-Knowledge Crypt Wizard Models

struct CryptCandidatesResponse: Decodable, Equatable {
    let candidates: [CryptCandidateItem]
}

struct CryptCandidateItem: Decodable, Identifiable, Equatable {
    var id: String { name }
    let name: String
    let type: String
}

struct CryptCreateRequest: Encodable {
    let name: String
    let baseRemote: String
    let basePath: String
    let filenameEncryption: String
    let directoryNameEncryption: Bool
    let password: String?
    let password2: String?

    enum CodingKeys: String, CodingKey {
        case name, password, password2
        case baseRemote = "base_remote"
        case basePath = "base_path"
        case filenameEncryption = "filename_encryption"
        case directoryNameEncryption = "directory_name_encryption"
    }
}

struct CryptKeysPayload: Decodable, Equatable {
    let password: String
    let password2: String
}

struct CryptCreateResponse: Decodable, Equatable {
    let name: String
    let remote: String
    let filenameEncryption: String
    let directoryNameEncryption: Bool
    let keys: CryptKeysPayload
    let warning: String

    enum CodingKeys: String, CodingKey {
        case name, remote, keys, warning
        case filenameEncryption = "filename_encryption"
        case directoryNameEncryption = "directory_name_encryption"
    }
}

struct CryptVerifyResponse: Decodable, Equatable {
    let ok: Bool
}

// MARK: - GFS Versions Lifecycle Models

struct VersionsPreviewResponse: Decodable, Equatable {
    var skipped: Bool? = nil
    var reason: String? = nil
    var dryRun: Bool? = nil
    var aborted: String? = nil
    var roots: [VersionRootPlan] = []
    var warnings: [String] = []

    enum CodingKeys: String, CodingKey {
        case skipped, reason, aborted, roots, warnings
        case dryRun = "dry_run"
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        skipped = try container.decodeIfPresent(Bool.self, forKey: .skipped)
        reason = try container.decodeIfPresent(String.self, forKey: .reason)
        dryRun = try container.decodeIfPresent(Bool.self, forKey: .dryRun)
        aborted = try container.decodeIfPresent(String.self, forKey: .aborted)
        roots = try container.decodeIfPresent([VersionRootPlan].self, forKey: .roots) ?? []
        warnings = try container.decodeIfPresent([String].self, forKey: .warnings) ?? []
    }
}

struct VersionRootPlan: Decodable, Identifiable, Equatable {
    var id: String { root }
    let root: String
    var total: Int = 0
    var keep: [String] = []
    var merge: [VersionMergeItem] = []
    var purge: [String] = []
    var merged: [VersionMergeItem] = []
    var purged: [String] = []
    var failed: [VersionFailureItem] = []
    var error: String? = nil

    enum CodingKeys: String, CodingKey {
        case root, total, keep, merge, purge, merged, purged, failed, error
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        root = try container.decode(String.self, forKey: .root)
        total = try container.decodeIfPresent(Int.self, forKey: .total) ?? 0
        keep = try container.decodeIfPresent([String].self, forKey: .keep) ?? []
        merge = try container.decodeIfPresent([VersionMergeItem].self, forKey: .merge) ?? []
        purge = try container.decodeIfPresent([String].self, forKey: .purge) ?? []
        merged = try container.decodeIfPresent([VersionMergeItem].self, forKey: .merged) ?? []
        purged = try container.decodeIfPresent([String].self, forKey: .purged) ?? []
        failed = try container.decodeIfPresent([VersionFailureItem].self, forKey: .failed) ?? []
        error = try container.decodeIfPresent(String.self, forKey: .error)
    }
}

struct VersionMergeItem: Decodable, Identifiable, Equatable {
    var id: String { "\(from)->\(into)" }
    let into: String
    let from: String
}

struct VersionFailureItem: Decodable, Identifiable, Equatable {
    var id: String { "\(name ?? into ?? "err"):\(error)" }
    var name: String? = nil
    var into: String? = nil
    var from: String? = nil
    let error: String
}

struct VersionsPruneRequest: Encodable {
    let pair: String
    let password: String
}

// MARK: - Bandwidth Heatmap & Smart Schedule Suggestions Models

struct BandwidthHeatmapResponse: Decodable, Equatable {
    let cells: [[Double?]]
    let samples: [[Int]]
    let days: Int
    let timezone: String
}

struct BandwidthSuggestionsResponse: Decodable, Equatable {
    let timezone: String
    let windows: [BandwidthWindowSuggestion]
    let schedules: [BandwidthScheduleProposal]
}

struct BandwidthWindowSuggestion: Decodable, Identifiable, Equatable {
    var id: String { "\(weekday)-\(hour)" }
    let weekday: Int
    let hour: Int
    let score: Double
    let label: String
}

struct BandwidthScheduleProposal: Decodable, Identifiable, Equatable {
    var id: String { definitionID }
    let definitionID: String
    let name: String
    let currentSchedule: String
    let proposedSchedule: String
    let weekday: Int
    let hour: Int
    let reason: String

    enum CodingKeys: String, CodingKey {
        case name, weekday, hour, reason
        case definitionID = "definition_id"
        case currentSchedule = "current_schedule"
        case proposedSchedule = "proposed_schedule"
    }
}
