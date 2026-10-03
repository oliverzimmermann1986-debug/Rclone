import XCTest
@testable import RcloneMobile

final class RecoveryRescueCredentialsTests: XCTestCase {
    func testReopenedPreviewCannotImportWithOnlyReenteredServerPassword() throws {
        var credentials = RecoveryRescueCredentials(passphrase: "packet-secret-123", password: "server-secret")
        let mappings = ["source": "replacement"]
        XCTAssertNotNil(credentials.importRequest(envelope: [:], mappings: mappings))

        credentials.clear()
        XCTAssertEqual(credentials.passphrase, "")
        XCTAssertEqual(credentials.password, "")
        credentials.password = "server-secret"

        XCTAssertFalse(credentials.canImport)
        XCTAssertNil(credentials.importRequest(envelope: [:], mappings: mappings))
        credentials.passphrase = "packet-secret-123"
        let request = try XCTUnwrap(credentials.importRequest(envelope: [:], mappings: mappings))
        XCTAssertEqual(request.passphrase, "packet-secret-123")
        XCTAssertEqual(request.currentPassword, "server-secret")
        XCTAssertEqual(request.mappings, mappings)
    }

    func testImportRequestEnforcesBothPassphraseBoundsAndServerPassword() {
        var credentials = RecoveryRescueCredentials(passphrase: "12345678901", password: "server-secret")
        XCTAssertNil(credentials.importRequest(envelope: [:], mappings: [:]))
        credentials.passphrase = "123456789012"
        XCTAssertNotNil(credentials.importRequest(envelope: [:], mappings: [:]))
        credentials.passphrase = String(repeating: "a", count: 1024)
        XCTAssertNotNil(credentials.importRequest(envelope: [:], mappings: [:]))
        credentials.passphrase += "a"
        XCTAssertNil(credentials.importRequest(envelope: [:], mappings: [:]))
        credentials.passphrase = "123456789012"
        credentials.password = ""
        XCTAssertNil(credentials.importRequest(envelope: [:], mappings: [:]))
        credentials.password = String(repeating: "a", count: 1024)
        XCTAssertNotNil(credentials.importRequest(envelope: [:], mappings: [:]))
        credentials.password += "a"
        XCTAssertNil(credentials.importRequest(envelope: [:], mappings: [:]))
    }

    func testPassphraseLengthUsesTheSameUnicodeCodePointsAsTheAPI() {
        let composedCharacter = "e\u{301}"
        var credentials = RecoveryRescueCredentials(passphrase: String(repeating: composedCharacter, count: 6),
                                                   password: "server-secret")
        XCTAssertEqual(credentials.passphrase.count, 6)
        XCTAssertEqual(credentials.passphrase.unicodeScalars.count, 12)
        XCTAssertTrue(credentials.canImport)
        credentials.clear()
        XCTAssertFalse(credentials.canImport)
    }
}
