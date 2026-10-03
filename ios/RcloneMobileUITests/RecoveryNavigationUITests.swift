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
        app.buttons["Abbrechen"].tap()
        XCTAssertTrue(app.buttons["recoveryFile-Beispiel.pdf"].exists)
        goBack(in: app)
        goBack(in: app)

        tap(app.buttons["verifyRestoreLink"], in: app)
        tap(app.buttons["recoveryPath-Fotos"], in: app)
        XCTAssertTrue(app.staticTexts["RTO-Stichprobe"].waitForExistence(timeout: 5))
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
        tap(assessment, in: app)
        XCTAssertTrue(app.navigationBars["Schutznachweis"].waitForExistence(timeout: 5))
        app.buttons["Fertig"].tap()
        XCTAssertTrue(app.navigationBars["Lage"].waitForExistence(timeout: 5))

        tap(app.tabBars.buttons["Wiederherstellen"], in: app)
        XCTAssertTrue(app.buttons["recoverFilesLink"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["verifyRestoreLink"].exists)
        XCTAssertTrue(app.buttons["serverLossLink"].exists)
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
        XCTAssertTrue(element.waitForExistence(timeout: 10), file: file, line: line)
        for _ in 0..<12 {
            if element.isHittable { break }
            app.swipeUp()
        }
        XCTAssertTrue(element.isHittable, file: file, line: line)
        element.tap()
    }

    @MainActor
    private func reveal(_ element: XCUIElement, in app: XCUIApplication) {
        for _ in 0..<12 {
            if element.exists && !element.frame.isEmpty && element.frame.intersects(app.frame) { break }
            app.swipeUp()
        }
        XCTAssertTrue(element.exists)
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
