import XCTest
@testable import RcloneMobile

final class RestorePlanDraftTests: XCTestCase {
    func testSameContextReappearancePreservesDirtyConflictAndFetchedServerPlan() {
        var draft = loadedDraft()
        draft.settings.sampleFiles = 42
        draft.markConflict(context: "server-a")
        draft.receive(plan("revision-2"), context: "server-a")

        XCTAssertFalse(draft.enterContext("server-a"))

        XCTAssertEqual(draft.settings.sampleFiles, 42)
        XCTAssertEqual(draft.plan?.revision, "revision-1")
        XCTAssertEqual(draft.latestServerPlan?.revision, "revision-2")
        XCTAssertTrue(draft.needsResolution)
        XCTAssertNil(draft.saveRequest(context: "server-a"))
    }

    func testSameContextReappearancePreservesThePreviewAndRequestBeingSaved() throws {
        var draft = loadedDraft()
        draft.settings.sampleFiles = 42
        let edited = draft.settings
        XCTAssertTrue(draft.acceptPreview(plan("revision-1", settings: edited), settings: edited,
                                          revision: "revision-1", context: "server-a"))
        let request = try XCTUnwrap(draft.saveRequest(context: "server-a"))

        XCTAssertFalse(draft.enterContext("server-a"))

        let preserved = try XCTUnwrap(draft.saveRequest(context: "server-a"))
        XCTAssertEqual(preserved.revision, request.revision)
        XCTAssertEqual(preserved.settings, request.settings)
    }

    func testReturningToOldBaselineDoesNotSilentlyResolveConflictOnReload() {
        var draft = loadedDraft()
        draft.settings.sampleFiles = 42
        draft.markConflict(context: "server-a")
        draft.settings = settings()
        XCTAssertFalse(draft.isDirty)
        var server = settings()
        server.sampleFiles = 30

        draft.receive(plan("revision-2", settings: server), context: "server-a")

        XCTAssertEqual(draft.settings, settings())
        XCTAssertEqual(draft.plan?.revision, "revision-1")
        XCTAssertEqual(draft.latestServerPlan?.settings, server)
        XCTAssertTrue(draft.needsResolution)
        XCTAssertNil(draft.saveRequest(context: "server-a"))
        XCTAssertTrue(draft.discardChanges(context: "server-a"))
        XCTAssertEqual(draft.settings, server)
        XCTAssertFalse(draft.needsResolution)
    }

    func testDirtyReloadRetainsDraftAndRevisionUntilExplicitResolution() {
        var draft = loadedDraft()
        draft.settings.sampleFiles = 42
        var server = settings()
        server.maxTotalMB = 512

        XCTAssertTrue(draft.receive(plan("revision-2", settings: server), context: "server-a"))

        XCTAssertEqual(draft.settings.sampleFiles, 42)
        XCTAssertEqual(draft.settings.maxTotalMB, 256)
        XCTAssertEqual(draft.plan?.revision, "revision-1")
        XCTAssertEqual(draft.latestServerPlan?.revision, "revision-2")
        XCTAssertTrue(draft.needsResolution)
        XCTAssertNil(draft.saveRequest(context: "server-a"))
    }

    func testConflictInvalidatesPreviewAndRejectsLatePreviewAndRepeatSave() {
        var draft = loadedDraft()
        draft.settings.sampleFiles = 42
        let edited = draft.settings
        XCTAssertTrue(draft.acceptPreview(plan("revision-1", settings: edited), settings: edited,
                                          revision: "revision-1", context: "server-a"))
        XCTAssertNotNil(draft.saveRequest(context: "server-a"))

        draft.markConflict(context: "server-a")

        XCTAssertEqual(draft.settings, edited)
        XCTAssertNil(draft.previewSettings)
        XCTAssertNil(draft.saveRequest(context: "server-a"))
        XCTAssertFalse(draft.acceptPreview(plan("revision-1", settings: edited), settings: edited,
                                           revision: "revision-1", context: "server-a"))
        XCTAssertNil(draft.saveRequest(context: "server-a"))
    }

    func testExplicitRebaseKeepsUserEditsAndNewUntouchedServerFieldsButRequiresPreview() throws {
        var draft = loadedDraft()
        draft.settings.sampleFiles = 42
        var server = settings()
        server.sampleFiles = 30
        server.maxTotalMB = 512
        server.maxScanFiles = 37_777
        server.schedule = "15 6 * * *"
        draft.markConflict(context: "server-a")
        draft.receive(plan("revision-2", settings: server), context: "server-a")

        XCTAssertTrue(draft.rebaseChanges(context: "server-a"))

        XCTAssertEqual(draft.settings.sampleFiles, 42)
        XCTAssertEqual(draft.settings.maxTotalMB, 512)
        XCTAssertEqual(draft.settings.maxScanFiles, 37_777)
        XCTAssertEqual(draft.settings.schedule, "15 6 * * *")
        XCTAssertFalse(draft.needsResolution)
        XCTAssertNil(draft.saveRequest(context: "server-a"))
        let rebased = draft.settings
        XCTAssertTrue(draft.acceptPreview(plan("revision-2", settings: rebased), settings: rebased,
                                          revision: "revision-2", context: "server-a"))
        let request = try XCTUnwrap(draft.saveRequest(context: "server-a"))
        XCTAssertEqual(request.revision, "revision-2")
        XCTAssertEqual(request.settings, rebased)
    }

