import Foundation
import XCTest
@testable import RcloneMobile

final class StorageMeasurementPresentationTests: XCTestCase {
    private let cloudTarget = StorageMeasurementPresentation.Context(isLocal: false, isSource: false)
    private let notice = "2026/11/16 08:00:00 NOTICE: Google Drive uses a shared client_id which will stop working in a future release. Configure your own Google Drive client_id."

    private func size(
        status: String? = nil, error: String? = nil, measurementError: String? = nil,
        count: Int? = 12, bytes: Int64? = 4096, measuredAt: Double? = 1_700_000_000
    ) -> PathSize {
        PathSize(path: "demo:backup", count: count, bytes: bytes, error: error,
            measuredAt: measuredAt, measurementStatus: status, measurementError: measurementError)
    }

    private func present(_ value: PathSize?) -> StorageMeasurementPresentation {
        StorageMeasurementPresentation(size: value, context: cloudTarget)
    }

    func testConcreteFailureAfterLongNoticeWinsAndFullOriginalIsRetained() {
        let full = notice + String(repeating: " More information about the shared client_id.", count: 12)
            + "\n2026/11/16 08:00:01 ERROR : error listing: directory not found"
        let value = present(size(error: full, count: nil, bytes: nil))
        XCTAssertEqual(value.issue, .directoryMissing)
        XCTAssertEqual(value.message, "Cloud-Ziel nicht gefunden. Zielpfad und Zugriff prüfen.")
        XCTAssertEqual(value.technicalDetails, full)
        XCTAssertFalse(value.message?.contains("client_id") == true)
        XCTAssertTrue(value.isWarning)
    }

    func testBothErrorFieldsParticipateRegardlessOfWhichContainsTheNotice() {
        for fields in [(notice, "googleapi: Error 403: userRateLimitExceeded"), ("googleapi: Error 403: userRateLimitExceeded", notice)] {
            let value = present(size(error: fields.0, measurementError: fields.1))
            XCTAssertEqual(value.issue, .rateLimited)
            XCTAssertTrue(value.technicalDetails?.contains(fields.0) == true)
            XCTAssertTrue(value.technicalDetails?.contains(fields.1) == true)
        }
    }

    func testConcreteRateLimitSignalsAreRecognizedBeforeGeneric403() {
        for error in ["rateLimitExceeded", "userRateLimitExceeded", "HTTP 429", "HTTP/2 429",
            "429 Too Many Requests", "googleapi: Error 403: Rate Limit Exceeded",
            #"googleapi: Error 403: Quota exceeded, "reason": "RATE_LIMIT_EXCEEDED""#,
            #"{"error":{"code":429,"message":"Request rejected"}}"#,
            "Quota exceeded for quota metric 'Queries' and limit 'Queries per minute per user'",
            "Requests per minute limit reached"] {
            let value = present(size(error: notice + "\nERROR : " + error))
            XCTAssertEqual(value.issue, .rateLimited, error)
            XCTAssertEqual(value.freshness, .outdated, error)
        }
    }

    func testStorageQuotaIsDifferentFromApiRateLimiting() {
        for error in ["googleapi: Error 403: storageQuotaExceeded", "Storage quota exceeded",
            "No space left on device", "Disk quota exceeded"] {
            let value = present(size(error: notice + "\nERROR : " + error))
            XCTAssertEqual(value.issue, .storageFull, error)
            XCTAssertFalse(value.message?.contains("Anfragelimit") == true)
        }
        XCTAssertEqual(present(size(error: "Quota exceeded")).issue, .unknown)
        XCTAssertEqual(present(size(error: "googleapi: Error 403: Quota exceeded")).issue, .unknown)
    }

    func testAuthenticationAndAccessErrorsRemainWarningsWithoutStatusField() {
        for error in ["HTTP 401", "googleapi: Error 403: Forbidden", "invalid_grant", "Permission denied", "AccessDenied"] {
            let value = present(size(error: notice + "\nERROR : " + error))
            XCTAssertEqual(value.issue, .accessDenied, error)
            XCTAssertTrue(value.isWarning)
            XCTAssertEqual(value.freshnessLabel, "Nicht frisch")
        }
    }

    func testTimeoutWinsOverClientWarning() {
        for error in ["Timeout nach 30s", "context deadline exceeded", "connection timed out"] {
            XCTAssertEqual(present(size(error: notice + "\nERROR : " + error)).issue, .timeout)
        }
    }

    func testLocalFailuresPointToTheServerInsteadOfACloudLogin() {
        let local = StorageMeasurementPresentation.Context(isLocal: true, isSource: true)
        let full = StorageMeasurementPresentation(size: size(error: "No space left on device"), context: local)
        XCTAssertEqual(full.issue, .storageFull)
        XCTAssertTrue(full.message?.contains("auf dem Server") == true)
        let denied = StorageMeasurementPresentation(size: size(error: "Permission denied"), context: local)
        XCTAssertEqual(denied.issue, .accessDenied)
        XCTAssertTrue(denied.message?.contains("Ordnerberechtigungen") == true)
        XCTAssertFalse(denied.message?.contains("Anmeldung") == true)
    }

    func testTruncatedOldBackendNoticeDoesNotInventTheHiddenFailure() {
        let truncated = String((notice + String(repeating: " Shared Google client_id information.", count: 20)).prefix(300))
        let value = present(size(error: truncated, count: nil, bytes: nil))
        XCTAssertEqual(value.issue, .sharedGoogleClient)
        XCTAssertTrue(value.message?.contains("Google-Zugang aktualisieren") == true)
        XCTAssertFalse(value.message?.contains("nicht gefunden") == true)
        XCTAssertEqual(value.technicalDetails, truncated)
    }

