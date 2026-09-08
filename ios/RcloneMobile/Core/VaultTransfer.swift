import ActivityKit
import Combine
import Foundation
import UIKit

@MainActor
final class VaultTransferModel: ObservableObject {
    @Published private(set) var current: VaultUploadStatus?
    @Published private(set) var library: [VaultUploadStatus] = []
    @Published private(set) var isWorking = false
    @Published private(set) var queue: [VaultQueueEntry] = []
    @Published private(set) var unassignedQueue: [VaultQueueEntry] = []
    @Published private(set) var activeEntryID: UUID?
    @Published var errorMessage: String?

    private let chunkSize = 1024 * 1024
    private var liveActivity: Activity<ProtectionActivityAttributes>?
    private let queueStore: VaultQueueStore
    private var pauseRequested = false
    private var activeScopeKeys: Set<String> = []
    private var displayedScope: VaultQueueScope?
    private var libraryGeneration = 0

    init(queueStore: VaultQueueStore = VaultQueueStore()) { self.queueStore = queueStore }

    func loadQueue(scope: VaultQueueScope, activeScopeKeys: Set<String>? = nil) {
        if displayedScope != scope {
            displayedScope = scope
            libraryGeneration += 1
            library = []
            current = nil
            queue = []
            unassignedQueue = []
        }
        if let activeScopeKeys { self.activeScopeKeys = activeScopeKeys }
        do {
            queue = try queueStore.entries(for: scope)
            unassignedQueue = try queueStore.unassignedEntries(for: scope, activeScopeKeys: self.activeScopeKeys.union([scope.key]))
        }
        catch { errorMessage = error.localizedDescription }
    }

    func pause() { pauseRequested = true }

    private func refreshDisplayedQueue(fallback: VaultQueueScope) {
        loadQueue(scope: displayedScope ?? fallback)
    }

    func reassign(_ entry: VaultQueueEntry, to scope: VaultQueueScope) async throws {
        guard !isWorking, entry.accountKey == scope.accountKey else { throw CocoaError(.fileWriteNoPermission) }
        let store = queueStore
        try await Task.detached(priority: .utility) { try store.reassign(entry, to: scope) }.value
        refreshDisplayedQueue(fallback: scope)
    }

    func export(_ entry: VaultQueueEntry, scope: VaultQueueScope) async throws -> URL {
        guard entry.accountKey == scope.accountKey else { throw CocoaError(.fileReadNoPermission) }
        let store = queueStore
        return try await Task.detached(priority: .utility) { try store.export(entry) }.value
    }

    func removeQueued(_ entry: VaultQueueEntry, scope: VaultQueueScope) {
        guard !isWorking, entry.scopeKey == scope.key else { return }
        do { try queueStore.remove(entry); loadQueue(scope: scope) }
        catch { errorMessage = error.localizedDescription }
    }

    func enqueue(fileURL: URL, filename: String, sourceType: String, scope: VaultQueueScope, id: UUID = UUID()) async throws {
        let store = queueStore
        _ = try await Task.detached(priority: .utility) {
            try store.stage(fileURL, filename: filename, sourceType: sourceType, scope: scope, id: id)
        }.value
        refreshDisplayedQueue(fallback: scope)
    }

    var progress: Double { current?.fractionCompleted ?? 0 }

    func refreshLibrary(
        identity: String?,
        using client: any APIClientProtocol
    ) async {
        libraryGeneration += 1
        let generation = libraryGeneration
        do {
            let items = try await client.getVaultLibrary(identity: identity).items
            guard generation == libraryGeneration else { return }
            library = items
        } catch is CancellationError {
        } catch {
            guard generation == libraryGeneration else { return }
            errorMessage = error.localizedDescription
        }
    }

