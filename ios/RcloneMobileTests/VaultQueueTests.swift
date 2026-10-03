import Foundation
import XCTest
@testable import RcloneMobile

final class VaultQueueTests: XCTestCase {
    private func scope(server: String = "https://backup.example", user: String = "admin", pair: String = "photos", target: String = "cloud:/Photos") -> VaultQueueScope {
        VaultQueueScope(serverURL: URL(string: server)!, username: user, pairID: pair,
            source: "/photos", target: target, direction: "push")
    }

    func testScopeCanonicalizationAndAccountTargetIsolation() {
        let canonical = scope(server: "HTTPS://BACKUP.EXAMPLE:443/")
        XCTAssertEqual(canonical.key, scope().key)
        XCTAssertNotEqual(scope().key, scope(server: "http://backup.example").key)
        XCTAssertNotEqual(scope().key, scope(server: "https://backup.example:8001").key)
        XCTAssertNotEqual(scope().key, scope(user: "other").key)
        XCTAssertNotEqual(scope().key, scope(pair: "other").key)
        XCTAssertNotEqual(scope().key, scope(target: "cloud:/New").key)
        XCTAssertFalse(scope(server: "https://user:secret@backup.example").server.contains("secret"))
    }

    func testQueueSurvivesRecreationWithUploadIDAndPreservesOriginal() throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: folder) }
        let source = folder.appendingPathComponent("original.txt")
        try Data("saved bytes".utf8).write(to: source)
        let first = VaultQueueStore(root: folder.appendingPathComponent("queue"))
        var entry = try first.stage(source, filename: "original.txt", sourceType: "file", scope: scope())
        entry.uploadID = "server-upload"
        entry.received = 4
        entry.state = "error"
        entry.lastError = "offline"
        try first.save(entry)
        let relaunched = VaultQueueStore(root: first.root)
        let recovered = try XCTUnwrap(relaunched.entries(for: scope()).first)
        XCTAssertEqual(recovered.uploadID, "server-upload")
        XCTAssertEqual(recovered.received, 4)
        XCTAssertEqual(try Data(contentsOf: relaunched.payload(for: recovered)), Data("saved bytes".utf8))
        XCTAssertTrue(try relaunched.entries(for: scope(user: "other")).isEmpty)
        try relaunched.remove(recovered)
        XCTAssertTrue(FileManager.default.fileExists(atPath: source.path))
        XCTAssertTrue(try relaunched.entries(for: scope()).isEmpty)
    }

    func testSharedInboxImportIsAtomicAndIdempotent() throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: folder) }
        let source = folder.appendingPathComponent("photo.jpg")
        try Data("photo bytes".utf8).write(to: source)
        let inbox = try VaultInbox(root: folder.appendingPathComponent("inbox"))
        let item = try inbox.stage(source, filename: "../../photo.jpg", sourceType: "photo")
        XCTAssertEqual(item.filename, "photo.jpg")
        try FileManager.default.createDirectory(at: inbox.root.appendingPathComponent(".pending-crash"), withIntermediateDirectories: true)
        XCTAssertEqual(try inbox.items().map(\.id), [item.id])
        let queue = VaultQueueStore(root: folder.appendingPathComponent("queue"))
        _ = try queue.stage(inbox.payload(for: item), filename: item.filename, sourceType: item.sourceType, scope: scope(), id: item.id)
        // Crash after queue commit but before inbox removal: importing again
        // reuses the same committed entry instead of uploading a second copy.
        _ = try queue.stage(inbox.payload(for: item), filename: item.filename, sourceType: item.sourceType, scope: scope(), id: item.id)
        try inbox.remove(item)
        XCTAssertEqual(try queue.entries(for: scope()).count, 1)
        XCTAssertTrue(try inbox.items().isEmpty)
        XCTAssertTrue(FileManager.default.fileExists(atPath: source.path))
    }

    func testChangedOrDeletedTargetRetainsExportableQueueAndExplicitReassignmentResetsRemoteID() throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: folder) }
        let source = folder.appendingPathComponent("file.txt")
        try Data("private bytes".utf8).write(to: source)
        let store = VaultQueueStore(root: folder.appendingPathComponent("queue"))
        var entry = try store.stage(source, filename: "file.txt", sourceType: "file", scope: scope())
        entry.uploadID = "old-remote-id"
        entry.received = 5
        try store.save(entry)
        let newScope = scope(target: "cloud:/New")
        XCTAssertTrue(try store.entries(for: newScope).isEmpty)
        let unassigned = try store.unassignedEntries(for: newScope, activeScopeKeys: [newScope.key])
        XCTAssertEqual(unassigned.map(\.id), [entry.id])
        XCTAssertEqual(unassigned.first?.destinationDescription, "cloud:/Photos")
        XCTAssertTrue(try store.unassignedEntries(for: scope(user: "other"), activeScopeKeys: []).isEmpty)
        let exported = try store.export(entry)
        defer { try? FileManager.default.removeItem(at: exported.deletingLastPathComponent()) }
        XCTAssertEqual(try Data(contentsOf: exported), Data("private bytes".utf8))
        XCTAssertTrue(FileManager.default.fileExists(atPath: try store.payload(for: entry).path))
        XCTAssertThrowsError(try store.reassign(entry, to: scope(user: "other")))
        try store.reassign(entry, to: newScope)
        let reassigned = try XCTUnwrap(store.entries(for: newScope).first)
        XCTAssertNil(reassigned.uploadID)
        XCTAssertEqual(reassigned.received, 0)
        XCTAssertEqual(reassigned.sha256, entry.sha256)
        XCTAssertTrue(try store.entries(for: scope()).isEmpty)
    }

    @MainActor
    func testInactiveViewGateKeepsQueueAndSendsNoRequest() async throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: folder) }
        let source = folder.appendingPathComponent("file.txt")
        try Data("bytes".utf8).write(to: source)
        let store = VaultQueueStore(root: folder.appendingPathComponent("queue"))
        let entry = try store.stage(source, filename: "file.txt", sourceType: "file", scope: scope())
        VaultResumeURLProtocol.reset(digest: entry.sha256)
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [VaultResumeURLProtocol.self]
        let client = APIClient(baseURL: URL(string: "https://backup.example")!, session: URLSession(configuration: configuration))
        let model = VaultTransferModel(queueStore: store)
        await model.resumeQueue(scope: scope(), using: client) { false }
        XCTAssertTrue(VaultResumeURLProtocol.recordedRequests().isEmpty)
        XCTAssertEqual(try store.entries(for: scope()).map(\.id), [entry.id])
        XCTAssertTrue(FileManager.default.fileExists(atPath: try store.payload(for: entry).path))
    }

    @MainActor
    func testRelaunchResumesKnownUploadAtServerOffsetWithoutCreatingAnother() async throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: folder) }
        let source = folder.appendingPathComponent("file.txt")
        try Data("0123456789".utf8).write(to: source)
        let store = VaultQueueStore(root: folder.appendingPathComponent("queue"))
        var entry = try store.stage(source, filename: "file.txt", sourceType: "file", scope: scope())
        entry.uploadID = "existing-upload"
        entry.received = 8
        try store.save(entry)
        VaultResumeURLProtocol.reset(digest: entry.sha256)
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [VaultResumeURLProtocol.self]
        let cookies = try XCTUnwrap(configuration.httpCookieStorage)
        cookies.setCookie(try XCTUnwrap(HTTPCookie(properties: [
            .name: APIClient.csrfCookie, .value: "test", .domain: "backup.example", .path: "/"
        ])))
        let client = APIClient(baseURL: URL(string: "https://backup.example")!,
            session: URLSession(configuration: configuration), cookieStorage: cookies)
        let transfer = VaultTransferModel(queueStore: VaultQueueStore(root: store.root))
        await transfer.resumeQueue(scope: scope(), using: client) { true }
        XCTAssertNil(transfer.errorMessage)
        XCTAssertTrue(try store.entries(for: scope()).isEmpty)
        let requests = VaultResumeURLProtocol.recordedRequests()
        XCTAssertFalse(requests.contains { $0.httpMethod == "POST" && $0.url?.path == "/api/vault/uploads" })
        let chunk = try XCTUnwrap(requests.first { $0.httpMethod == "PUT" })
        XCTAssertEqual(URLComponents(url: try XCTUnwrap(chunk.url), resolvingAgainstBaseURL: false)?.query, "offset=4")
        XCTAssertTrue(FileManager.default.fileExists(atPath: source.path))
    }
    @MainActor
    func testCorruptFirstFileIsRetainedAndFollowingFileCompletes() async throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: folder) }
        let source = folder.appendingPathComponent("file.txt")
        try Data("0123456789".utf8).write(to: source)
        let store = VaultQueueStore(root: folder.appendingPathComponent("queue"))
        let bad = try store.stage(source, filename: "bad.txt", sourceType: "file", scope: scope())
        var good = try store.stage(source, filename: "file.txt", sourceType: "file", scope: scope())
        good.uploadID = "existing-upload"
        try store.save(good)
        try Data("corrupt".utf8).write(to: store.payload(for: bad))
        let client = try queueClient(digest: good.sha256)
        let transfer = VaultTransferModel(queueStore: store)
        await transfer.resumeQueue(scope: scope(), using: client) { true }
        let retained = try XCTUnwrap(store.entries(for: scope()).first)
        XCTAssertEqual(retained.id, bad.id)
        XCTAssertEqual(retained.state, "failed")
        XCTAssertTrue(retained.requiresUserRetry)
        XCTAssertNotNil(retained.lastError)
        XCTAssertFalse(try store.entries(for: scope()).contains { $0.id == good.id })
        XCTAssertTrue(FileManager.default.fileExists(atPath: try store.payload(for: retained).path))
        let requestsAfterSuccess = VaultResumeURLProtocol.recordedRequests().count
        await transfer.resumeQueue(scope: scope(), using: client) { true }
        XCTAssertEqual(VaultResumeURLProtocol.recordedRequests().count, requestsAfterSuccess + 1,
                       "Only library refresh is allowed; failed files do not immediately retry")
        transfer.skipEntry(retained, scope: scope())
        let skipped = try XCTUnwrap(store.entries(for: scope()).first)
        XCTAssertEqual(skipped.state, "skipped")
        XCTAssertTrue(FileManager.default.fileExists(atPath: try store.payload(for: skipped).path))
        transfer.retryEntry(skipped, scope: scope())
        let retried = try XCTUnwrap(store.entries(for: scope()).first)
        XCTAssertEqual(retried.state, "waiting")
        XCTAssertFalse(retried.requiresUserRetry)
        XCTAssertNil(retried.lastError)
    }

    @MainActor
    func testExpiredLoginPausesWholeQueueAndRetainsBothFilesForResume() async throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: folder) }
        let source = folder.appendingPathComponent("file.txt")
        try Data("0123456789".utf8).write(to: source)
        let store = VaultQueueStore(root: folder.appendingPathComponent("queue"))
        var first = try store.stage(source, filename: "first.txt", sourceType: "file", scope: scope())
        let second = try store.stage(source, filename: "second.txt", sourceType: "file", scope: scope())
        first.uploadID = "existing-upload"
        try store.save(first)
        let client = try queueClient(digest: first.sha256, failureStatus: 401)
        let transfer = VaultTransferModel(queueStore: store)
        await transfer.resumeQueue(scope: scope(), using: client) { true }
        let retained = try store.entries(for: scope())
        XCTAssertEqual(Set(retained.map(\.id)), Set([first.id, second.id]))
        XCTAssertTrue(retained.allSatisfy { $0.state == "waiting" && !$0.requiresUserRetry })
        XCTAssertEqual(VaultResumeURLProtocol.recordedRequests().count, 1)
        XCTAssertNotNil(transfer.errorMessage)
    }

    private func queueClient(digest: String, failureStatus: Int? = nil) throws -> APIClient {
        VaultResumeURLProtocol.reset(digest: digest, failureStatus: failureStatus)
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [VaultResumeURLProtocol.self]
        let cookies = try XCTUnwrap(configuration.httpCookieStorage)
        cookies.setCookie(try XCTUnwrap(HTTPCookie(properties: [
            .name: APIClient.csrfCookie, .value: "test", .domain: "backup.example", .path: "/"
        ])))
        return APIClient(baseURL: URL(string: "https://backup.example")!,
                         session: URLSession(configuration: configuration), cookieStorage: cookies)
    }

}

