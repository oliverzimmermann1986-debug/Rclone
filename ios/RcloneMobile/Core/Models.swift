import Foundation

struct OverviewResponse: Decodable {
    let app: AppInfo
    let system: SystemSnapshot
    let services: ServicesSnapshot
    let pairs: PairSummary
    let jobs: JobOverview
    let alerts: [SystemAlert]
    let generatedAt: Double

    enum CodingKeys: String, CodingKey {
        case app, system, services, pairs, jobs, alerts
        case generatedAt = "generated_at"
    }
}

struct AppInfo: Decodable {
    let version: String
    let timezone: String
}

struct ServicesSnapshot: Decodable {
    let web: ServiceState
    let scheduler: SchedulerServiceState
}

struct ServiceState: Decodable {
    let enabled: String?
    let active: String?
}

struct SchedulerServiceState: Decodable {
    let enabled: String?
    let active: String?
    let configuredEnabled: Bool
    let control: SchedulerControl?

    enum CodingKeys: String, CodingKey {
        case enabled, active, control
        case configuredEnabled = "configured_enabled"
    }
}

struct SchedulerControl: Codable {
    let paused: Bool?
    let until: Double?
    let reason: String?
    let actor: String?
}

struct SchedulerPauseRequest: Encodable {
    let minutes: Int
    let reason = "Wartungsfenster per iPhone"
}

struct PairSummary: Decodable {
    let total: Int
    let enabled: Int
    let scheduled: Int
    let manual: Int
    let destructive: Int
    let health: [PairHealth]
}

struct PairHealth: Decodable, Identifiable {
    var id: String { historyKey }
    let name: String
    let historyKey: String
    let direction: String
    let mode: String
    let schedule: String
    let jobs: [PairJobAssignment]
    let nextRun: Double?
    let lastStatus: String?
    let lastRun: Double?
    let jobID: Int?
    let overdue: Bool?
    let error: String?

    enum CodingKeys: String, CodingKey {
        case name, direction, mode, schedule, jobs, overdue, error
        case historyKey = "history_key"
        case nextRun = "next_run"
        case lastStatus = "last_status"
        case lastRun = "last_run"
        case jobID = "job_id"
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        name = try values.decode(String.self, forKey: .name)
        historyKey = try values.decode(String.self, forKey: .historyKey)
        direction = try values.decode(String.self, forKey: .direction)
        mode = try values.decode(String.self, forKey: .mode)
        jobs = try values.decodeIfPresent([PairJobAssignment].self, forKey: .jobs) ?? []
        schedule = try values.decodeIfPresent(String.self, forKey: .schedule)
            ?? jobs.first?.schedule
            ?? "manual"
        nextRun = try values.decodeIfPresent(Double.self, forKey: .nextRun)
        lastStatus = try values.decodeIfPresent(String.self, forKey: .lastStatus)
        lastRun = try values.decodeIfPresent(Double.self, forKey: .lastRun)
        jobID = try values.decodeIfPresent(Int.self, forKey: .jobID)
        overdue = try values.decodeIfPresent(Bool.self, forKey: .overdue)
        error = try values.decodeIfPresent(String.self, forKey: .error)
    }
}

struct PairJobAssignment: Decodable, Identifiable {
    let id: String
    let name: String
    let schedule: String
}

struct JobOverview: Decodable {
    let last: JobRecord?
    let lastSuccess: JobRecord?
    let lastError: JobRecord?

    enum CodingKeys: String, CodingKey {
        case last
        case lastSuccess = "last_success"
        case lastError = "last_error"
    }
}

struct SystemAlert: Decodable, Identifiable {
    var id: String { level + message }
    let level: String
    let message: String
}

struct SystemSnapshot: Decodable {
    let hostname: String
    let platform: String
    let kernel: String
    let python: String
    let virtualization: String
    let addresses: [String]
    let uptimeSeconds: Double
    let cpu: CPUMetrics
    let memory: MemoryMetrics
    let pids: PIDMetrics
    let dataDisk: DiskMetrics

