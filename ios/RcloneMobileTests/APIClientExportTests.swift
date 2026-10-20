import Foundation
import XCTest
@testable import RcloneMobile

final class APIClientExportTests: XCTestCase {
    func testUntrustedFilenamesCannotEscapeExportDirectory() throws {
        let names = ["../", ".", "..", "/", "\\", "../../escape.txt", "..\\..\\escape.txt",
                     "/absolute/file.txt", "C:\\outside\\file.txt", "", "  ", "\0", "line\nfeed.txt",
                     "Foto Ä.png", String(repeating: "🙂", count: 100)]
        for name in names {
            let target = try APIClient.writeExport(filename: name) { url in
                try Data("fixture".utf8).write(to: url)
            }
            let directory = target.deletingLastPathComponent()
            defer { try? FileManager.default.removeItem(at: directory) }
            XCTAssertEqual(target.standardizedFileURL.deletingLastPathComponent(), directory.standardizedFileURL)
            XCTAssertTrue(directory.lastPathComponent.hasPrefix("sicherpfad-export-"))
            XCTAssertFalse(["", ".", ".."].contains(target.lastPathComponent))
            XCTAssertFalse(target.lastPathComponent.contains("/"))
            XCTAssertFalse(target.lastPathComponent.contains("\\"))
            XCTAssertLessThanOrEqual(target.lastPathComponent.utf8.count, 183)
            XCTAssertEqual(try Data(contentsOf: target), Data("fixture".utf8))
        }
    }

    func testSameNameExportsAreIndependentAndProtected() throws {
        let files = RecordingExportFileManager()
        let first = try APIClient.writeExport(filename: "receipt.json", fileManager: files) { try Data("first".utf8).write(to: $0) }
        defer { try? FileManager.default.removeItem(at: first.deletingLastPathComponent()) }
        let second = try APIClient.writeExport(filename: "receipt.json", fileManager: files) { try Data("second".utf8).write(to: $0) }
        defer { try? FileManager.default.removeItem(at: second.deletingLastPathComponent()) }
        XCTAssertNotEqual(first.deletingLastPathComponent(), second.deletingLastPathComponent())
        XCTAssertEqual(first.lastPathComponent, "receipt.json")
        XCTAssertEqual(try Data(contentsOf: first), Data("first".utf8))
        XCTAssertEqual(try Data(contentsOf: second), Data("second".utf8))
        for target in [first, second] {
            let file = try FileManager.default.attributesOfItem(atPath: target.path)
            let directory = try FileManager.default.attributesOfItem(atPath: target.deletingLastPathComponent().path)
            XCTAssertEqual((file[.posixPermissions] as? NSNumber)?.intValue, 0o600)
            XCTAssertEqual((directory[.posixPermissions] as? NSNumber)?.intValue, 0o700)
            // FileManager does not expose Data Protection attributes in Simulator.
            // Verify the requested policy here, and read it back on physical devices.
            XCTAssertEqual(files.fileAttributes[target.path]?[.protectionKey] as? FileProtectionType, .complete)
            XCTAssertEqual(files.directoryAttributes[target.deletingLastPathComponent().path]?[.protectionKey] as? FileProtectionType, .complete)
            #if !targetEnvironment(simulator)
            XCTAssertEqual(file[.protectionKey] as? String, FileProtectionType.complete.rawValue)
            XCTAssertEqual(directory[.protectionKey] as? String, FileProtectionType.complete.rawValue)
            #endif
        }
    }

    func testProtectionFailureRemovesExportInsteadOfReturningUnprotectedFile() throws {
        let files = RecordingExportFileManager()
        files.rejectFileProtection = true
        var partial: URL?
        XCTAssertThrowsError(try APIClient.writeExport(filename: "private.json", fileManager: files) { url in
            partial = url
            try Data("fixture".utf8).write(to: url)
        }) { error in
            XCTAssertEqual((error as NSError).code, NSFileWriteNoPermissionError)
        }
        let target = try XCTUnwrap(partial)
        XCTAssertEqual(files.fileAttributes[target.path]?[.protectionKey] as? FileProtectionType, .complete)
        XCTAssertFalse(FileManager.default.fileExists(atPath: target.deletingLastPathComponent().path))
    }

