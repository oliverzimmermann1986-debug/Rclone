import Foundation
import XCTest
@testable import RcloneMobile

final class RestorePlanTests: XCTestCase {
    func testFullAPIResponseDecodesSettingsDatesAndPathEvidence() throws {
        let data = Data(#"""
        {
          "revision": "restore-plan-revision-7",
          "settings": {
            "enabled": true,
            "schedule": "15 5 * * 0,3",
            "sample_files": 24,
            "max_total_mb": 384,
            "max_scan_files": 37777
          },
          "timezone": "Europe/Berlin",
          "generated_at": 1791025200.25,
          "next_runs": [1791083700, 1791342900],
          "due_now": false,
          "data_paths": [
            {
              "id": "photos-path-id",
              "name": "Fotos",
              "evidence_state": "passed",
              "valid_until": 1791535200,
              "coverage_gap": false
            },
            {
              "id": "recipes-path-id",
              "name": "Rezepte",
              "evidence_state": "never",
              "valid_until": null,
              "coverage_gap": true
            }
          ],
          "warnings": ["Rezepte: Kein aktuell gültiger Restore-Nachweis"]
        }
        """#.utf8)

        let plan = try JSONDecoder().decode(RestorePlanResponse.self, from: data)
        XCTAssertEqual(plan.revision, "restore-plan-revision-7")
        XCTAssertEqual(plan.settings, RestorePlanSettings(enabled: true, schedule: "15 5 * * 0,3",
                                                         sampleFiles: 24, maxTotalMB: 384, maxScanFiles: 37777))
        XCTAssertEqual(plan.timezone, "Europe/Berlin")
        XCTAssertEqual(plan.generatedAt, 1791025200.25)
        XCTAssertEqual(plan.nextRuns, [1791083700, 1791342900])
        XCTAssertFalse(plan.dueNow)
        XCTAssertEqual(plan.dataPaths.map(\.id), ["photos-path-id", "recipes-path-id"])
        XCTAssertEqual(plan.dataPaths.map(\.name), ["Fotos", "Rezepte"])
        XCTAssertEqual(plan.dataPaths.map(\.evidenceState), ["passed", "never"])
        XCTAssertEqual(plan.dataPaths[0].validUntil, 1791535200)
        XCTAssertFalse(plan.dataPaths[0].coverageGap)
        XCTAssertNil(plan.dataPaths[1].validUntil)
        XCTAssertTrue(plan.dataPaths[1].coverageGap)
        XCTAssertEqual(plan.warnings, ["Rezepte: Kein aktuell gültiger Restore-Nachweis"])
    }

    func testUpdateUsesBackendWireKeysAndPreservesLoadedScanLimit() throws {
        let loaded = try JSONDecoder().decode(RestorePlanSettings.self, from: Data(#"""
        {"enabled":true,"schedule":"0 5 * * 0","sample_files":20,"max_total_mb":256,"max_scan_files":37777}
        """#.utf8))
        var edited = loaded
        edited.schedule = "15 5 * * 0,3"
        edited.sampleFiles = 24
        let update = RestorePlanUpdate(revision: "expected-base-revision", settings: edited)
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(update)) as? [String: Any])
        let settings = try XCTUnwrap(object["settings"] as? [String: Any])

        XCTAssertEqual(Set(object.keys), ["revision", "settings"])
        XCTAssertEqual(object["revision"] as? String, "expected-base-revision")
        XCTAssertEqual(Set(settings.keys), ["enabled", "schedule", "sample_files", "max_total_mb", "max_scan_files"])
        XCTAssertEqual(settings["enabled"] as? Bool, true)
        XCTAssertEqual(settings["schedule"] as? String, "15 5 * * 0,3")
        XCTAssertEqual(settings["sample_files"] as? Int, 24)
        XCTAssertEqual(settings["max_total_mb"] as? Int, 256)
        XCTAssertEqual(settings["max_scan_files"] as? Int, 37777)
    }

    func testRhythmsEncodeDailyWeeklyAndSundayWednesdayWithoutReplacingCustomSchedule() {
        XCTAssertEqual(RestorePlanRhythm.expression(.daily, hour: 5, minute: 15, weekday: 4), "15 5 * * *")
        XCTAssertEqual(RestorePlanRhythm.expression(.weekly, hour: 6, minute: 30, weekday: 0), "30 6 * * 0")
        XCTAssertEqual(RestorePlanRhythm.expression(.weekly, hour: 6, minute: 30, weekday: 6), "30 6 * * 6")
        XCTAssertEqual(RestorePlanRhythm.expression(.twiceWeekly, hour: 5, minute: 15, weekday: 6), "15 5 * * 0,3")
        XCTAssertNil(RestorePlanRhythm.expression(.custom, hour: 5, minute: 15, weekday: 3))
    }
}