    enum CodingKeys: String, CodingKey {
        case hostname, platform, kernel, python, virtualization, addresses, cpu, memory, pids
        case uptimeSeconds = "uptime_seconds"
        case dataDisk = "data_disk"
    }
}

struct CPUMetrics: Decodable {
    let count: Int
    let capacity: Double
    let source: String
    let load1: Double
    let load5: Double
    let load15: Double
    let loadPercent: Double?

    enum CodingKeys: String, CodingKey {
        case count, capacity, source
        case load1 = "load_1"
        case load5 = "load_5"
        case load15 = "load_15"
        case loadPercent = "load_percent"
    }
}

struct MemoryMetrics: Decodable {
    let totalBytes: Int64?
    let availableBytes: Int64?
    let usedBytes: Int64?
    let percentUsed: Double?
    let source: String?

    enum CodingKeys: String, CodingKey {
        case source
        case totalBytes = "total_bytes"
        case availableBytes = "available_bytes"
        case usedBytes = "used_bytes"
        case percentUsed = "percent_used"
    }
}

struct PIDMetrics: Decodable {
    let current: Int
    let max: Int?
    let percentUsed: Double?

    enum CodingKeys: String, CodingKey {
        case current, max
        case percentUsed = "percent_used"
    }
}

struct DiskMetrics: Decodable {
    let path: String
    let totalBytes: Int64?
    let usedBytes: Int64?
    let freeBytes: Int64?
    let percentUsed: Double?
    let error: String?

    enum CodingKeys: String, CodingKey {
        case path, error
        case totalBytes = "total_bytes"
        case usedBytes = "used_bytes"
        case freeBytes = "free_bytes"
        case percentUsed = "percent_used"
    }
}

struct StorageOverview: Decodable {
    let pairs: [StoragePair]
}

struct StoragePair: Decodable, Identifiable {
    var id: String { name }
    let name: String
    let local: String
    let remote: String?
    let direction: String
    let source: String
    let target: String
    let localDisk: LocalDisk?
    let lastSync: Double?
    let lastTransferred: Int64?
    let sourceSize: PathSize?
    let targetSize: PathSize?

    enum CodingKeys: String, CodingKey {
        case name, local, remote, direction, source, target
        case localDisk = "local_disk"
        case lastSync = "last_sync"
        case lastTransferred = "last_transferred"
        case sourceSize = "source_size"
        case targetSize = "target_size"
    }
}

struct LocalDisk: Decodable {
    let path: String?
    let exists: Bool?
    let totalBytes: Int64?
    let freeBytes: Int64?
    let percentUsed: Double?

    enum CodingKeys: String, CodingKey {
        case path, exists
        case totalBytes = "total_bytes"
        case freeBytes = "free_bytes"
        case percentUsed = "percent_used"
    }
}

struct PathSize: Decodable {
    let path: String?
    let count: Int?
    let bytes: Int64?
    let error: String?
    let measuredAt: Double?
    let measurementStatus: String?
    let measurementError: String?

    enum CodingKeys: String, CodingKey {
        case path, count, bytes, error
        case measuredAt = "measured_at"
        case measurementStatus = "measurement_status"
        case measurementError = "measurement_error"
    }
}

struct LogoutResult: Decodable, Equatable {
    let globalRevocation: Bool
    let localSessionCleared: Bool
    let detail: String?

    enum CodingKeys: String, CodingKey {
        case detail
        case globalRevocation = "global_revocation"
        case localSessionCleared = "local_session_cleared"
    }
}

private struct DynamicCodingKey: CodingKey {
    let stringValue: String
    let intValue: Int?

    init?(stringValue: String) {
        self.stringValue = stringValue
        intValue = nil
    }

    init?(intValue: Int) {
        stringValue = String(intValue)
        self.intValue = intValue
    }
}

