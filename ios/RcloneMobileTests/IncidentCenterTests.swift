import Foundation
import SwiftUI
import XCTest
@testable import RcloneMobile

final class IncidentCenterTests: XCTestCase {
    private func overview(health: [PairHealth] = [], alerts: [SystemAlert] = [], last: JobRecord? = nil) throws -> OverviewResponse {
        let fixture = try StorePreviewData.load(bundle: Bundle(for: Self.self)).overview
        return OverviewResponse(app: fixture.app, system: fixture.system, services: fixture.services,
            pairs: PairSummary(total: health.count, enabled: health.count, scheduled: 0, manual: 0, destructive: 0, health: health),
            jobs: JobOverview(last: last, lastSuccess: nil, lastError: nil), alerts: alerts, generatedAt: fixture.generatedAt)
    }

    private func health(error: String, status: String = "error", jobID: Int = 81) throws -> PairHealth {
        let fields: [String: Any] = ["name": "Fotos", "history_key": "photos", "direction": "push",
            "mode": "copy", "last_status": status, "job_id": jobID, "error": error]
        return try JSONDecoder().decode(PairHealth.self, from: JSONSerialization.data(withJSONObject: fields))
    }

    private func makeStorage(proof: [String: Any], name: String = "Fotos") throws -> StorageOverview {
        let fields: [String: Any] = ["pairs": [["name": name, "local": "/photos", "remote": "cloud:photos", "restore_evidence": proof]]]
        return try JSONDecoder().decode(StorageOverview.self, from: JSONSerialization.data(withJSONObject: fields))
    }

    private var partialProof: [String: Any] {
        ["state": "partial", "valid": false, "checksum_verified": false, "verified_files": 19,
            "sample_size": 19, "requested_sample_size": 20, "budget_bytes": 268_435_456,
            "sample_shortfall_reason": "byte_budget", "warning": "Teil-Stichprobe", "job_id": 81]
    }

    func testBlankHealthErrorsAndSuccessfulHealthNeverCreateEmptyOrStaleIncidents() throws {
        let pairs = try [health(error: "", status: "ok"), health(error: " \n \t", status: "skipped"),
            health(error: "Alter Fehler", status: "ok"), health(error: "Abgebrochen", status: "cancelled")]
        XCTAssertTrue(ProtectionIncident.collect(overview: try overview(health: pairs), storage: nil).isEmpty)
    }

    func testBlankFailedHealthUsesFallbackAndPreservesRunLink() throws {
        for status in ["error", "failed", "timeout", "stale", " ERROR "] {
            let model = try overview(health: [health(error: " \n \t", status: status, jobID: 93)])
            let incidents = ProtectionIncident.collect(overview: model, storage: nil)
            XCTAssertEqual(incidents.count, 1)
            let incident = try XCTUnwrap(incidents.first)
            XCTAssertEqual(incident.message, "Fotos: Der letzte Lauf dieses Datenwegs ist fehlgeschlagen. Protokoll prüfen.")
            XCTAssertEqual(incident.severity, "error")
            XCTAssertEqual(incident.pairName, "Fotos")
            XCTAssertEqual(incident.jobID, 93)
        }
    }

    func testSkippedPrecheckFailureRemainsVisible() throws {
        let model = try overview(health: [health(error: "Pre-Check fehlgeschlagen (remote: Timeout nach 15s / local: ok)", status: "skipped", jobID: 93)])
        let incident = try XCTUnwrap(ProtectionIncident.collect(overview: model, storage: nil).first)
        XCTAssertTrue(incident.message.contains("Timeout nach 15s"))
        XCTAssertEqual(incident.severity, "error")
        XCTAssertEqual(incident.jobID, 93)
    }

    func testBlankRestoreErrorUsesActionableFailureFallback() throws {
        for error in ["", " \n "] {
            let storage = try makeStorage(proof: ["state": "failed", "checksum_verified": false, "error": error, "job_id": 82])
            let incident = try XCTUnwrap(ProtectionIncident.collect(overview: try overview(), storage: storage).first)
            XCTAssertEqual(incident.message, "Fotos: Restore-Prüfung fehlgeschlagen. Prüfprotokoll öffnen.")
            XCTAssertEqual(incident.severity, "error")
            XCTAssertEqual(incident.color, Color.red)
            XCTAssertEqual(incident.jobID, 82)
        }
    }

