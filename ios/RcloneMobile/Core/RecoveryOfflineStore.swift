import CryptoKit
import Foundation

struct RecoveryOfflineStore {
    let storageDirectory: URL
    let defaults: UserDefaults
    private let fileManager: FileManager

    init(
        directory: URL? = nil,
        defaults: UserDefaults = .standard,
        fileManager: FileManager = .default
    ) {
        self.defaults = defaults
        self.fileManager = fileManager
        if let directory {
            self.storageDirectory = directory
        } else {
            let appSupport = fileManager.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
                ?? fileManager.temporaryDirectory
            self.storageDirectory = appSupport.appendingPathComponent("RecoveryOfflineStore", isDirectory: true)
        }
    }

    func key(server: String, username: String) -> String {
        let canonical = (try? APIClient.normalizedServerURL(server).absoluteString) ?? server
        let digest = SHA256.hash(data: Data((canonical + "\0" + username).utf8))
        return "recoveryPass.v2." + digest.map { String(format: "%02x", $0) }.joined()
    }

    func fileURL(forServer server: String, username: String) -> URL {
        storageDirectory.appendingPathComponent(key(server: server, username: username) + ".json", isDirectory: false)
    }

    func save(_ pass: RecoveryPassResponse, server: String, username: String) {
        let entryKey = key(server: server, username: username)
        guard let data = try? JSONEncoder().encode(pass) else { return }
        do {
            try writeProtectedFile(data: data, entryKey: entryKey)
            defaults.removeObject(forKey: entryKey)
            defaults.removeObject(forKey: "offlineRecoveryPass")
        } catch {
            // Fail closed rather than writing sensitive recovery metadata into UserDefaults.
        }
    }

    func load(server: String, username: String) -> RecoveryPassResponse? {
        let entryKey = key(server: server, username: username)
        let target = storageDirectory.appendingPathComponent(entryKey + ".json", isDirectory: false)
        if fileManager.fileExists(atPath: target.path),
           let data = try? Data(contentsOf: target),
           let decoded = try? JSONDecoder().decode(RecoveryPassResponse.self, from: data) {
            defaults.removeObject(forKey: entryKey)
            defaults.removeObject(forKey: "offlineRecoveryPass")
            return decoded
        }

        // Seamless one-time migration from legacy UserDefaults storage.
        guard let legacyData = defaults.data(forKey: entryKey) else { return nil }
        guard let decoded = try? JSONDecoder().decode(RecoveryPassResponse.self, from: legacyData) else {
            defaults.removeObject(forKey: entryKey)
            defaults.removeObject(forKey: "offlineRecoveryPass")
            return nil
        }
        if (try? writeProtectedFile(data: legacyData, entryKey: entryKey)) != nil {
            defaults.removeObject(forKey: entryKey)
            defaults.removeObject(forKey: "offlineRecoveryPass")
        }
        return decoded
    }

    func remove(server: String, username: String) {
        let entryKey = key(server: server, username: username)
        let target = storageDirectory.appendingPathComponent(entryKey + ".json", isDirectory: false)
        try? fileManager.removeItem(at: target)
        defaults.removeObject(forKey: entryKey)
        defaults.removeObject(forKey: "offlineRecoveryPass")
    }

    private func writeProtectedFile(data: Data, entryKey: String) throws {
        try fileManager.createDirectory(
            at: storageDirectory,
            withIntermediateDirectories: true,
            attributes: [
                .posixPermissions: 0o700,
                .protectionKey: FileProtectionType.complete
            ]
        )
        try fileManager.setAttributes([
            .posixPermissions: 0o700,
            .protectionKey: FileProtectionType.complete
        ], ofItemAtPath: storageDirectory.path)
        var excludedDir = storageDirectory
        var dirValues = URLResourceValues()
        dirValues.isExcludedFromBackup = true
        try excludedDir.setResourceValues(dirValues)

        let target = storageDirectory.appendingPathComponent(entryKey + ".json", isDirectory: false)
        try data.write(to: target, options: [.atomic, .completeFileProtection])
        try fileManager.setAttributes([
            .posixPermissions: 0o600,
            .protectionKey: FileProtectionType.complete
        ], ofItemAtPath: target.path)
        var excludedFile = target
        var fileValues = URLResourceValues()
        fileValues.isExcludedFromBackup = true
        try excludedFile.setResourceValues(fileValues)
    }
}
