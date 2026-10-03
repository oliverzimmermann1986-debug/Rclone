import UIKit
import XCTest

/// Exercises the shipped navigation against the existing offline preview.
/// The preview refuses server writes; confirmation dialogs are always cancelled.
final class RecoveryNavigationUITests: XCTestCase {
    override func setUpWithError() throws {
        continueAfterFailure = false
    }

    @MainActor
    func testRecoveryTasksReachExistingFeaturesWithoutStartingAWrite() {
        let app = launchPreview(destination: "recovery")
        attachScreenshot(app, name: "Recovery task entries with standard text")

        tap(app.buttons["recoverFilesLink"], in: app)
        tap(app.buttons["recoveryPath-Fotos"], in: app)
        let restore = app.navigationBars.buttons["Wiederherstellen"]
        XCTAssertTrue(restore.waitForExistence(timeout: 5))
        XCTAssertFalse(restore.isEnabled, "Restoring requires an explicit file selection.")
        tap(app.buttons["recoveryFile-Beispiel.pdf"], in: app)
        XCTAssertTrue(restore.isEnabled)
        restore.tap()
        XCTAssertTrue(app.buttons["Getrennt wiederherstellen"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@",
                         "Produktive Quell- und Zielpfade werden nicht verändert")).firstMatch.exists)
        dismissConfirmation(in: app)
        XCTAssertTrue(app.buttons["recoveryFile-Beispiel.pdf"].exists)
        goBack(in: app)
        goBack(in: app)

        tap(app.buttons["verifyRestoreLink"], in: app)
        tap(app.buttons["recoveryPath-Fotos"], in: app)
        reveal(app.staticTexts["RTO-Stichprobe"], in: app)
        let drill = app.buttons["restoreTestActionButton"]
        reveal(drill, in: app)
        XCTAssertTrue(drill.exists)
        XCTAssertFalse(drill.isEnabled, "The store preview must not start a server restore test.")
        goBack(in: app)
        goBack(in: app)

        tap(app.buttons["serverLossLink"], in: app)
        XCTAssertTrue(app.navigationBars["Serververlust"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["Datei öffnen …"].exists)
        goBack(in: app)

        tap(app.buttons["restorePlanLink"], in: app)
        XCTAssertTrue(app.navigationBars["Restore-Prüfplan"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.staticTexts[
            "Verbinde dich mit deinem Server, um den Prüfplan zu sehen und zu ändern."
        ].exists)
        XCTAssertFalse(app.buttons["saveRestorePlan"].exists)
        attachScreenshot(app, name: "Restore plan in offline preview")
    }

    @MainActor
    func testLargestTextKeepsDashboardMetricsAndActionsAccessible() {
        let app = launchPreview(largeText: true)
        let active = identified("protectionMetric-Datenwege", in: app)
        let scheduled = identified("protectionMetric-Geplant", in: app)
        let restoreMetric = identified("protectionMetric-Restore", in: app)
        XCTAssertTrue(active.waitForExistence(timeout: 10))
        XCTAssertTrue(scheduled.exists)
        XCTAssertTrue(restoreMetric.exists)
        XCTAssertGreaterThan(active.frame.height, 0)
        XCTAssertGreaterThanOrEqual(scheduled.frame.minY, active.frame.maxY - 1,
                                   "Accessibility text sizes need vertical metrics.")
        XCTAssertGreaterThanOrEqual(restoreMetric.frame.minY, scheduled.frame.maxY - 1)
        attachScreenshot(app, name: "Dashboard with largest accessibility text")

        // The fixture omits a current proof validity flag. This action is present
        // independently of the date, and must remain disabled in the offline demo.
        let restore = app.buttons["restoreTestActionButton"]
        let assessment = app.buttons["protectionAssessmentButton"]
        reveal(restore, in: app)
        XCTAssertTrue(restore.exists)
        XCTAssertFalse(restore.isEnabled)
        XCTAssertTrue(restore.label.contains("Fotos"))
        reveal(assessment, in: app)
        XCTAssertTrue(assessment.isHittable)
        attachScreenshot(app, name: "Assessment label before center tap with largest text")
        // Exercise the label rectangle itself, not an icon or a selected glyph.
        assessment.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5)).tap()
        XCTAssertTrue(app.navigationBars["Schutznachweis"].waitForExistence(timeout: 5))
        app.buttons["Fertig"].tap()
        XCTAssertTrue(app.navigationBars["Lage"].waitForExistence(timeout: 5))

