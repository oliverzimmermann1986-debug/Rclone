import Foundation

struct VaultInboxItem: Codable, Identifiable, Sendable {
    let id: UUID
    let filename: String
    let sourceType: String
    let size: Int64
    let createdAt: Date
}

/// The share extension only stages local bytes. The main app asks for a target.
struct VaultInbox: Sendable {
    static let groupID = "group.de.oliverzimmermann.rclonesync"
    let root: URL

    init(root: URL? = nil) throws {
        if let root { self.root = root; return }
        guard let container = FileManager.default.containerURL(forSecurityApplicationGroupIdentifier: Self.groupID) else {
            throw CocoaError(.fileWriteNoPermission)
        }
        self.root = container.appendingPathComponent("VaultInbox", isDirectory: true)
    }

    func items() throws -> [VaultInboxItem] {
        guard FileManager.default.fileExists(atPath: root.path) else { return [] }
        return try FileManager.default.contentsOfDirectory(at: root, includingPropertiesForKeys: nil)
            .filter { UUID(uuidString: $0.lastPathComponent) != nil }
            .compactMap { folder -> VaultInboxItem? in
                guard let bytes = try? Data(contentsOf: folder.appendingPathComponent("item.json")),
                      let item = try? JSONDecoder().decode(VaultInboxItem.self, from: bytes),
                      item.id.uuidString == folder.lastPathComponent else { return nil }
                return item
            }.sorted { $0.createdAt < $1.createdAt }
    }

    func stage(_ source: URL, filename: String, sourceType: String) throws -> VaultInboxItem {
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true,
            attributes: [.protectionKey: FileProtectionType.complete])
        var rootURL = root
        var values = URLResourceValues()
        values.isExcludedFromBackup = true
        try rootURL.setResourceValues(values)
        let id = UUID()
        let pending = root.appendingPathComponent(".pending-\(id.uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: pending, withIntermediateDirectories: false,
            attributes: [.protectionKey: FileProtectionType.complete])
        defer { try? FileManager.default.removeItem(at: pending) }
        let access = source.startAccessingSecurityScopedResource()
        defer { if access { source.stopAccessingSecurityScopedResource() } }
        let target = pending.appendingPathComponent("payload")
        try FileManager.default.copyItem(at: source, to: target)
        try FileManager.default.setAttributes([.protectionKey: FileProtectionType.complete], ofItemAtPath: target.path)
        let attributes = try target.resourceValues(forKeys: [.fileSizeKey, .isRegularFileKey, .isSymbolicLinkKey])
        guard attributes.isRegularFile == true, attributes.isSymbolicLink != true,
              let size = attributes.fileSize, size > 0 else { throw CocoaError(.fileReadCorruptFile) }
        let item = VaultInboxItem(id: id, filename: URL(fileURLWithPath: filename).lastPathComponent,
            sourceType: sourceType, size: Int64(size), createdAt: Date())
        try JSONEncoder().encode(item).write(to: pending.appendingPathComponent("item.json"), options: [.atomic, .completeFileProtection])
        try FileManager.default.moveItem(at: pending, to: root.appendingPathComponent(id.uuidString, isDirectory: true))
        return item
    }

    func payload(for item: VaultInboxItem) -> URL {
        root.appendingPathComponent(item.id.uuidString, isDirectory: true).appendingPathComponent("payload")
    }

    func remove(_ item: VaultInboxItem) throws {
        try FileManager.default.removeItem(at: root.appendingPathComponent(item.id.uuidString, isDirectory: true))
    }
}
