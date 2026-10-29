import Foundation
import XCTest
@testable import RcloneMobile

final class AppNavigationTests: XCTestCase {
    func testRecognizesOnlySupportedNavigationLinks() throws {
        for scheme in ["sicherpfad", "rclonesync", "SICHERPFAD"] {
            XCTAssertEqual(AppNavigationDestination(url: try XCTUnwrap(URL(string: "\(scheme)://vault"))), .vault)
            XCTAssertEqual(AppNavigationDestination(url: try XCTUnwrap(URL(string: "\(scheme)://recovery"))), .recovery)
        }
        for value in ["https://vault", "file:///recovery", "sicherpfad://unknown"] {
            XCTAssertNil(AppNavigationDestination(url: try XCTUnwrap(URL(string: value))))
        }
    }

    func testLinksSurviveCheckingAndSignedOutAndAreConsumedExactlyOnceAfterSignIn() {
        for destination in [AppNavigationDestination.recovery, .vault] {
            var pending = PendingAppNavigation()
            pending.request(destination)
            XCTAssertNil(pending.consume(isSignedIn: false))
            XCTAssertNil(pending.consume(isSignedIn: false))
            XCTAssertEqual(pending.destination, destination)
            XCTAssertEqual(pending.consume(isSignedIn: true), destination.notificationName)
            XCTAssertNil(pending.consume(isSignedIn: true))
        }
    }

    func testMostRecentNavigationWinsWhileWaitingForLogin() {
        var pending = PendingAppNavigation()
        pending.request(.recovery)
        pending.request(.vault)
        XCTAssertEqual(pending.consume(isSignedIn: true), .deviceVaultNavigationRequested)
        pending.request(.recovery)
        XCTAssertNil(pending.consume(isSignedIn: false))
        XCTAssertEqual(pending.consume(isSignedIn: true), .pushRecoveryNavigationRequested)
    }
}
