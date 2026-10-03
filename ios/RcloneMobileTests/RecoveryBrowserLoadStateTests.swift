import XCTest
@testable import RcloneMobile

@MainActor
final class RecoveryBrowserLoadStateTests: XCTestCase {
    func testLateDirectorySuccessCannotReplaceFinishedParentListingOrSelection() async {
        let state = RecoveryBrowserLoadState()
        let service = ControlledRecoveryBrowser()
        let old = Task { await state.load(path: "A") { try await service.fetch("old", path: $0) } }
        await service.waitUntilRequested("old")
        let parent = Task { await state.load(path: "") { try await service.fetch("parent", path: $0) } }
        await service.waitUntilRequested("parent")

        await service.finish("parent", with: .success([file("root.pdf")]))
        await parent.value
        state.toggle("root.pdf")
        await service.finish("old", with: .success([file("A/late.pdf")]))
        await old.value

        XCTAssertEqual(state.path, "")
        XCTAssertEqual(state.items.map(\.path), ["root.pdf"])
        XCTAssertEqual(state.selection, ["root.pdf"])
        XCTAssertNil(state.errorMessage)
        XCTAssertFalse(state.isLoading)
        let paths = await service.requestedPaths
        XCTAssertEqual(paths, ["old": "A", "parent": ""])
    }

    func testOldErrorCannotEndNewLoadingOrRestoreOldSelection() async {
        let state = RecoveryBrowserLoadState()
        await state.load(path: "A") { _ in [self.file("A/selected.pdf")] }
        state.toggle("A/selected.pdf")
        XCTAssertEqual(state.selection, ["A/selected.pdf"])
        let service = ControlledRecoveryBrowser()
        let old = Task { await state.load(path: "A") { try await service.fetch("old", path: $0) } }
        await service.waitUntilRequested("old")
        let parent = Task { await state.load(path: "") { try await service.fetch("parent", path: $0) } }
        await service.waitUntilRequested("parent")

        await service.finish("old", with: .failure(BrowseFailure.oldDirectory))
        await old.value

        XCTAssertEqual(state.path, "")
        XCTAssertTrue(state.isLoading)
        XCTAssertTrue(state.items.isEmpty)
        XCTAssertTrue(state.selection.isEmpty)
        XCTAssertNil(state.errorMessage)
        state.toggle("A/selected.pdf")
        XCTAssertTrue(state.selection.isEmpty)

        await service.finish("parent", with: .success([file("root.pdf")]))
        await parent.value
        XCTAssertEqual(state.items.map(\.path), ["root.pdf"])
        XCTAssertFalse(state.isLoading)
    }

    func testReturningToSamePathStillRejectsEarlierRequestForThatPath() async {
        let state = RecoveryBrowserLoadState()
        let service = ControlledRecoveryBrowser()
        let first = Task { await state.load(path: "A") { try await service.fetch("first", path: $0) } }
        await service.waitUntilRequested("first")
        let parent = Task { await state.load(path: "") { try await service.fetch("parent", path: $0) } }
        await service.waitUntilRequested("parent")
        let latest = Task { await state.load(path: "A") { try await service.fetch("latest", path: $0) } }
        await service.waitUntilRequested("latest")

        await service.finish("latest", with: .success([file("A/current.pdf")]))
        await latest.value
        await service.finish("first", with: .success([file("A/stale.pdf")]))
        await first.value
        await service.finish("parent", with: .failure(BrowseFailure.oldDirectory))
        await parent.value

        XCTAssertEqual(state.path, "A")
        XCTAssertEqual(state.items.map(\.path), ["A/current.pdf"])
        XCTAssertNil(state.errorMessage)
        XCTAssertFalse(state.isLoading)
    }

    func testCurrentFailureClearsLoadingAndDoesNotOfferPreviousFiles() async {
        let state = RecoveryBrowserLoadState()
        await state.load(path: "A") { _ in [self.file("A/selected.pdf")] }
        state.toggle("A/selected.pdf")

        await state.load(path: "") { _ in throw BrowseFailure.currentDirectory }

        XCTAssertEqual(state.path, "")
        XCTAssertTrue(state.items.isEmpty)
        XCTAssertTrue(state.selection.isEmpty)
        XCTAssertEqual(state.errorMessage, "Current directory failed")
        XCTAssertFalse(state.isLoading)
    }

    func testRestoreUsesCapturedPathsAndDoesNotClearNewParentSelection() async {
        let state = RecoveryBrowserLoadState()
        await state.load(path: "A") { _ in [self.file("A/selected.pdf")] }
        state.toggle("A/selected.pdf")
        let service = ControlledSelectiveRestore()
        let pending = Task { await state.restore { try await service.start(paths: $0) } }
        await service.waitUntilRequested()

        await state.load(path: "") { _ in [self.file("root.pdf")] }
        state.toggle("root.pdf")
        XCTAssertTrue(state.isStartingRestore)
        XCTAssertFalse(state.canStartRestore)
        let paths = await service.capturedPaths
        XCTAssertEqual(paths, ["A/selected.pdf"])
        await service.finish(with: .success(17))
        let result = await pending.value

        XCTAssertEqual(result, 17)
        XCTAssertEqual(state.path, "")
        XCTAssertEqual(state.selection, ["root.pdf"])
        XCTAssertNil(state.errorMessage)
        XCTAssertFalse(state.isStartingRestore)
        XCTAssertTrue(state.canStartRestore)
    }

