import CryptoKit
import Foundation

struct VaultQueueScope: Codable, Equatable, Sendable {
    let server: String
    let username: String
    let pairID: String
    let source: String
    let target: String
    let direction: String

    init(serverURL: URL, username: String, pairID: String, source: String, target: String, direction: String) {
        var components = URLComponents(url: serverURL, resolvingAgainstBaseURL: false)
        let normalizedScheme = components?.scheme?.lowercased()
        let normalizedHost = components?.host?.lowercased()
        components?.scheme = normalizedScheme
        components?.host = normalizedHost
        components?.user = nil
        components?.password = nil
        components?.query = nil
        components?.fragment = nil
        if (components?.scheme == "https" && components?.port == 443)
            || (components?.scheme == "http" && components?.port == 80) {
            components?.port = nil
        }
        server = (components?.string ?? serverURL.absoluteString)
            .trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        self.username = username
        self.pairID = pairID
        self.source = source
        self.target = target
        self.direction = direction
    }

    var key: String {
        let encoded = try? JSONEncoder().encode([server, username, pairID, source, target, direction])
        return SHA256.hash(data: encoded ?? Data()).map { String(format: "%02x", $0) }.joined()
    }

    var accountKey: String {
        SHA256.hash(data: (try? JSONEncoder().encode([server, username])) ?? Data())
            .map { String(format: "%02x", $0) }.joined()
    }

    var destinationDescription: String { direction == "push" ? target : source }
}

struct VaultQueueEntry: Codable, Identifiable, Sendable {
    let id: UUID
    let scopeKey: String
    let pairID: String
    let filename: String
    let sourceType: String
    let size: Int64
    let sha256: String
    let createdAt: Date
    let accountKey: String?
    let destinationDescription: String?
    var uploadID: String?
    var received: Int64 = 0
    var state = "waiting"
    var lastError: String?
}

/// Entries commit atomically and keep their payload until the target is verified.
/// No passwords, cookies or keys are persisted in the queue or shared extension.
struct VaultQueueStore: Sendable {
    let root: URL

    init(root: URL? = nil) {
        self.root = root ?? FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("VaultQueue", isDirectory: true)
    }

