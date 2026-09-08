import Foundation

enum StorePreviewMode {
    static let launchFlag = "--store-preview"

    static var isLaunchEnabled: Bool {
        ProcessInfo.processInfo.arguments.contains(launchFlag)
    }

    static var initialTab: Int {
        let arguments = ProcessInfo.processInfo.arguments
        guard let flagIndex = arguments.firstIndex(of: launchFlag),
              arguments.indices.contains(flagIndex + 1) else { return 0 }
        switch arguments[flagIndex + 1].lowercased() {
        case "vault", "protect": return 1
        case "recovery", "timeline", "rescue": return 2
        case "paths", "jobs", "runs", "system": return 3
        default: return 0
        }
    }

    static var adminDestination: String? {
        let arguments = ProcessInfo.processInfo.arguments
        guard let index = arguments.firstIndex(of: launchFlag), arguments.indices.contains(index + 1) else { return nil }
        let value = arguments[index + 1].lowercased()
        return ["paths", "jobs", "runs", "system"].contains(value) ? value : nil
    }

    static var opensDeviceVault: Bool {
        let arguments = ProcessInfo.processInfo.arguments
        guard let flagIndex = arguments.firstIndex(of: launchFlag),
              arguments.indices.contains(flagIndex + 1) else { return false }
        return arguments[flagIndex + 1].lowercased() == "vault"
    }
}

struct StorePreviewFixture: Decodable {
    let overview: OverviewResponse
    let storage: StorageOverview
    let config: ConfigSnapshot
    let jobs: [JobRecord]
    let doctor: DoctorResponse
    let progress: BackupProgress
    let pbs: PBSStatus
}

enum StorePreviewData {
    static func load(bundle: Bundle = .main) throws -> StorePreviewFixture {
        guard let url = bundle.url(forResource: "StorePreviewData", withExtension: "json") else {
            throw CocoaError(.fileNoSuchFile)
        }
        return try JSONDecoder().decode(StorePreviewFixture.self, from: Data(contentsOf: url))
    }
}