enum JSONValue: Codable, Equatable {
    case string(String)
    case number(Double)
    case bool(Bool)
    case object([String: JSONValue])
    case array([JSONValue])
    case null

    init(from decoder: Decoder) throws {
        let value = try decoder.singleValueContainer()
        if value.decodeNil() { self = .null }
        else if let decoded = try? value.decode(Bool.self) { self = .bool(decoded) }
        else if let decoded = try? value.decode(Double.self) { self = .number(decoded) }
        else if let decoded = try? value.decode(String.self) { self = .string(decoded) }
        else if let decoded = try? value.decode([String: JSONValue].self) { self = .object(decoded) }
        else if let decoded = try? value.decode([JSONValue].self) { self = .array(decoded) }
        else { throw DecodingError.dataCorruptedError(in: value, debugDescription: "Unbekannter JSON-Wert") }
    }

    func encode(to encoder: Encoder) throws {
        var value = encoder.singleValueContainer()
        switch self {
        case let .string(decoded): try value.encode(decoded)
        case let .number(decoded): try value.encode(decoded)
        case let .bool(decoded): try value.encode(decoded)
        case let .object(decoded): try value.encode(decoded)
        case let .array(decoded): try value.encode(decoded)
        case .null: try value.encodeNil()
        }
    }
}

struct ConfigSnapshot: Codable {
    let revision: String
    let backup: BackupConfig
    private let extraSections: [String: JSONValue]

    init(revision: String, backup: BackupConfig, extraSections: [String: JSONValue] = [:]) {
        self.revision = revision
        self.backup = backup
        self.extraSections = extraSections
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: DynamicCodingKey.self)
        revision = try values.decode(String.self, forKey: DynamicCodingKey(stringValue: "_revision")!)
        backup = try values.decode(BackupConfig.self, forKey: DynamicCodingKey(stringValue: "backup")!)
        var extras: [String: JSONValue] = [:]
        for key in values.allKeys where !["_revision", "backup"].contains(key.stringValue) {
            extras[key.stringValue] = try values.decode(JSONValue.self, forKey: key)
        }
        extraSections = extras
    }

    func encode(to encoder: Encoder) throws {
        var values = encoder.container(keyedBy: DynamicCodingKey.self)
        for (name, value) in extraSections {
            try values.encode(value, forKey: DynamicCodingKey(stringValue: name)!)
        }
        try values.encode(revision, forKey: DynamicCodingKey(stringValue: "_revision")!)
        try values.encode(backup, forKey: DynamicCodingKey(stringValue: "backup")!)
    }

    func replacing(pairs: [PairConfig], jobs: [JobDefinition]) -> ConfigSnapshot {
        ConfigSnapshot(
            revision: revision,
            backup: backup.replacing(pairs: pairs, jobs: jobs),
            extraSections: extraSections
        )
    }
}

struct BackupConfig: Codable {
    let enabled: Bool?
    let timezone: String?
    let defaultSchedule: String?
    let pairs: [PairConfig]
    let jobs: [JobDefinition]
    private let extras: [String: JSONValue]