    func testLateRestoreFailureCannotReplaceCurrentParentLoadError() async {
        let state = RecoveryBrowserLoadState()
        await state.load(path: "A") { _ in [self.file("A/selected.pdf")] }
        state.toggle("A/selected.pdf")
        let service = ControlledSelectiveRestore()
        let pending = Task { await state.restore { try await service.start(paths: $0) } }
        await service.waitUntilRequested()

        await state.load(path: "") { _ in throw BrowseFailure.currentDirectory }
        await service.finish(with: .failure(BrowseFailure.oldDirectory))
        let result = await pending.value

        XCTAssertNil(result)
        XCTAssertEqual(state.path, "")
        XCTAssertEqual(state.errorMessage, "Current directory failed")
        XCTAssertTrue(state.selection.isEmpty)
        XCTAssertFalse(state.isStartingRestore)
    }

    func testRestoreSuccessDoesNotClearChangedSelectionInSameDirectory() async {
        let state = RecoveryBrowserLoadState()
        await state.load(path: "A") { _ in [self.file("A/first.pdf"), self.file("A/next.pdf")] }
        state.toggle("A/first.pdf")
        let service = ControlledSelectiveRestore()
        let pending = Task { await state.restore { try await service.start(paths: $0) } }
        await service.waitUntilRequested()

        state.toggle("A/first.pdf")
        state.toggle("A/next.pdf")
        await service.finish(with: .success(17))
        _ = await pending.value

        XCTAssertEqual(state.path, "A")
        XCTAssertEqual(state.selection, ["A/next.pdf"])
        XCTAssertNil(state.errorMessage)
        let paths = await service.capturedPaths
        XCTAssertEqual(paths, ["A/first.pdf"])
    }

    func testRestoreFailureIsIgnoredAfterReselectingIdenticalPaths() async {
        let state = RecoveryBrowserLoadState()
        await state.load(path: "A") { _ in [self.file("A/selected.pdf")] }
        state.toggle("A/selected.pdf")
        let service = ControlledSelectiveRestore()
        let pending = Task { await state.restore { try await service.start(paths: $0) } }
        await service.waitUntilRequested()

        state.toggle("A/selected.pdf")
        state.toggle("A/selected.pdf")
        await service.finish(with: .failure(BrowseFailure.oldDirectory))
        _ = await pending.value

        XCTAssertEqual(state.path, "A")
        XCTAssertEqual(state.selection, ["A/selected.pdf"])
        XCTAssertNil(state.errorMessage)
        XCTAssertFalse(state.isStartingRestore)
    }

    func testPendingGateRejectsDuplicateStartAndCurrentCompletionStillApplies() async {
        let state = RecoveryBrowserLoadState()
        await state.load(path: "A") { _ in [self.file("A/selected.pdf")] }
        state.toggle("A/selected.pdf")
        let service = ControlledSelectiveRestore()
        let pending = Task { await state.restore { try await service.start(paths: $0) } }
        await service.waitUntilRequested()

        let duplicate = await state.restore { _ -> Int in
            XCTFail("A second pending restore must not be sent")
            return -1
        }
        XCTAssertNil(duplicate)
        await service.finish(with: .success(17))
        _ = await pending.value

        XCTAssertTrue(state.selection.isEmpty)
        XCTAssertFalse(state.isStartingRestore)
        let count = await service.callCount
        XCTAssertEqual(count, 1)
        state.toggle("A/selected.pdf")
        let failure: Int? = await state.restore { _ in throw BrowseFailure.currentDirectory }
        XCTAssertNil(failure)
        XCTAssertEqual(state.errorMessage, "Current directory failed")
        XCTAssertEqual(state.selection, ["A/selected.pdf"])
        XCTAssertFalse(state.isStartingRestore)
    }

    private func file(_ path: String) -> RecoveryBrowseItem {
        RecoveryBrowseItem(name: path, path: path, isDirectory: false, size: 1, modifiedAt: nil)
    }
}

private enum BrowseFailure: LocalizedError {
    case oldDirectory, currentDirectory
    var errorDescription: String? {
        switch self {
        case .oldDirectory: "Old directory failed"
        case .currentDirectory: "Current directory failed"
        }
    }
}

private actor ControlledRecoveryBrowser {
    private var pending: [String: CheckedContinuation<[RecoveryBrowseItem], Error>] = [:]
    private var waiters: [String: CheckedContinuation<Void, Never>] = [:]
    private(set) var requestedPaths: [String: String] = [:]

    func fetch(_ request: String, path: String) async throws -> [RecoveryBrowseItem] {
        try await withCheckedThrowingContinuation { continuation in
            requestedPaths[request] = path
            pending[request] = continuation
            waiters.removeValue(forKey: request)?.resume()
        }
    }

    func waitUntilRequested(_ request: String) async {
        if pending[request] != nil { return }
        await withCheckedContinuation { waiters[request] = $0 }
    }

    func finish(_ request: String, with result: Result<[RecoveryBrowseItem], Error>) {
        guard let continuation = pending.removeValue(forKey: request) else {
            preconditionFailure("No pending request: \(request)")
        }
        continuation.resume(with: result)
    }
}

private actor ControlledSelectiveRestore {
    private var pending: CheckedContinuation<Int, Error>?
    private var waiter: CheckedContinuation<Void, Never>?
    private(set) var capturedPaths: [String] = []
    private(set) var callCount = 0

    func start(paths: [String]) async throws -> Int {
        try await withCheckedThrowingContinuation { continuation in
            capturedPaths = paths
            callCount += 1
            pending = continuation
            waiter?.resume()
            waiter = nil
        }
    }

    func waitUntilRequested() async {
        if pending != nil { return }
        await withCheckedContinuation { waiter = $0 }
    }

    func finish(with result: Result<Int, Error>) {
        guard let continuation = pending else { preconditionFailure("No pending restore") }
        pending = nil
        continuation.resume(with: result)
    }
}