    func resumeQueue(scope: VaultQueueScope, using client: any APIClientProtocol, isCurrentScope: () -> Bool) async {
        guard !isWorking else { return }
        if displayedScope == nil { loadQueue(scope: scope) }
        isWorking = true
        errorMessage = nil
        pauseRequested = false
        defer {
            isWorking = false
            activeEntryID = nil
            refreshDisplayedQueue(fallback: scope)
            if displayedScope != scope { current = nil }
        }
        do {
            for var entry in try queueStore.entries(for: scope) {
                try checkActiveScope(isCurrentScope)
                activeEntryID = entry.id
                do {
                    try await transfer(&entry, scope: scope, using: client, isCurrentScope: isCurrentScope)
                    try queueStore.remove(entry)
                    refreshDisplayedQueue(fallback: scope)
                } catch {
                    entry.state = error is CancellationError ? "waiting" : "error"
                    entry.lastError = error is CancellationError ? nil : error.localizedDescription
                    try queueStore.save(entry)
                    throw error
                }
            }
            try checkActiveScope(isCurrentScope)
            await refreshLibrary(identity: scope.pairID, using: client)
        } catch is CancellationError {
            await endLiveActivity(current, cancelled: true)
        } catch {
            errorMessage = error.localizedDescription
            await endLiveActivity(current, error: error.localizedDescription)
        }
    }

    private func transfer(_ entry: inout VaultQueueEntry, scope: VaultQueueScope,
                          using client: any APIClientProtocol, isCurrentScope: () -> Bool) async throws {
            let staged = try queueStore.payload(for: entry)
            let size = entry.size
            let digest = try await Task.detached(priority: .utility) { try VaultQueueStore.digest(staged) }.value
            guard digest == entry.sha256 else { throw CocoaError(.fileReadCorruptFile) }
            try checkActiveScope(isCurrentScope)
            var resumed: VaultUploadStatus?
            if let uploadID = entry.uploadID {
                do { resumed = try await client.getVaultUpload(uploadID: uploadID) }
                catch APIError.server(status: 404, message: _) { resumed = nil }
                try checkActiveScope(isCurrentScope)
            }
            if let resumed { try validate(resumed, for: entry) }
            let statusToResume = resumed?.status == "error" ? nil : resumed
            var status: VaultUploadStatus
            if let statusToResume {
                status = statusToResume
            } else {
                status = try await client.createVaultUpload(
                VaultUploadRequest(
                    identity: scope.pairID,
                    filename: entry.filename,
                    size: size,
                    sha256: digest,
                    sourceType: entry.sourceType,
                    deviceName: UIDevice.current.name
                )
            )
            }
            try validate(status, for: entry)
            try persist(status, entry: &entry)
            current = status
            await updateLiveActivity(status)

            let handle = try FileHandle(forReadingFrom: staged)
            defer { try? handle.close() }
            try handle.seek(toOffset: UInt64(status.received))
            var offset = status.received
            var retryCount = 0
            while offset < size {
                try checkActiveScope(isCurrentScope)
                guard let data = try handle.read(upToCount: chunkSize), !data.isEmpty else {
                    throw CocoaError(.fileReadCorruptFile)
                }
                do {
                    status = try await client.uploadVaultChunk(
                        uploadID: status.id,
                        offset: offset,
                        data: data
                    )
                    retryCount = 0
                } catch {
                    guard retryCount < 3 else { throw error }
                    retryCount += 1
                    try await Task.sleep(for: .seconds(retryCount))
                    try checkActiveScope(isCurrentScope)
                    status = try await client.getVaultUpload(uploadID: status.id)
                    try validate(status, for: entry)
                    try handle.seek(toOffset: UInt64(status.received))
                }
                try validate(status, for: entry)
                try persist(status, entry: &entry)
                offset = status.received
                current = status
                await updateLiveActivity(status)
            }

            try checkActiveScope(isCurrentScope)
            do {
                status = try await client.completeVaultUpload(uploadID: status.id)
            } catch {
                try checkActiveScope(isCurrentScope)
                status = try await client.getVaultUpload(uploadID: status.id)
                guard ["queued", "transferring", "ready"].contains(status.status) else {
                    throw error
                }
            }
            try validate(status, for: entry)
            try persist(status, entry: &entry)
            current = status
            await updateLiveActivity(status)
            let deadline = Date().addingTimeInterval(15 * 60)
            var pollCount = 0
            while ["queued", "transferring"].contains(status.status), Date() < deadline {
                try checkActiveScope(isCurrentScope)
                try await Task.sleep(for: .seconds(1))
                try checkActiveScope(isCurrentScope)
                pollCount += 1
                if pollCount.isMultiple(of: 15) {
                    // Re-arms a completion that was interrupted by a server restart.
                    // The backend serializes this operation per upload.
                    status = try await client.completeVaultUpload(uploadID: status.id)
                } else {
                    status = try await client.getVaultUpload(uploadID: status.id)
                }
                try validate(status, for: entry)
                try persist(status, entry: &entry)
                current = status
                await updateLiveActivity(status)
            }
            try checkActiveScope(isCurrentScope)
            guard status.status == "ready", status.verified else {
                throw APIError.server(
                    status: 502,
                    message: status.error ?? "Die Zielkopie konnte nicht verifiziert werden."
                )
            }
            await endLiveActivity(status)
    }

