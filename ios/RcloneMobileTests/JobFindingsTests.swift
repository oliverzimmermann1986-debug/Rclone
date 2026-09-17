import Foundation
import SwiftUI
import XCTest
@testable import RcloneMobile

final class JobFindingsTests: XCTestCase {
    private func job(status: String = "error", displayStatus: String? = nil, kind: String = "backup",
                     summary: [String: Any]? = nil) throws -> JobRecord {
        var fields: [String: Any] = ["id": 93, "kind": kind, "status": status, "started_at": 1]
        fields["summary"] = summary
        fields["display_status"] = displayStatus
        return try JSONDecoder().decode(JobRecord.self, from: JSONSerialization.data(withJSONObject: fields))
    }

    func testBackupPairErrorIncludesPathNameWithoutTopLevelError() throws {
        let record = try job(summary: ["pairs": [["name": "Rezepte", "ok": false,
            "error": "Verbindungsvorprüfung nach 15 Sekunden abgebrochen"]]])
        XCTAssertEqual(JobFindings.message(for: record),
            "Rezepte: Verbindungsvorprüfung nach 15 Sekunden abgebrochen")
    }

    func testMixedResultsKeepEveryFailureAndIgnoreSuccessfulPairOldError() throws {
        let record = try job(summary: ["pairs": [
            ["name": "Fotos", "ok": true, "error": "Alter Fehler"],
            ["name": "Rezepte", "ok": false, "error": "Zugriff verweigert"],
            ["name": "Dokumente", "ok": false, "error": "Zeitlimit erreicht"],
        ]])
        XCTAssertEqual(JobFindings.message(for: record),
            "Rezepte: Zugriff verweigert\n\nDokumente: Zeitlimit erreicht")
    }

    func testMissingBlankAndMalformedErrorsUseAnActionableFallback() throws {
        for summary in [nil, [:], ["error": " \n\t", "warning": " "], ["error": 42, "pairs": ["invalid"]]] as [[String: Any]?] {
            let message = try XCTUnwrap(JobFindings.message(for: job(summary: summary)))
            XCTAssertTrue(message.contains("fehlgeschlagen"))
            XCTAssertTrue(message.contains("kein Fehlergrund übermittelt"))
            XCTAssertTrue(message.contains("Protokoll öffnen"))
        }
    }

    func testFailedBlankPairStillIdentifiesAffectedPath() throws {
        let record = try job(summary: ["pairs": [["name": " Rezepte ", "ok": false, "error": " \n "]]])
        XCTAssertEqual(JobFindings.message(for: record),
            "Rezepte: Kein Fehlergrund übermittelt. Vollständiges Protokoll öffnen.")
        let timeout = try job(status: "timeout", summary: ["pairs": [["name": "Fotos", "status": "timeout"]]])
        XCTAssertEqual(JobFindings.message(for: timeout),
            "Fotos: Kein Fehlergrund übermittelt. Vollständiges Protokoll öffnen.")
        XCTAssertEqual(StatusStyle.color(for: JobFindings.severity(for: timeout)), Color.red)
    }

    func testWarningPrefersWarningAndKeepsHistoricalPartialText() throws {
        let message = "19 von 20 Dateien erfolgreich geprüft"
        let record = try job(displayStatus: "warning", kind: "restoretest", summary: ["pairs": [
            ["name": "Fotos", "ok": false, "warning": message, "error": ""],
            ["name": "restore-drill", "ok": false, "pairs_tested": 1],
        ]])
        XCTAssertEqual(JobFindings.message(for: record), "Fotos: \(message)")
        XCTAssertEqual(StatusStyle.color(for: JobFindings.severity(for: record)), Color.orange)
        let historical = try job(displayStatus: "warning", kind: "restoretest", summary: ["pairs": [
            ["name": "Fotos", "ok": false, "error": "Teil-Stichprobe: \(message)"],
        ]])
        XCTAssertEqual(JobFindings.message(for: historical), "Fotos: Teil-Stichprobe: \(message)")
        let realFailure = try job(kind: "restoretest", summary: ["pairs": [
            ["name": "Fotos", "ok": false, "error": "Teil-Stichprobe: Bereinigung fehlgeschlagen"],
        ]])
        XCTAssertEqual(StatusStyle.color(for: JobFindings.severity(for: realFailure)), Color.red)
        XCTAssertEqual(JobFindings.message(for: try job(status: "partial")),
            "Der Prüfumfang ist begrenzt. Vollständiges Protokoll öffnen.")
    }