    func testFailedExportRemovesPartialFileAndItsDirectory() throws {
        var partial: URL?
        XCTAssertThrowsError(try APIClient.writeExport(filename: "partial.json") { url in
            partial = url
            try Data("partial".utf8).write(to: url)
            throw URLError(.cancelled)
        })
        let directory = try XCTUnwrap(partial).deletingLastPathComponent()
        XCTAssertFalse(FileManager.default.fileExists(atPath: directory.path))
    }

    func testGETAndPOSTDownloadsUsePrivateProtectedExports() async throws {
        let baseURL = try XCTUnwrap(URL(string: "https://export.example"))
        let jar = URLSessionConfiguration.ephemeral.httpCookieStorage!
        jar.setCookie(try XCTUnwrap(HTTPCookie(properties: [
            .domain: "export.example", .path: "/", .name: APIClient.csrfCookie, .value: "fixture"
        ])))
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [ExportURLProtocol.self]
        configuration.httpCookieStorage = jar
        let session = URLSession(configuration: configuration)
        defer { session.invalidateAndCancel() }
        let client = APIClient(baseURL: baseURL, session: session, cookieStorage: jar)
        let get = try await client.downloadVaultItem(id: "fixture", filename: "../../outside.txt")
        defer { try? FileManager.default.removeItem(at: get.deletingLastPathComponent()) }
        let post = try await client.downloadRecoveryHandover(RecoveryHandoverRequest(
            currentPassword: "fixture", passphrase: "fixture-passphrase", includePaths: false
        ))
        defer { try? FileManager.default.removeItem(at: post.deletingLastPathComponent()) }
        for target in [get, post] {
            XCTAssertEqual(try Data(contentsOf: target), Data("fixture-export".utf8))
            XCTAssertTrue(target.deletingLastPathComponent().lastPathComponent.hasPrefix("sicherpfad-export-"))
            let attributes = try FileManager.default.attributesOfItem(atPath: target.path)
            let directory = try FileManager.default.attributesOfItem(atPath: target.deletingLastPathComponent().path)
            XCTAssertEqual((attributes[.posixPermissions] as? NSNumber)?.intValue, 0o600)
            XCTAssertEqual((directory[.posixPermissions] as? NSNumber)?.intValue, 0o700)
            #if !targetEnvironment(simulator)
            XCTAssertEqual(attributes[.protectionKey] as? String, FileProtectionType.complete.rawValue)
            #endif
        }
        XCTAssertNotEqual(get.deletingLastPathComponent(), post.deletingLastPathComponent())
    }
}

private final class RecordingExportFileManager: FileManager, @unchecked Sendable {
    var directoryAttributes: [String: [FileAttributeKey: Any]] = [:]
    var fileAttributes: [String: [FileAttributeKey: Any]] = [:]
    var rejectFileProtection = false

    override func createDirectory(at url: URL, withIntermediateDirectories createIntermediates: Bool,
                                  attributes: [FileAttributeKey: Any]? = nil) throws {
        directoryAttributes[url.path] = attributes ?? [:]
        try super.createDirectory(at: url, withIntermediateDirectories: createIntermediates, attributes: attributes)
    }

    override func setAttributes(_ attributes: [FileAttributeKey: Any], ofItemAtPath path: String) throws {
        fileAttributes[path] = attributes
        if rejectFileProtection { throw CocoaError(.fileWriteNoPermission) }
        try super.setAttributes(attributes, ofItemAtPath: path)
    }
}

private final class ExportURLProtocol: URLProtocol {
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        let response = HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil,
                                       headerFields: ["Content-Type": "application/octet-stream"])!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data("fixture-export".utf8))
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}