    func testExplicitDiscardUsesFetchedServerValuesWithoutSaving() {
        var draft = loadedDraft()
        draft.settings.sampleFiles = 42
        var server = settings()
        server.maxTotalMB = 512
        draft.receive(plan("revision-2", settings: server), context: "server-a")
        XCTAssertEqual(draft.settings.sampleFiles, 42)

        XCTAssertTrue(draft.discardChanges(context: "server-a"))

        XCTAssertEqual(draft.settings, server)
        XCTAssertEqual(draft.plan?.revision, "revision-2")
        XCTAssertFalse(draft.isDirty)
        XCTAssertFalse(draft.needsResolution)
        XCTAssertNil(draft.saveRequest(context: "server-a"))
    }

    func testSessionBoundaryRejectsOldResponsesConfirmationsAndConflicts() {
        var draft = loadedDraft()
        draft.settings.sampleFiles = 42
        let previous = draft.settings
        XCTAssertTrue(draft.enterContext("server-b"))
        draft.receive(plan("b-revision"), context: "server-b")

        XCTAssertFalse(draft.receive(plan("old-a"), context: "server-a"))
        XCTAssertFalse(draft.acceptSaved(plan("old-a"), context: "server-a"))
        XCTAssertFalse(draft.acceptPreview(plan("revision-1", settings: previous), settings: previous,
                                           revision: "revision-1", context: "server-a"))
        draft.markConflict(context: "server-a")
        XCTAssertFalse(draft.needsResolution)
        XCTAssertEqual(draft.plan?.revision, "b-revision")
        XCTAssertEqual(draft.settings, settings())
        XCTAssertNil(draft.saveRequest(context: "server-a"))
        XCTAssertFalse(draft.rebaseChanges(context: "server-a"))
        XCTAssertFalse(draft.discardChanges(context: "server-a"))
    }

    func testChangedDraftOrRevisionCannotUseAnEarlierPreview() {
        var draft = loadedDraft()
        draft.settings.sampleFiles = 42
        let edited = draft.settings
        XCTAssertFalse(draft.acceptPreview(plan("revision-2", settings: edited), settings: edited,
                                           revision: "revision-1", context: "server-a"))
        XCTAssertTrue(draft.acceptPreview(plan("revision-1", settings: edited), settings: edited,
                                          revision: "revision-1", context: "server-a"))
        draft.settings.sampleFiles = 43
        XCTAssertNil(draft.saveRequest(context: "server-a"))
        XCTAssertFalse(draft.acceptPreview(plan("revision-1", settings: edited), settings: edited,
                                           revision: "revision-1", context: "server-a"))
    }

    func testCleanRefreshAndSuccessfulSavePublishCanonicalBaseline() {
        var draft = loadedDraft()
        var server = settings()
        server.maxTotalMB = 512
        draft.receive(plan("revision-2", settings: server), context: "server-a")
        XCTAssertEqual(draft.settings, server)
        XCTAssertFalse(draft.needsResolution)
        draft.settings.sampleFiles = 42
        server.sampleFiles = 42

        XCTAssertTrue(draft.acceptSaved(plan("revision-3", settings: server), context: "server-a"))

        XCTAssertEqual(draft.plan?.revision, "revision-3")
        XCTAssertFalse(draft.isDirty)
        XCTAssertNil(draft.saveRequest(context: "server-a"))
    }

    private func loadedDraft() -> RestorePlanDraft {
        var draft = RestorePlanDraft()
        draft.reset(context: "server-a")
        draft.receive(plan("revision-1"), context: "server-a")
        return draft
    }

    private func settings() -> RestorePlanSettings {
        RestorePlanSettings(enabled: true, schedule: "0 5 * * 0,3", sampleFiles: 20,
                            maxTotalMB: 256, maxScanFiles: 20_000)
    }

    private func plan(_ revision: String, settings: RestorePlanSettings? = nil) -> RestorePlanResponse {
        RestorePlanResponse(revision: revision, settings: settings ?? self.settings(), timezone: "Europe/Berlin",
                            generatedAt: 1_800_000_000, nextRuns: [], dueNow: false, dataPaths: [], warnings: [])
    }
}
