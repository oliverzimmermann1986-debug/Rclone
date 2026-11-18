import Foundation

struct VaultInboxItem: Codable, Identifiable, Sendable {
    let id: UUID
    let filename: String
    let sourceType: String
    let size: Int64
    let createdAt: Date
}

enum VaultInboxError: LocalizedError, Equatable, Sendable {
    case inboxQuotaExceeded(limitBytes: Int64)
    case insufficientDiskSpace

    var errorDescription: String? {
        switch self {
        case let .inboxQuotaExceeded(limitBytes):
            let formatted = ByteCountFormatter.string(fromByteCount: limitBytes, countStyle: .file)
            return "Der Zwischenspeicher für geteilte Dateien ist voll (maximal \(formatted)). Öffne Sicherpfad und übernimm zuerst die vorgemerkten Dateien."
        case .insufficientDiskSpace:
            return "Nicht genügend freier Gerätespeicher verfügbar, um diese Datei lokal vorzumerken."
        }
    }
}

/// The share extension only stages local bytes. The main app asks for a target.
struct VaultInbox: Sendable {
    static let groupID = "group.de.oliverzimmermann.rclonesync"
    static let defaultMaxInboxBytes: Int64 = 4 * 1024 * 1024 * 1024
    static let defaultMinFreeReserveBytes: Int64 = 256 * 1024 * 1024

    let root: URL
    let maxInboxBytes: Int64
    let minFreeReserveBytes: Int64
    private let availableBytesProvider: (@Sendable (URL) -> Int64?)?

    init(
        root: URL? = nil,
        maxInboxBytes: Int64 = Self.defaultMaxInboxBytes,
        minFreeReserveBytes: Int64 = Self.defaultMinFreeReserveBytes,
        availableBytesProvider: (@Sendable (URL) -> Int64?)? = nil
    ) throws {
        self.maxInboxBytes = max(1, maxInboxBytes)
        self.minFreeReserveBytes = max(0, minFreeReserveBytes)
        self.availableBytesProvider = availableBytesProvider
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

        let access = source.startAccessingSecurityScopedResource()
        defer { if access { source.stopAccessingSecurityScopedResource() } }

        let sourceAttributes = try source.resourceValues(forKeys: [.fileSizeKey, .isRegularFileKey, .isSymbolicLinkKey])
        guard sourceAttributes.isRegularFile == true,
              sourceAttributes.isSymbolicLink != true,
              let sourceSize = sourceAttributes.fileSize,
              sourceSize > 0 else {
            throw CocoaError(.fileReadCorruptFile)
        }
        let incomingBytes = Int64(sourceSize)
        let stagedBytes = (try? items().reduce(Int64(0)) { $0 + max(0, $1.size) }) ?? 0
        guard incomingBytes <= maxInboxBytes, stagedBytes + incomingBytes <= maxInboxBytes else {
            throw VaultInboxError.inboxQuotaExceeded(limitBytes: maxInboxBytes)
        }
        if let available = availableDiskBytes(at: root), available < incomingBytes + minFreeReserveBytes {
            throw VaultInboxError.insufficientDiskSpace
        }

        let id = UUID()
        let pending = root.appendingPathComponent(".pending-\(id.uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: pending, withIntermediateDirectories: false,
            attributes: [.protectionKey: FileProtectionType.complete])
        defer { try? FileManager.default.removeItem(at: pending) }

        let target = pending.appendingPathComponent("payload")
        try FileManager.default.copyItem(at: source, to: target)
        try FileManager.default.setAttributes([.protectionKey: FileProtectionType.complete], ofItemAtPath: target.path)
        let attributes = try target.resourceValues(forKeys: [.fileSizeKey, .isRegularFileKey, .isSymbolicLinkKey])
        guard attributes.isRegularFile == true, attributes.isSymbolicLink != true,
              let size = attributes.fileSize, size > 0 else { throw CocoaError(.fileReadCorruptFile) }
        guard Int64(size) <= maxInboxBytes, stagedBytes + Int64(size) <= maxInboxBytes else {
            throw VaultInboxError.inboxQuotaExceeded(limitBytes: maxInboxBytes)
        }
        let item = VaultInboxItem(id: id, filename: URL(fileURLWithPath: filename).lastPathComponent,
            sourceType: sourceType, size: Int64(size), createdAt: Date())
        try JSONEncoder().encode(item).write(to: pending.appendingPathComponent("item.json"), options: [.atomic, .completeFileProtection])
        try FileManager.default.moveItem(at: pending, to: root.appendingPathComponent(id.uuidString, isDirectory: true))
        return item
    }

    private func availableDiskBytes(at url: URL) -> Int64? {
        if let availableBytesProvider {
            return availableBytesProvider(url)
        }
        if let important = try? url.resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey])
            .volumeAvailableCapacityForImportantUsage, important > 0 {
            return important
        }
        if let free = (try? FileManager.default.attributesOfFileSystem(forPath: url.path)[.systemFreeSize] as? NSNumber)?.int64Value,
           free >= 0 {
            return free
        }
        return nil
    }

    func payload(for item: VaultInboxItem) -> URL {
        root.appendingPathComponent(item.id.uuidString, isDirectory: true).appendingPathComponent("payload")
    }

    func remove(_ item: VaultInboxItem) throws {
        try FileManager.default.removeItem(at: root.appendingPathComponent(item.id.uuidString, isDirectory: true))
    }
}