    func testWarningsArrayAndTopLevelErrorStayReadable() throws {
        let record = try job(summary: ["error": " Hauptfehler \n", "warnings": ["Warnung A", " ", "Warnung B"]])
        XCTAssertEqual(JobFindings.message(for: record), "Hauptfehler\n\nWarnung A\n\nWarnung B")
        let warning = try job(status: "warning", summary: ["warning": "Hinweis", "error": "Details"])
        XCTAssertEqual(JobFindings.message(for: warning), "Hinweis\n\nDetails")
    }

    func testDuplicateFindingsKeepPathContextAndDifferentPathsRemainDistinct() throws {
        let record = try job(summary: ["error": "Zugriff verweigert", "warning": "Fotos: Zugriff verweigert", "pairs": [
            ["name": "Fotos", "ok": false, "error": " Zugriff verweigert ", "warning": "Zugriff  verweigert"],
            ["name": "Fotos", "ok": false, "error": "Fotos: Zugriff verweigert"],
            ["name": "Rezepte", "ok": false, "error": "Zugriff verweigert"],
        ]])
        XCTAssertEqual(JobFindings.message(for: record), "Fotos: Zugriff verweigert\n\nRezepte: Zugriff verweigert")
    }

    func testSuccessfulAndRunningJobsNeverShowOldErrors() throws {
        for status in ["ok", "running", "skipped"] {
            let record = try job(status: status, summary: ["error": "Alter Fehler", "pairs": [
                ["name": "Fotos", "ok": false, "error": "Alter Datenwegfehler"],
            ]])
            XCTAssertNil(JobFindings.message(for: record))
        }
    }

    func testSuccessfulJobWarningsStayVisibleWithoutOldErrorsAndHaveWarningSeverity() throws {
        let record = try job(status: "ok", summary: ["error": "Alter Fehler", "warning": "Hinweis zum Lauf", "pairs": [
            ["name": "Fotos", "ok": true, "error": "Alter Datenwegfehler",
             "warning": "Sicherung erfolgreich; vollständiger Recovery-Stand konnte nicht erstellt werden"],
        ]])
        XCTAssertEqual(JobFindings.severity(for: record), "warning")
        XCTAssertEqual(StatusStyle.color(for: JobFindings.severity(for: record)), Color.orange)
        XCTAssertEqual(record.effectiveStatus, "ok")
        XCTAssertEqual(JobFindings.message(for: record),
            "Fotos: Sicherung erfolgreich; vollständiger Recovery-Stand konnte nicht erstellt werden\n\nHinweis zum Lauf")
        let warningsOnly = try job(status: "ok", summary: ["warnings": ["Scanlimit erreicht"]])
        XCTAssertEqual(JobFindings.severity(for: warningsOnly), "warning")
        XCTAssertEqual(JobFindings.message(for: warningsOnly), "Scanlimit erreicht")
        XCTAssertEqual(JobFindings.severity(for: try job(status: "ok", summary: ["warning": " \n "])), "ok")
    }

    func testRestoreAggregateDoesNotInventAnAdditionalFailure() throws {
        let record = try job(kind: "restoretest", summary: ["pairs": [
            ["name": "Fotos", "ok": false, "error": "Prüfsumme stimmt nicht überein"],
            ["name": "restore-drill", "ok": false, "pairs_tested": 1],
        ]])
        XCTAssertEqual(JobFindings.message(for: record), "Fotos: Prüfsumme stimmt nicht überein")
    }
}
