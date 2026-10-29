import SwiftUI
import XCTest
@testable import RcloneMobile

final class AuditStatusTests: XCTestCase {
    func testInterruptedAndQuarantinedPathsAreNeverHealthy() throws {
        for (status, error) in [("stale", ""), ("skipped", "Quelle in Quarantäne"), ("error", "")] {
            let payload: [String: Any] = ["name": "Fotos", "history_key": "pair", "direction": "push", "mode": "copy", "last_status": status, "error": error]
            let pair = try JSONDecoder().decode(PairHealth.self, from: JSONSerialization.data(withJSONObject: payload))
            XCTAssertTrue(pair.hasFailure)
        }
    }

    func testSuccessfulPathWithNoErrorStaysHealthy() throws {
        let payload = Data(#"{"name":"Fotos","history_key":"pair","direction":"push","mode":"copy","last_status":"ok","error":""}"#.utf8)
        XCTAssertFalse(try JSONDecoder().decode(PairHealth.self, from: payload).hasFailure)
    }

    func testDryRunSurvivesJobDecodingAndOlderJobsRemainCompatible() throws {
        let trial = Data(#"{"id":1,"kind":"backup","status":"ok","started_at":1,"dry_run":true}"#.utf8)
        let old = Data(#"{"id":1,"kind":"backup","status":"ok","started_at":1}"#.utf8)
        XCTAssertEqual(try JSONDecoder().decode(JobRecord.self, from: trial).dryRun, true)
        XCTAssertNil(try JSONDecoder().decode(JobRecord.self, from: old).dryRun)
    }

    func testInterruptedRunIsRedAndExplicitlyNamed() {
        XCTAssertEqual(StatusStyle.label(for: "stale"), "Unterbrochen")
        XCTAssertEqual(StatusStyle.color(for: "stale"), Color.red)
    }
}