    func entries(for scope: VaultQueueScope) throws -> [VaultQueueEntry] {
        let directory = root.appendingPathComponent(scope.key, isDirectory: true)
        guard FileManager.default.fileExists(atPath: directory.path) else { return [] }
        return try FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil)
            .filter { UUID(uuidString: $0.lastPathComponent) != nil }
            .compactMap { folder -> VaultQueueEntry? in
                guard let data = try? Data(contentsOf: folder.appendingPathComponent("entry.json")),
                      let entry = try? JSONDecoder().decode(VaultQueueEntry.self, from: data),
                      entry.id.uuidString == folder.lastPathComponent,
                      entry.scopeKey == scope.key, entry.pairID == scope.pairID else { return nil }
                return entry
            }
            .sorted { $0.createdAt < $1.createdAt }
    }

    func stage(_ source: URL, filename: String, sourceType: String, scope: VaultQueueScope, id: UUID = UUID()) throws -> VaultQueueEntry {
        if let existing = try entries(for: scope).first(where: { $0.id == id }) { return existing }
        let parent = root.appendingPathComponent(scope.key, isDirectory: true)
        try makeProtectedDirectory(parent)
        let pending = parent.appendingPathComponent(".pending-\(UUID().uuidString)", isDirectory: true)
        try makeProtectedDirectory(pending)
        defer { try? FileManager.default.removeItem(at: pending) }
        let scoped = source.startAccessingSecurityScopedResource()
        defer { if scoped { source.stopAccessingSecurityScopedResource() } }
        let payload = pending.appendingPathComponent("payload")
        try FileManager.default.copyItem(at: source, to: payload)
        try FileManager.default.setAttributes([.protectionKey: FileProtectionType.complete], ofItemAtPath: payload.path)
        let values = try payload.resourceValues(forKeys: [.fileSizeKey, .isRegularFileKey, .isSymbolicLinkKey])
        guard values.isRegularFile == true, values.isSymbolicLink != true, let size = values.fileSize, size > 0 else {
            throw CocoaError(.fileReadCorruptFile)
        }
        let entry = VaultQueueEntry(id: id, scopeKey: scope.key, pairID: scope.pairID,
            filename: URL(fileURLWithPath: filename).lastPathComponent, sourceType: sourceType,
            size: Int64(size), sha256: try Self.digest(payload), createdAt: Date(),
            accountKey: scope.accountKey, destinationDescription: scope.destinationDescription)
        try JSONEncoder().encode(entry).write(to: pending.appendingPathComponent("entry.json"), options: [.atomic, .completeFileProtection])
        try FileManager.default.moveItem(at: pending, to: parent.appendingPathComponent(id.uuidString, isDirectory: true))
        return entry
    }

    func save(_ entry: VaultQueueEntry) throws {
        let folder = try directory(for: entry)
        guard FileManager.default.fileExists(atPath: folder.path) else { throw CocoaError(.fileNoSuchFile) }
        try JSONEncoder().encode(entry).write(to: folder.appendingPathComponent("entry.json"), options: [.atomic, .completeFileProtection])
    }

    func unassignedEntries(for scope: VaultQueueScope, activeScopeKeys: Set<String>) throws -> [VaultQueueEntry] {
        guard FileManager.default.fileExists(atPath: root.path) else { return [] }
        let scopes = try FileManager.default.contentsOfDirectory(at: root, includingPropertiesForKeys: nil)
        var result: [VaultQueueEntry] = []
        for directory in scopes where !activeScopeKeys.contains(directory.lastPathComponent) {
            guard directory.lastPathComponent.count == 64,
                  directory.lastPathComponent.allSatisfy({ $0.isHexDigit }) else { continue }
            for folder in try FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil) {
                guard UUID(uuidString: folder.lastPathComponent) != nil,
                      let data = try? Data(contentsOf: folder.appendingPathComponent("entry.json")),
                      let entry = try? JSONDecoder().decode(VaultQueueEntry.self, from: data),
                      entry.id.uuidString == folder.lastPathComponent,
                      entry.scopeKey == directory.lastPathComponent,
                      entry.accountKey == scope.accountKey else { continue }
                result.append(entry)
            }
        }
        return result.sorted { $0.createdAt < $1.createdAt }
    }

    func reassign(_ entry: VaultQueueEntry, to scope: VaultQueueScope) throws {
        guard entry.accountKey == scope.accountKey, entry.scopeKey != scope.key else {
            throw CocoaError(.fileWriteNoPermission)
        }
        let source = try payload(for: entry)
        guard try Self.digest(source) == entry.sha256 else { throw CocoaError(.fileReadCorruptFile) }
        // Commit fresh metadata with NO previous server upload ID. Only then
        // remove the prior local queue copy; the old remote upload is untouched.
        _ = try stage(source, filename: entry.filename, sourceType: entry.sourceType, scope: scope, id: entry.id)
        try remove(entry)
    }

    func export(_ entry: VaultQueueEntry) throws -> URL {
        let source = try payload(for: entry)
        guard try Self.digest(source) == entry.sha256 else { throw CocoaError(.fileReadCorruptFile) }
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("vault-export-\(UUID().uuidString)", isDirectory: true)
        try makeProtectedDirectory(directory)
        let target = directory.appendingPathComponent(URL(fileURLWithPath: entry.filename).lastPathComponent)
        try FileManager.default.copyItem(at: source, to: target)
        try FileManager.default.setAttributes([.protectionKey: FileProtectionType.complete], ofItemAtPath: target.path)
        return target
    }

    func payload(for entry: VaultQueueEntry) throws -> URL { try directory(for: entry).appendingPathComponent("payload") }

    func remove(_ entry: VaultQueueEntry) throws { try FileManager.default.removeItem(at: directory(for: entry)) }

    private func directory(for entry: VaultQueueEntry) throws -> URL {
        guard entry.scopeKey.count == 64, entry.scopeKey.allSatisfy({ $0.isHexDigit }) else { throw CocoaError(.fileReadCorruptFile) }
        return root.appendingPathComponent(entry.scopeKey, isDirectory: true).appendingPathComponent(entry.id.uuidString, isDirectory: true)
    }

    private func makeProtectedDirectory(_ directory: URL) throws {
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true,
            attributes: [.protectionKey: FileProtectionType.complete])
        var protected = directory
        var values = URLResourceValues()
        values.isExcludedFromBackup = true
        try protected.setResourceValues(values)
    }

    static func digest(_ url: URL) throws -> String {
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        var hasher = SHA256()
        while let data = try handle.read(upToCount: 1024 * 1024), !data.isEmpty { hasher.update(data: data) }
        return hasher.finalize().map { String(format: "%02x", $0) }.joined()
    }
}