    private func validate(_ status: VaultUploadStatus, for entry: VaultQueueEntry) throws {
        guard status.identity == entry.pairID, status.sha256.lowercased() == entry.sha256,
              status.size == entry.size, status.received >= 0, status.received <= entry.size else {
            throw APIError.invalidResponse
        }
    }

    private func persist(_ status: VaultUploadStatus, entry: inout VaultQueueEntry) throws {
        entry.uploadID = status.id
        entry.received = status.received
        entry.state = status.status
        entry.lastError = status.error
        try queueStore.save(entry)
        if let index = queue.firstIndex(where: { $0.id == entry.id && $0.scopeKey == entry.scopeKey }) { queue[index] = entry }
    }

    private func checkActiveScope(_ isCurrentScope: () -> Bool) throws {
        try Task.checkCancellation()
        guard !pauseRequested, isCurrentScope() else { throw CancellationError() }
    }

    func simulateDemoUpload(identity: String) async {
        guard !isWorking else { return }
        isWorking = true
        errorMessage = nil
        let id = UUID().uuidString.lowercased()
        for step in 0...10 {
            let received = Int64(step) * 420_000
            current = VaultUploadStatus(
                id: id,
                pair: identity,
                identity: identity,
                filename: "Familienmoment.heic",
                sourceType: "photo",
                deviceName: "Demo-iPhone",
                size: 4_200_000,
                sha256: String(repeating: "a", count: 64),
                received: received,
                status: step == 10 ? "ready" : "receiving",
                deduplicated: false,
                verified: step == 10,
                targetRelative: "Sicherpfad/Demo-iPhone/Fotos/2026/08/Familienmoment.heic",
                createdAt: Date().timeIntervalSince1970,
                updatedAt: Date().timeIntervalSince1970,
                completedAt: step == 10 ? Date().timeIntervalSince1970 : nil,
                error: nil
            )
            try? await Task.sleep(for: .milliseconds(120))
        }
        if let current { library.insert(current, at: 0) }
        isWorking = false
    }

    private func updateLiveActivity(_ status: VaultUploadStatus) async {
        guard ActivityAuthorizationInfo().areActivitiesEnabled else { return }
        let state = ProtectionActivityAttributes.ContentState(
            kind: "vault",
            pair: status.filename,
            status: status.status,
            percent: status.fractionCompleted * 100,
            transferred: ByteCountFormatter.string(fromByteCount: status.received, countStyle: .file),
            error: status.error
        )
        if let liveActivity {
            await liveActivity.update(
                ActivityContent(state: state, staleDate: Date().addingTimeInterval(60))
            )
        } else {
            liveActivity = try? Activity.request(
                attributes: ProtectionActivityAttributes(hostname: status.deviceName, jobID: 0),
                content: ActivityContent(state: state, staleDate: Date().addingTimeInterval(60)),
                pushType: nil
            )
        }
    }

    private func endLiveActivity(
        _ status: VaultUploadStatus?,
        cancelled: Bool = false,
        error: String? = nil
    ) async {
        guard let activity = liveActivity else { return }
        liveActivity = nil
        let state = ProtectionActivityAttributes.ContentState(
            kind: "vault",
            pair: status?.filename ?? "Geräte-Vault",
            status: cancelled ? "cancelled" : (error == nil ? "ready" : "error"),
            percent: error == nil && !cancelled ? 100 : status.map { $0.fractionCompleted * 100 },
            transferred: status.map { ByteCountFormatter.string(fromByteCount: $0.received, countStyle: .file) },
            error: error
        )
        await activity.end(
            ActivityContent(state: state, staleDate: nil),
            dismissalPolicy: .default
        )
    }
}