    init(
        enabled: Bool?,
        timezone: String?,
        defaultSchedule: String?,
        pairs: [PairConfig],
        jobs: [JobDefinition] = [],
        extras: [String: JSONValue] = [:]
    ) {
        self.enabled = enabled
        self.timezone = timezone
        self.defaultSchedule = defaultSchedule
        self.pairs = pairs
        self.jobs = jobs
        self.extras = extras
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: DynamicCodingKey.self)
        func key(_ value: String) -> DynamicCodingKey { DynamicCodingKey(stringValue: value)! }
        enabled = try values.decodeIfPresent(Bool.self, forKey: key("enabled"))
        timezone = try values.decodeIfPresent(String.self, forKey: key("timezone"))
        defaultSchedule = try values.decodeIfPresent(String.self, forKey: key("default_schedule"))
        pairs = try values.decodeIfPresent([PairConfig].self, forKey: key("pairs")) ?? []
        jobs = try values.decodeIfPresent([JobDefinition].self, forKey: key("jobs")) ?? []
        let known = Set(["enabled", "timezone", "default_schedule", "pairs", "jobs"])
        var preserved: [String: JSONValue] = [:]
        for codingKey in values.allKeys where !known.contains(codingKey.stringValue) {
            preserved[codingKey.stringValue] = try values.decode(JSONValue.self, forKey: codingKey)
        }
        extras = preserved
    }

    func encode(to encoder: Encoder) throws {
        var values = encoder.container(keyedBy: DynamicCodingKey.self)
        func key(_ value: String) -> DynamicCodingKey { DynamicCodingKey(stringValue: value)! }
        for (name, value) in extras { try values.encode(value, forKey: key(name)) }
        try values.encodeIfPresent(enabled, forKey: key("enabled"))
        try values.encodeIfPresent(timezone, forKey: key("timezone"))
        try values.encodeIfPresent(defaultSchedule, forKey: key("default_schedule"))
        try values.encode(pairs, forKey: key("pairs"))
        try values.encode(jobs, forKey: key("jobs"))
    }

    func replacing(pairs: [PairConfig], jobs: [JobDefinition]) -> BackupConfig {
        BackupConfig(
            enabled: enabled,
            timezone: timezone,
            defaultSchedule: defaultSchedule,
            pairs: pairs,
            jobs: jobs,
            extras: extras
        )
    }
}

struct PairConfig: Codable, Identifiable {
    let stableID: String?
    var id: String { stableID ?? name }
    let name: String
    let local: String
    let remote: String
    let direction: String
    let mode: String
    let enabled: Bool
    let allowDelete: Bool
    let maxDelete: Int?
    let backupDir: String
    let minLocalFiles: Int
    let minRemoteFiles: Int
    let requireMountpoint: Bool
    let mountpoint: String
    let sentinelFile: String
    private let extras: [String: JSONValue]