    func testLimitedSampleShowsRequestedCountBudgetAndOrangeCategoryWithoutValidProof() throws {
        let storage = try makeStorage(proof: partialProof)
        let proof = try XCTUnwrap(storage.pairs.first?.restoreEvidence)
        XCTAssertFalse(proof.isCurrent)
        XCTAssertFalse(proof.checksumVerified)
        XCTAssertEqual(proof.sampleScope.countDescription, "19 von 20 Dateien")
        XCTAssertEqual(proof.sampleScope.budgetDescription, "256 MiB")
        let incident = try XCTUnwrap(ProtectionIncident.collect(overview: try overview(), storage: storage).first)
        XCTAssertEqual(incident.category, "Prüfumfang")
        XCTAssertEqual(incident.severity, "warning")
        XCTAssertEqual(incident.color, Color.orange)
        XCTAssertTrue(incident.message.contains("19 von 20"))
        XCTAssertTrue(incident.message.contains("256 MiB"))
        XCTAssertFalse(incident.message.contains("19 von 19"))
        XCTAssertTrue(incident.recommendation.contains("bei 20 Dateien"))
        XCTAssertTrue(incident.recommendation.contains("nur bewusst"))
        XCTAssertEqual(incident.pairName, "Fotos")
        XCTAssertEqual(incident.jobID, 81)
    }

    func testDuplicateMergesNavigationContextAndMaximumSeverity() throws {
        let alerts = [SystemAlert(level: "warning", message: "Fotos: Zugriff verweigert"),
            SystemAlert(level: "error", message: " Fotos: Zugriff verweigert ")]
        let model = try overview(health: [health(error: "Zugriff verweigert", jobID: 91)], alerts: alerts)
        let incidents = ProtectionIncident.collect(overview: model, storage: nil)
        XCTAssertEqual(incidents.count, 1)
        let incident = try XCTUnwrap(incidents.first)
        XCTAssertEqual(incident.severity, "error")
        XCTAssertEqual(incident.pairName, "Fotos")
        XCTAssertEqual(incident.jobID, 91)
    }

    func testDifferentErrorsInSameJobAndCategoryRemainVisible() throws {
        let health = try [self.health(error: "Zugriff verweigert", jobID: 81),
            self.health(error: "Lesen fehlgeschlagen", jobID: 81)]
        let incidents = ProtectionIncident.collect(overview: try overview(health: health), storage: nil)
        XCTAssertEqual(incidents.count, 2)
        XCTAssertEqual(Set(incidents.map(\.message)), Set(["Fotos: Zugriff verweigert", "Fotos: Lesen fehlgeschlagen"]))
        XCTAssertTrue(incidents.allSatisfy { $0.jobID == 81 && $0.pairName == "Fotos" && $0.severity == "error" })
    }

    func testIdenticalErrorsFromDifferentJobsKeepTheirOwnContextAndIdentity() throws {
        let alerts = [SystemAlert(level: "error", message: "Fotos: Zugriff verweigert", jobID: 81),
            SystemAlert(level: "error", message: "Fotos: Zugriff verweigert", jobID: 82)]
        let model = try overview(health: [health(error: "Zugriff verweigert", jobID: 82)], alerts: alerts)
        let incidents = ProtectionIncident.collect(overview: model, storage: nil)
        XCTAssertEqual(incidents.count, 2)
        XCTAssertEqual(Set(incidents.compactMap(\.jobID)), Set([81, 82]))
        XCTAssertEqual(Set(incidents.map(\.id)).count, 2)
        XCTAssertEqual(incidents.first(where: { $0.jobID == 82 })?.pairName, "Fotos")
        XCTAssertNil(incidents.first(where: { $0.jobID == 81 })?.pairName)
    }

    func testDifferentGlobalLimitedSampleMessagesDoNotMergeWithoutPairDetail() throws {
        let alerts = [SystemAlert(level: "warning", message: "Prüfumfang begrenzt: Datenlimit erreicht", jobID: 81),
            SystemAlert(level: "warning", message: "Prüfumfang begrenzt: Dateiliste unvollständig", jobID: 81)]
        let incidents = ProtectionIncident.collect(overview: try overview(alerts: alerts), storage: nil)
        XCTAssertEqual(incidents.count, 2)
        XCTAssertEqual(Set(incidents.map(\.message)), Set(alerts.map(\.message)))
    }