        tap(app.tabBars.buttons["Wiederherstellen"], in: app)
        tap(app.buttons["recoverFilesLink"], in: app)
        XCTAssertTrue(app.navigationBars["Dateien zurückholen"].waitForExistence(timeout: 5))
        tap(app.buttons["recoveryPath-Fotos"], in: app)
        reveal(app.buttons["recoveryFile-Beispiel.pdf"], in: app)
        goBack(in: app)
        goBack(in: app)

        tap(app.buttons["verifyRestoreLink"], in: app)
        XCTAssertTrue(app.navigationBars["Wiederherstellbarkeit prüfen"].waitForExistence(timeout: 5))
        tap(app.buttons["recoveryPath-Fotos"], in: app)
        reveal(app.staticTexts["RTO-Stichprobe"], in: app)
        goBack(in: app)
        goBack(in: app)

        tap(app.buttons["serverLossLink"], in: app)
        XCTAssertTrue(app.navigationBars["Serververlust"].waitForExistence(timeout: 5))
        goBack(in: app)

        tap(app.buttons["restorePlanLink"], in: app)
        XCTAssertTrue(app.navigationBars["Restore-Prüfplan"].waitForExistence(timeout: 5))
        attachScreenshot(app, name: "Restore plan with largest accessibility text")
    }

    @MainActor
    private func launchPreview(destination: String = "dashboard", largeText: Bool = false) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["--store-preview", destination, "-AppleLanguages", "(de)", "-AppleLocale", "de_DE"]
        if largeText {
            app.launchArguments += ["-UIPreferredContentSizeCategoryName",
                                    UIContentSizeCategory.accessibilityExtraExtraExtraLarge.rawValue]
        }
        app.launch()
        let title = destination == "recovery" ? "Wiederherstellen" : "Lage"
        XCTAssertTrue(app.navigationBars[title].waitForExistence(timeout: 15))
        return app
    }

    @MainActor
    private func identified(_ identifier: String, in app: XCUIApplication) -> XCUIElement {
        app.descendants(matching: .any).matching(identifier: identifier).firstMatch
    }

    @MainActor
    private func tap(_ element: XCUIElement, in app: XCUIApplication,
                     file: StaticString = #filePath, line: UInt = #line) {
        // SwiftUI List may create an offscreen row only after it is scrolled in.
        // Existence is checked after scrolling, before any requested action.
        reveal(element, in: app, file: file, line: line)
        for _ in 0..<12 {
            if element.isHittable { break }
            app.swipeUp()
        }
        XCTAssertTrue(element.isHittable, file: file, line: line)
        element.tap()
    }

    @MainActor
    private func reveal(_ element: XCUIElement, in app: XCUIApplication,
                        file: StaticString = #filePath, line: UInt = #line) {
        for _ in 0..<12 {
            if element.exists && !element.frame.isEmpty && element.frame.intersects(app.frame) { break }
            app.swipeUp()
        }
        XCTAssertTrue(element.exists, file: file, line: line)
    }

    @MainActor
    private func dismissConfirmation(in app: XCUIApplication) {
        let confirmation = app.buttons["Getrennt wiederherstellen"]
        // iOS 26 renders this toolbar confirmation as a popover. Its native
        // dismissal region replaces the cancel row used by an action sheet.
        let popoverDismissal = app.otherElements["PopoverDismissRegion"]
        if popoverDismissal.exists {
            XCTAssertTrue(popoverDismissal.isHittable)
            popoverDismissal.tap()
        } else {
            let cancel = app.buttons.matching(NSPredicate(format: "label IN %@", ["Abbrechen", "Cancel"])).firstMatch
            XCTAssertTrue(cancel.waitForExistence(timeout: 5))
            cancel.tap()
        }
        let dismissed = XCTNSPredicateExpectation(predicate: NSPredicate(format: "exists == false"), object: confirmation)
        XCTAssertEqual(XCTWaiter.wait(for: [dismissed], timeout: 5), .completed)
    }

    @MainActor
    private func goBack(in app: XCUIApplication) {
        let back = app.navigationBars.buttons.element(boundBy: 0)
        XCTAssertTrue(back.waitForExistence(timeout: 5))
        back.tap()
    }

    @MainActor
    private func attachScreenshot(_ app: XCUIApplication, name: String) {
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
