import ActivityKit
import Combine
import CryptoKit
import Foundation
import UIKit

@MainActor
final class VaultTransferModel: ObservableObject {
    @Published private(set) var current: VaultUploadStatus?
    @Published private(set) var library: [VaultUploadStatus] = []
    @Published private(set) var isWorking = false
    @Published var errorMessage: String?

    private let chunkSize = 1024 * 1024
    private var liveActivity: Activity<ProtectionActivityAttributes>?

    var progress: Double { current?.fractionCompleted ?? 0 }

    func refreshLibrary(
        identity: String?,
        using client: any APIClientProtocol
    ) async {
        do {
            library = try await client.getVaultLibrary(identity: identity).items
        } catch is CancellationError {
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func upload(
        fileURL: URL,
        filename: String,
        sourceType: String,
        identity: String,
        using client: any APIClientProtocol
    ) async {
        guard !isWorking else { return }
        isWorking = true
        errorMessage = nil
        var stagedURL: URL?
        defer {
            isWorking = false
            if let stagedURL {
                try? FileManager.default.removeItem(at: stagedURL.deletingLastPathComponent())
            }
        }
        do {
            let staged = try stageForUpload(fileURL, preferredFilename: filename)
            stagedURL = staged
            let values = try staged.resourceValues(forKeys: [.fileSizeKey])
            let size = Int64(values.fileSize ?? 0)
            guard size > 0 else { throw CocoaError(.fileReadCorruptFile) }
            let digest = try sha256(of: staged)
            var status = try await client.createVaultUpload(
                VaultUploadRequest(
                    identity: identity,
                    filename: filename,
                    size: size,
                    sha256: digest,
                    sourceType: sourceType,
                    deviceName: UIDevice.current.name
                )
            )
            current = status
            await updateLiveActivity(status)

            let handle = try FileHandle(forReadingFrom: staged)
            defer { try? handle.close() }
            try handle.seek(toOffset: UInt64(status.received))
            var offset = status.received
            var retryCount = 0
            while offset < size {
                try Task.checkCancellation()
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
                    status = try await client.getVaultUpload(uploadID: status.id)
                    try handle.seek(toOffset: UInt64(status.received))
                }
                offset = status.received
                current = status
                await updateLiveActivity(status)
            }

            do {
                status = try await client.completeVaultUpload(uploadID: status.id)
            } catch {
                status = try await client.getVaultUpload(uploadID: status.id)
                guard ["queued", "transferring", "ready"].contains(status.status) else {
                    throw error
                }
            }
            current = status
            await updateLiveActivity(status)
            let deadline = Date().addingTimeInterval(15 * 60)
            var pollCount = 0
            while ["queued", "transferring"].contains(status.status), Date() < deadline {
                try await Task.sleep(for: .seconds(1))
                pollCount += 1
                if pollCount.isMultiple(of: 15) {
                    // Re-arms a completion that was interrupted by a server restart.
                    // The backend serializes this operation per upload.
                    status = try await client.completeVaultUpload(uploadID: status.id)
                } else {
                    status = try await client.getVaultUpload(uploadID: status.id)
                }
                current = status
                await updateLiveActivity(status)
            }
            guard status.status == "ready", status.verified else {
                throw APIError.server(
                    status: 502,
                    message: status.error ?? "Die Zielkopie konnte nicht verifiziert werden."
                )
            }
            await endLiveActivity(status)
            await refreshLibrary(identity: identity, using: client)
        } catch is CancellationError {
            await endLiveActivity(current, cancelled: true)
        } catch {
            errorMessage = error.localizedDescription
            await endLiveActivity(current, error: error.localizedDescription)
        }
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

    private func stageForUpload(_ source: URL, preferredFilename: String) throws -> URL {
        let target = FileManager.default.temporaryDirectory
            .appendingPathComponent("sicherpfad-\(UUID().uuidString)", isDirectory: true)
            .appendingPathComponent(preferredFilename)
        try FileManager.default.createDirectory(
            at: target.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        let scoped = source.startAccessingSecurityScopedResource()
        defer { if scoped { source.stopAccessingSecurityScopedResource() } }
        try FileManager.default.copyItem(at: source, to: target)
        return target
    }

    private func sha256(of url: URL) throws -> String {
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        var hasher = SHA256()
        while let data = try handle.read(upToCount: chunkSize), !data.isEmpty {
            hasher.update(data: data)
        }
        return hasher.finalize().map { String(format: "%02x", $0) }.joined()
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