    init(
        stableID: String?, name: String, local: String, remote: String,
        direction: String = "push", mode: String = "copy", enabled: Bool = true,
        allowDelete: Bool = false, maxDelete: Int? = nil, backupDir: String = "",
        minLocalFiles: Int = 1, minRemoteFiles: Int = 0,
        requireMountpoint: Bool = false, mountpoint: String = "", sentinelFile: String = "",
        extras: [String: JSONValue] = [:]
    ) {
        self.stableID = stableID
        self.name = name
        self.local = local
        self.remote = remote
        self.direction = direction
        self.mode = mode
        self.enabled = enabled
        self.allowDelete = allowDelete
        self.maxDelete = maxDelete
        self.backupDir = backupDir
        self.minLocalFiles = minLocalFiles
        self.minRemoteFiles = minRemoteFiles
        self.requireMountpoint = requireMountpoint
        self.mountpoint = mountpoint
        self.sentinelFile = sentinelFile
        self.extras = extras
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: DynamicCodingKey.self)
        func key(_ value: String) -> DynamicCodingKey { DynamicCodingKey(stringValue: value)! }
        stableID = try values.decodeIfPresent(String.self, forKey: key("id"))
        name = try values.decode(String.self, forKey: key("name"))
        local = try values.decode(String.self, forKey: key("local"))
        remote = try values.decode(String.self, forKey: key("remote"))
        direction = try values.decodeIfPresent(String.self, forKey: key("direction")) ?? "push"
        mode = try values.decodeIfPresent(String.self, forKey: key("mode")) ?? (direction == "bisync" ? "bisync" : "copy")
        enabled = try values.decodeIfPresent(Bool.self, forKey: key("enabled")) ?? true
        allowDelete = try values.decodeIfPresent(Bool.self, forKey: key("allow_delete")) ?? false
        maxDelete = try values.decodeIfPresent(Int.self, forKey: key("max_delete"))
        backupDir = try values.decodeIfPresent(String.self, forKey: key("backup_dir")) ?? ""
        minLocalFiles = try values.decodeIfPresent(Int.self, forKey: key("min_local_files")) ?? 1
        minRemoteFiles = try values.decodeIfPresent(Int.self, forKey: key("min_remote_files")) ?? 0
        requireMountpoint = try values.decodeIfPresent(Bool.self, forKey: key("require_mountpoint")) ?? false
        mountpoint = try values.decodeIfPresent(String.self, forKey: key("mountpoint")) ?? ""
        sentinelFile = try values.decodeIfPresent(String.self, forKey: key("sentinel_file")) ?? ""
        let known = Set(["id", "name", "local", "remote", "direction", "mode", "enabled", "allow_delete", "max_delete", "backup_dir", "min_local_files", "min_remote_files", "require_mountpoint", "mountpoint", "sentinel_file"])
        var preserved: [String: JSONValue] = [:]
        for codingKey in values.allKeys where !known.contains(codingKey.stringValue) {
            preserved[codingKey.stringValue] = try values.decode(JSONValue.self, forKey: codingKey)
        }
        extras = preserved
    }

    func encode(to encoder: Encoder) throws {
        var values = encoder.container(keyedBy: DynamicCodingKey.self)
        func key(_ value: String) -> DynamicCodingKey { DynamicCodingKey(stringValue: value)! }
        for (name, value) in extras { try values.encode(value, forKey: key(name)) }
        try values.encodeIfPresent(stableID, forKey: key("id"))
        try values.encode(name, forKey: key("name"))
        try values.encode(local, forKey: key("local"))
        try values.encode(remote, forKey: key("remote"))
        try values.encode(direction, forKey: key("direction"))
        try values.encode(mode, forKey: key("mode"))
        try values.encode(enabled, forKey: key("enabled"))
        try values.encode(allowDelete, forKey: key("allow_delete"))
        try values.encodeIfPresent(maxDelete, forKey: key("max_delete"))
        try values.encode(backupDir, forKey: key("backup_dir"))
        try values.encode(minLocalFiles, forKey: key("min_local_files"))
        try values.encode(minRemoteFiles, forKey: key("min_remote_files"))
        try values.encode(requireMountpoint, forKey: key("require_mountpoint"))
        try values.encode(mountpoint, forKey: key("mountpoint"))
        try values.encode(sentinelFile, forKey: key("sentinel_file"))
    }

    func replacing(
        name: String, local: String, remote: String, direction: String, mode: String,
        enabled: Bool, allowDelete: Bool, maxDelete: Int?, backupDir: String,
        minLocalFiles: Int, minRemoteFiles: Int, requireMountpoint: Bool,
        mountpoint: String, sentinelFile: String
    ) -> PairConfig {
        PairConfig(
            stableID: stableID,
            name: name,
            local: local,
            remote: remote,
            direction: direction,
            mode: mode,
            enabled: enabled,
            allowDelete: allowDelete,
            maxDelete: maxDelete,
            backupDir: backupDir,
            minLocalFiles: minLocalFiles,
            minRemoteFiles: minRemoteFiles,
            requireMountpoint: requireMountpoint,
            mountpoint: mountpoint,
            sentinelFile: sentinelFile,
            extras: extras
        )
    }
}

struct JobDefinition: Codable, Identifiable {
    let id: String
    let name: String
    let enabled: Bool
    let dataPathIDs: [String]
    let schedule: String
    let executionMode: String
    let maxParallel: Int
    let retryMinutes: Int

    enum CodingKeys: String, CodingKey {
        case id, name, enabled, schedule
        case dataPathIDs = "data_path_ids"
        case executionMode = "execution_mode"
        case maxParallel = "max_parallel"
        case retryMinutes = "retry_minutes"
    }
}

struct ConfigUpdateRequest: Encodable {
    let config: ConfigSnapshot
    let currentPassword: String?

