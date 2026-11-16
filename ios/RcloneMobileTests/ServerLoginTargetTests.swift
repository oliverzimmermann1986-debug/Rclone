import XCTest
@testable import RcloneMobile

final class ServerLoginTargetTests: XCTestCase {
    func testBrowserLoginPageBecomesServerRootWithoutBrowserState() throws {
        let target = try ServerLoginTarget(address: " https://demo.tail0000.ts.net/login/?next=/security#private ")
        XCTAssertEqual(target.address, "https://demo.tail0000.ts.net")
        XCTAssertTrue(target.isTailscale)
        XCTAssertTrue(target.usesHTTPS)
        XCTAssertTrue(target.supportsPasskeys)
    }

    func testLoginPageKeepsProxyPrefixPortAndExplicitTransport() throws {
        let target = try ServerLoginTarget(address: "http://192.168.1.67:8080/sicherpfad/login///?next=/")
        XCTAssertEqual(target.address, "http://192.168.1.67:8080/sicherpfad")
        XCTAssertFalse(target.usesHTTPS)
        XCTAssertFalse(target.supportsPasskeys)
    }

    func testOnlyExactFinalLoginPageIsRemoved() throws {
        for path in ["/login-proxy", "/login/nested", "/nested/Login", "/nested/%2Flogin"] {
            let target = try ServerLoginTarget(address: "https://backup.example" + path)
            XCTAssertEqual(URLComponents(url: target.url, resolvingAgainstBaseURL: false)?.percentEncodedPath, path)
        }
        XCTAssertEqual(try ServerLoginTarget(address: "https://backup.example/proxy/login/login").url.path,
            "/proxy/login")
    }

    func testSelectedBasePathIsNotInterpretedAgainByAPIClient() throws {
        let target = try ServerLoginTarget(address: "https://backup.example/proxy/login/login")
        XCTAssertEqual(try APIClient.normalizedServerURL(target.address), target.url)
        XCTAssertEqual(try ServerLoginTarget(serverURL: target.url), target)
    }

    func testTailscaleHintRequiresActualDomainSuffix() throws {
        for host in ["demo.ts.net.example", "fake-ts.net", "192.168.1.67"] {
            XCTAssertFalse(try ServerLoginTarget(address: "https://" + host).isTailscale, host)
        }
        XCTAssertTrue(try ServerLoginTarget(address: "https://DEMO.TS.NET").isTailscale)
    }

    func testPasskeyHintRequiresHTTPSDomainAndExcludesIPAddresses() throws {
        for address in ["http://backup.example", "https://192.168.1.67", "https://[fd00::1]", "https://[::1]"] {
            XCTAssertFalse(try ServerLoginTarget(address: address).supportsPasskeys, address)
        }
        XCTAssertTrue(try ServerLoginTarget(address: "backup.example").supportsPasskeys)
        XCTAssertTrue(try ServerLoginTarget(address: "https://backup.example:8443/proxy").supportsPasskeys)
    }

    func testTargetDoesNotAcceptEmbeddedCredentialsOrUnsupportedSchemes() {
        for address in ["https://admin:private@backup.example/login", "ftp://backup.example/login", "https://"] {
            XCTAssertThrowsError(try ServerLoginTarget(address: address), address)
        }
    }
}
