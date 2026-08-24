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
        case "paths": return 1
        case "jobs": return 2
        case "runs": return 3
        case "system": return 4
        default: return 0
        }
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