    func testExpiryIsOnlyMentionedWhenTheNoticeActuallySaysSo() {
        XCTAssertTrue(present(size(error: notice)).message?.contains("läuft aus") == true)
        let value = present(size(error: "NOTICE: shared Google Drive client_id"))
        XCTAssertEqual(value.issue, .sharedGoogleClient)
        XCTAssertFalse(value.message?.contains("läuft aus") == true)
    }

    func testUnknownErrorAfterNoticeNeverBecomesHealthyOrJustANotice() {
        let value = present(size(status: "fresh", error: notice + "\nERROR : unexpected provider response abc123"))
        XCTAssertEqual(value.issue, .unknown)
        XCTAssertEqual(value.freshness, .outdated)
        XCTAssertTrue(value.isWarning)
        XCTAssertTrue(value.hasDetails)
    }

    func testStaleMeasurementsKeepBothValuesAndTheirOriginalTimestamp() {
        let value = present(size(status: "stale", measurementError: "HTTP 429"))
        XCTAssertEqual(value.count, 12)
        XCTAssertEqual(value.bytes, 4096)
        XCTAssertEqual(value.measuredAt, 1_700_000_000)
        XCTAssertEqual(value.freshnessLabel, "Nicht frisch")
        XCTAssertTrue(value.isWarning)
    }

    func testLegacyErrorWithCountsCannotBePresentedAsFresh() {
        let value = present(size(error: "directory not found"))
        XCTAssertEqual(value.count, 12)
        XCTAssertEqual(value.bytes, 4096)
        XCTAssertEqual(value.freshness, .outdated)
        XCTAssertTrue(value.isWarning)
    }

    func testPartialCountsWithoutStatusOrErrorAreExplicitlyNotFresh() {
        let value = present(size(bytes: nil))
        XCTAssertEqual(value.count, 12)
        XCTAssertNil(value.bytes)
        XCTAssertEqual(value.freshness, .partial)
        XCTAssertEqual(value.freshnessLabel, "Nicht frisch")
        XCTAssertTrue(value.message?.contains("unvollständig") == true)
        XCTAssertTrue(value.isWarning)
    }

    func testExplicitPartialOrFailedStatusWarnsWithoutTechnicalText() {
        for status in ["partial", "failed", "stale", "new-unknown-status"] {
            let value = present(size(status: status))
            XCTAssertTrue(value.isWarning, status)
            XCTAssertNotNil(value.message, status)
            XCTAssertEqual(value.freshnessLabel, "Nicht frisch", status)
        }
    }

    func testSuccessfulLegacyMeasurementUsesCachedLabelAndPreservesMeasuredZero() {
        let value = present(size(count: 0, bytes: 0))
        XCTAssertEqual(value.count, 0)
        XCTAssertEqual(value.bytes, 0)
        XCTAssertEqual(value.freshness, .cached)
        XCTAssertFalse(value.isWarning)
        XCTAssertNil(value.message)
        XCTAssertEqual(present(size(measuredAt: nil)).freshness, .outdated)
    }

    func testFreshAndPendingResponsesDoNotRequireTechnicalDetails() {
        XCTAssertEqual(present(size(status: "fresh")).freshness, .fresh)
        XCTAssertEqual(present(nil).freshness, .unmeasured)
        let loading = StorageMeasurementPresentation(size: nil, context: cloudTarget, isMeasuring: true)
        XCTAssertEqual(loading.freshness, .measuring)
        XCTAssertFalse(loading.hasDetails)
    }

    func testFullTechnicalTextIsNotTrimmedAndDuplicateFieldsAreNotRepeated() {
        let text = "  ERROR : Unexpected response\nOriginal provider detail  \n"
        let value = present(size(error: text, measurementError: text))
        XCTAssertEqual(value.technicalDetails, text)
        XCTAssertEqual(value.issue, .unknown)
        XCTAssertNil(present(size(error: " \n", measurementError: "\t")).issue)
    }

    func testMissingDirectoryExplanationUsesSourceTargetAndBidirectionalContext() {
        let cases: [(StorageMeasurementPresentation.Context, String)] = [
            (.init(isLocal: true, isSource: true), "Quellordner"),
            (.init(isLocal: false, isSource: true), "Cloud-Quelle"),
            (.init(isLocal: true, isSource: false), "Lokales Ziel"),
            (.init(isLocal: false, isSource: false), "Cloud-Ziel"),
            (.init(isLocal: false, isSource: true, isBidirectional: true), "beiden Seiten")
        ]
        for (context, expected) in cases {
            let value = StorageMeasurementPresentation(size: size(error: "directory not found"), context: context)
            XCTAssertTrue(value.message?.contains(expected) == true)
            XCTAssertFalse(value.message?.contains("ersten") == true)
        }
    }

    func testPullCloudEndpointIsASourceAndLocalEndpointIsATarget() throws {
        let data = Data(#"{"name":"Demo","local":"/demo","remote":"cloud:demo","direction":"pull"}"#.utf8)
        let pair = try JSONDecoder().decode(StoragePair.self, from: data)
        let cloud = StorageMeasurementPresentation.Context(path: "cloud:demo", pair: pair)
        let local = StorageMeasurementPresentation.Context(path: "/demo", pair: pair)
        XCTAssertTrue(cloud.isSource)
        XCTAssertFalse(cloud.isLocal)
        XCTAssertFalse(local.isSource)
        XCTAssertTrue(local.isLocal)
    }

    func testMissingLastSyncOnlyDescribesMissingProof() {
        XCTAssertEqual(StorageMeasurementPresentation.missingSuccessNote(lastSync: nil),
            "Für diesen Datenweg ist noch keine erfolgreiche Sicherung belegt.")
        XCTAssertNil(StorageMeasurementPresentation.missingSuccessNote(lastSync: 1_700_000_000))
    }
}