    func testExplicitAlertJobAndExactLegacyFallbackNeverAttachUnrelatedAlerts() throws {
        let job = try JSONDecoder().decode(JobRecord.self, from: Data(#"{"id":81,"kind":"restoretest","status":"error","started_at":1}"#.utf8))
        let alerts = [SystemAlert(level: "error", message: "Der letzte Job ist fehlgeschlagen."),
            SystemAlert(level: "error", message: "Server nicht erreichbar"),
            SystemAlert(level: "warning", message: "Prüfumfang begrenzt", jobID: 82, kind: "restoretest", source: "last_job")]
        let incidents = ProtectionIncident.collect(overview: try overview(alerts: alerts, last: job), storage: nil)
        XCTAssertEqual(incidents.first(where: { $0.message == alerts[0].message })?.jobID, 81)
        XCTAssertNil(incidents.first(where: { $0.message == alerts[1].message })?.jobID)
        XCTAssertEqual(incidents.first(where: { $0.message == alerts[2].message })?.jobID, 82)
        let current = try JSONDecoder().decode(JobRecord.self, from: Data(#"{"id":83,"kind":"backup","status":"ok","started_at":1}"#.utf8))
        XCTAssertNil(ProtectionIncident.collect(overview: try overview(alerts: [alerts[0]], last: current), storage: nil).first?.jobID)
        let oldWithoutPeriod = SystemAlert(level: "error", message: "Der letzte Job ist fehlgeschlagen")
        XCTAssertEqual(ProtectionIncident.collect(overview: try overview(alerts: [oldWithoutPeriod], last: job), storage: nil).first?.jobID, 81)
    }

    func testSameJobPartialSummaryMergesIntoDetailedPairFinding() throws {
        let alerts = [SystemAlert(level: "warning", message: "Letzte Restore-Prüfung: Prüfumfang begrenzt.",
            jobID: 81, kind: "restoretest", source: "last_job")]
        let incidents = ProtectionIncident.collect(overview: try overview(alerts: alerts), storage: try makeStorage(proof: partialProof))
        XCTAssertEqual(incidents.count, 1)
        let incident = try XCTUnwrap(incidents.first)
        XCTAssertEqual(incident.pairName, "Fotos")
        XCTAssertEqual(incident.jobID, 81)
        XCTAssertTrue(incident.message.contains("19 von 20"))
        XCTAssertTrue(incident.recommendation.contains("bei 20 Dateien"))
    }

    func testDifferentPairsInSameRestoreJobKeepSeparateFindings() throws {
        let photos = try makeStorage(proof: partialProof)
        let recipes = try makeStorage(proof: partialProof, name: "Rezepte")
        let storage = StorageOverview(pairs: photos.pairs + recipes.pairs)
        let alerts = [SystemAlert(level: "warning", message: "Prüfumfang begrenzt", jobID: 81)]
        let incidents = ProtectionIncident.collect(overview: try overview(alerts: alerts), storage: storage)
        XCTAssertEqual(incidents.count, 2)
        XCTAssertEqual(Set(incidents.compactMap(\.pairName)), Set(["Fotos", "Rezepte"]))
        XCTAssertTrue(incidents.allSatisfy { $0.jobID == 81 && $0.severity == "warning" })
    }

    func testRealFailureWithPartialWordingStaysRed() throws {
        let storage = try makeStorage(proof: ["state": "failed", "checksum_verified": false,
            "error": "Teil-Stichprobe: Aufräumen fehlgeschlagen", "verified_files": 19, "sample_size": 19])
        let incident = try XCTUnwrap(ProtectionIncident.collect(overview: try overview(), storage: storage).first)
        XCTAssertEqual(incident.severity, "error")
        XCTAssertEqual(incident.color, Color.red)
        XCTAssertEqual(incident.category, "Restore-Test")
        XCTAssertTrue(incident.recommendation.contains("Bereinigung"))
        XCTAssertFalse(try XCTUnwrap(storage.pairs.first?.restoreEvidence).sampleScope.isPartial)
    }

    func testNewProofFieldsDecodeInBothEndpointsAndOldResponsesRemainCompatible() throws {
        let data = try JSONSerialization.data(withJSONObject: partialProof)
        let storage = try JSONDecoder().decode(RestoreEvidence.self, from: data)
        let recovery = try JSONDecoder().decode(RecoveryRestoreProof.self, from: data)
        XCTAssertEqual(storage.requestedSampleSize, recovery.requestedSampleSize)
        XCTAssertEqual(storage.budgetBytes, recovery.budgetBytes)
        XCTAssertEqual(storage.sampleShortfallReason, recovery.sampleShortfallReason)
        XCTAssertEqual(storage.warning, recovery.warning)
        XCTAssertFalse(recovery.isCurrent)
        let old = Data(#"{"state":"passed","checksum_verified":true,"sample_size":20,"verified_files":20}"#.utf8)
        let legacy = try JSONDecoder().decode(RestoreEvidence.self, from: old)
        let oldRecovery = try JSONDecoder().decode(RecoveryRestoreProof.self, from: old)
        XCTAssertNil(legacy.requestedSampleSize)
        XCTAssertNil(oldRecovery.warning)
        XCTAssertEqual(legacy.sampleScope.countDescription, "20 von 20 Dateien")
        let oldAlert = try JSONDecoder().decode(SystemAlert.self, from: Data(#"{"level":"error","message":"Fehler"}"#.utf8))
        XCTAssertNil(oldAlert.jobID)
        XCTAssertNil(oldAlert.source)
    }

    func testHistoricalDisplayStatusDoesNotRewriteStoredJobOutcome() throws {
        let data = Data(#"{"id":81,"kind":"restoretest","status":"error","display_status":"warning","started_at":1}"#.utf8)
        let job = try JSONDecoder().decode(JobRecord.self, from: data)
        XCTAssertEqual(job.status, "error")
        XCTAssertEqual(job.effectiveStatus, "warning")
        let old = Data(#"{"id":80,"kind":"backup","status":"error","started_at":1}"#.utf8)
        XCTAssertEqual(try JSONDecoder().decode(JobRecord.self, from: old).effectiveStatus, "error")
    }
}