    enum CodingKeys: String, CodingKey {
        case config
        case currentPassword = "current_password"
    }
}

struct ConfigSaveResponse: Decodable {
    let ok: Bool
    let warnings: [String]
    let config: ConfigSnapshot
}

struct JobPlan: Decodable {
    let ok: Bool
    let dryRun: Bool
    let totalPairs: Int
    let pairs: [JobPlanPair]
    let warnings: [String]
    let definitionID: String?
    let definitionName: String?

    enum CodingKeys: String, CodingKey {
        case ok, pairs, warnings
        case dryRun = "dry_run"
        case totalPairs = "total_pairs"
        case definitionID = "definition_id"
        case definitionName = "definition_name"
    }
}

struct JobPlanPair: Decodable, Identifiable {
    var id: String { name }
    let name: String
    let enabled: Bool?
    let direction: String?
    let mode: String?
    let command: String?
    let warnings: [String]?
    let error: String?
}

struct JobSearchResponse: Decodable {
    let items: [JobRecord]
    let total: Int
    let limit: Int
    let offset: Int
}

struct JobRecord: Decodable, Identifiable {
    let id: Int
    let kind: String
    let status: String
    let startedAt: Double
    let endedAt: Double?
    let logFile: String?

    enum CodingKeys: String, CodingKey {
        case id, kind, status
        case startedAt = "started_at"
        case endedAt = "ended_at"
        case logFile = "log_file"
    }
}

struct JobLogResponse: Decodable {
    let log: String
}

struct DoctorResponse: Decodable {
    let ok: Bool
    let level: String
    let checks: [DoctorCheck]
    let generatedAt: Double

    enum CodingKeys: String, CodingKey {
        case ok, level, checks
        case generatedAt = "generated_at"
    }
}

struct DoctorCheck: Decodable, Identifiable {
    var id: String { (name ?? title ?? "Prüfung") + (message ?? detail ?? "") }
    let name: String?
    let title: String?
    let level: String?
    let message: String?
    let detail: String?
}

struct ActionResponse: Decodable {
    let ok: Bool
    let jobID: Int?
    let error: String?

    enum CodingKeys: String, CodingKey {
        case ok, error
        case jobID = "job_id"
    }
}

struct BackupProgress: Decodable {
    let running: Bool
    let jobID: Int?
    let startedAt: Double?
    let elapsedSeconds: Int?
    let pairs: [BackupPairProgress]?
    let totalPairs: Int?
    let donePairs: Int?
    let last: JobRecord?

    enum CodingKeys: String, CodingKey {
        case running, pairs, last
        case jobID = "job_id"
        case startedAt = "started_at"
        case elapsedSeconds = "elapsed_sec"
        case totalPairs = "total_pairs"
        case donePairs = "done_pairs"
    }
}

struct BackupPairProgress: Decodable, Identifiable {
    var id: String { name }
    let name: String
    let status: String
    let transferred: String?
    let total: String?
    let percent: Double?
    let speed: String?
    let eta: String?
    let error: String?
}

struct PBSStatus: Decodable {
    let enabled: Bool
    let clientAvailable: Bool
    let repository: String
    let namespace: String
    let running: Bool
    let runningJob: JobRecord?
    let targets: [PBSTarget]

    enum CodingKeys: String, CodingKey {
        case enabled, repository, namespace, running, targets
        case clientAvailable = "client_available"
        case runningJob = "running_job"
    }
}

struct PBSTarget: Decodable, Identifiable {
    var id: String { name }
    let name: String
    let paths: [String]
    let schedule: String
    let namespace: String
    let lastSuccess: Double?
    let nextRun: Double?

    enum CodingKeys: String, CodingKey {
        case name, paths, schedule, namespace
        case lastSuccess = "last_success"
        case nextRun = "next_run"
    }
}

struct PBSRunRequest: Encodable {
    let target: String?
}
