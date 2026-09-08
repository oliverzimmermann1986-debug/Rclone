import CryptoKit
import Foundation

struct RecoveryOfflineStore {
    let defaults: UserDefaults
    init(defaults: UserDefaults = .standard) { self.defaults = defaults }

    func key(server: String, username: String) -> String {
        let canonical = (try? APIClient.normalizedServerURL(server).absoluteString) ?? server
        let digest = SHA256.hash(data: Data((canonical + "\0" + username).utf8))
        return "recoveryPass.v2." + digest.map { String(format: "%02x", $0) }.joined()
    }

    func save(_ pass: RecoveryPassResponse, server: String, username: String) {
        guard let data = try? JSONEncoder().encode(pass) else { return }
        defaults.set(data, forKey: key(server: server, username: username))
        defaults.removeObject(forKey: "offlineRecoveryPass")
    }

    func load(server: String, username: String) -> RecoveryPassResponse? {
        guard let data = defaults.data(forKey: key(server: server, username: username)) else { return nil }
        return try? JSONDecoder().decode(RecoveryPassResponse.self, from: data)
    }

    func remove(server: String, username: String) {
        defaults.removeObject(forKey: key(server: server, username: username))
    }
}