private final class VaultResumeURLProtocol: URLProtocol {
    private static let lock = NSLock()
    private static var digest = ""
    private static var requests: [URLRequest] = []
    private static var failureStatus: Int?
    static func reset(digest: String, failureStatus: Int? = nil) {
        lock.lock(); defer { lock.unlock() }
        self.digest = digest
        self.failureStatus = failureStatus
        requests = []
    }
    static func recordedRequests() -> [URLRequest] {
        lock.lock(); defer { lock.unlock() }
        return requests
    }
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        Self.lock.lock()
        Self.requests.append(request)
        let digest = Self.digest
        let failureStatus = Self.failureStatus
        Self.lock.unlock()
        if let failureStatus {
            let response = HTTPURLResponse(url: request.url!, statusCode: failureStatus,
                                           httpVersion: "HTTP/1.1", headerFields: ["Content-Type": "application/json"])!
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: Data(#"{"detail":"Session expired"}"#.utf8))
            client?.urlProtocolDidFinishLoading(self)
            return
        }
        let ready = request.url?.path.hasSuffix("complete") == true || request.url?.path.hasSuffix("library") == true
        let uploaded = request.httpMethod == "PUT"
        let item: [String: Any] = [
            "id": "existing-upload", "pair": "Fotos", "identity": "photos", "filename": "file.txt",
            "source_type": "file", "device_name": "Test", "size": 10, "sha256": digest,
            "received": ready || uploaded ? 10 : 4, "status": ready ? "ready" : uploaded ? "uploaded" : "receiving",
            "deduplicated": false, "verified": ready, "target_relative": "Sicherpfad/file.txt",
            "created_at": 100, "updated_at": 100
        ]
        let body: [String: Any]
        if request.url?.path.hasSuffix("library") == true {
            body = ["items": [item]]
        } else {
            body = item
        }
        let data = try! JSONSerialization.data(withJSONObject: body)
        let response = HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: "HTTP/1.1", headerFields: ["Content-Type": "application/json"])!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data)
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}
