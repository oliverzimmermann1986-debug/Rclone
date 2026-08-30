import Foundation
#if canImport(ActivityKit)
import ActivityKit
#endif

let protectionAppGroup = "group.de.oliverzimmermann.rclonesync"

struct ProtectionWidgetSnapshot: Codable {
    let score: Int
    let state: String
    let hostname: String
    let generatedAt: Double
    let activePaths: Int
    let totalPaths: Int
    let quarantines: Int

    static func load() -> ProtectionWidgetSnapshot? {
        guard let data = UserDefaults(suiteName: protectionAppGroup)?.data(forKey: "protectionWidgetSnapshot") else {
            return nil
        }
        return try? JSONDecoder().decode(Self.self, from: data)
    }

    func save() {
        guard let data = try? JSONEncoder().encode(self) else { return }
        UserDefaults(suiteName: protectionAppGroup)?.set(data, forKey: "protectionWidgetSnapshot")
    }
}

#if canImport(ActivityKit)
struct ProtectionActivityAttributes: ActivityAttributes {
    struct ContentState: Codable, Hashable {
        let kind: String
        let pair: String
        let status: String
        let percent: Double?
        let transferred: String?
        let error: String?
    }

    let hostname: String
    let jobID: Int
}
#endif
