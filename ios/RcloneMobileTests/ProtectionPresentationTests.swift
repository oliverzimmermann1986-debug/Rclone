import Foundation
import XCTest
@testable import RcloneMobile

final class ProtectionPresentationTests: XCTestCase {
    private func pair(id: String = "photos-path", name: String = "Fotos", local: String = "/photos",
                      enabled: Bool = true) -> PairConfig {
        PairConfig(stableID: id, name: name, local: local, remote: "cloud:photos", enabled: enabled)
    }

    private func config(_ pairs: [PairConfig]) throws -> ConfigSnapshot {
        try StorePreviewData.load(bundle: Bundle(for: Self.self)).config.replacing(pairs: pairs, jobs: [])
    }

    private func storage(name: String = "Fotos", local: String = "/photos", state: String = "never") throws -> StorageOverview {
        let fields: [String: Any] = ["pairs": [["name": name, "local": local, "remote": "cloud:photos",
            "direction": "push", "restore_evidence": ["state": state, "checksum_verified": state == "passed",
            "valid": state == "passed", "valid_until": Date().timeIntervalSince1970 + 60]]]]
        return try JSONDecoder().decode(StorageOverview.self, from: JSONSerialization.data(withJSONObject: fields))
    }

    func testOpenPathUsesUpdatedProofInsteadOfTheSelectedSnapshot() throws {
        let original = try XCTUnwrap(storage().pairs.first)
        let fresh = try storage(state: "passed")
        let resolved = ProtectionPathSelection.resolve(original: original, dataPathID: "photos-path",
                                                       storage: fresh, config: try config([pair()]))
        XCTAssertTrue(try XCTUnwrap(resolved?.restoreEvidence).isCurrent)
        XCTAssertEqual(original.restoreEvidence?.state, "never")
    }

    func testStablePathIdentitySurvivesRename() throws {
        let original = try XCTUnwrap(storage().pairs.first)
        let resolved = ProtectionPathSelection.resolve(original: original, dataPathID: "photos-path",
            storage: try storage(name: "Familienbilder"), config: try config([pair(name: "Familienbilder")]))
        XCTAssertEqual(resolved?.name, "Familienbilder")
    }

    func testDeletedPathCannotResolveToCachedOrRecreatedSameName() throws {
        let original = try XCTUnwrap(storage().pairs.first)
        XCTAssertNil(ProtectionPathSelection.resolve(original: original, dataPathID: "photos-path",
                                                      storage: try storage(), config: try config([])))
        XCTAssertNil(ProtectionPathSelection.resolve(original: original, dataPathID: "photos-path",
            storage: try storage(), config: try config([pair(id: "replacement-path")])))
    }

    func testLegacySnapshotDoesNotResolveToChangedEndpoints() throws {
        let original = try XCTUnwrap(storage().pairs.first)
        XCTAssertNil(ProtectionPathSelection.resolve(original: original, dataPathID: nil,
            storage: try storage(local: "/different"), config: nil))
    }

    func testConfirmedRestoreCannotFollowRenamedChangedOrDisabledConfiguration() {
        let target = RestoreActionTarget(pair: pair())
        XCTAssertTrue(target.matches(pair()))
        XCTAssertFalse(target.matches(pair(id: "other-path")))
        XCTAssertFalse(target.matches(pair(name: "Neue Fotos")))
        XCTAssertFalse(target.matches(pair(local: "/changed")))
        XCTAssertFalse(target.matches(pair(enabled: false)))
    }

    func testCalendarAnnouncementIncludesFailuresCancellationsAndRestoreTests() {
        let day = RecoveryCalendarDay(date: "2026-10-03", total: 6, successful: 3, failed: 2,
                                      cancelled: 1, restoreTests: 2, state: "error")
        XCTAssertEqual(day.accessibilitySummary,
                       "2026-10-03. 6 Läufe: 3 erfolgreich, 2 fehlgeschlagen, 1 abgebrochen. 2 Restore-Prüfungen.")
    }
}
