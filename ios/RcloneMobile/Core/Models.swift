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
    let nextRun: Double?
    let lastStatus: String?
    let lastRun: Double?
    let jobID: Int?
    let overdue: Bool?
    let error: String?

    enum CodingKeys: String, CodingKey {
        case name, direction, mode, schedule, overdue, error
        case historyKey = "history_key"
        case nextRun = "next_run"
        case lastStatus = "last_status"
        case lastRun = "last_run"
        case jobID = "job_id"
    }
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
}

struct ConfigSnapshot: Decodable {
    let revision: String
    let backup: BackupConfig

    enum CodingKeys: String, CodingKey {
        case revision = "_revision"
        case backup
    }
}

struct BackupConfig: Decodable {
    let enabled: Bool?
    let timezone: String?
    let defaultSchedule: String?
    let pairs: [PairConfig]

    enum CodingKeys: String, CodingKey {
        case enabled, timezone, pairs
        case defaultSchedule = "default_schedule"
    }
}

struct PairConfig: Decodable, Identifiable {
    var id: String { name }
    let name: String
    let local: String
    let remote: String
    let direction: String?
    let mode: String?
    let enabled: Bool?
    let allowDelete: Bool?

    enum CodingKeys: String, CodingKey {
        case name, local, remote, direction, mode, enabled
        case allowDelete = "allow_delete"
    }
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
