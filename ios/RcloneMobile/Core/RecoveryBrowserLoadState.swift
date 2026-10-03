import Combine
import Foundation

/// Only the latest directory request may publish its files, error or loading state.
@MainActor
final class RecoveryBrowserLoadState: ObservableObject {
    @Published private(set) var path = ""
    @Published private(set) var items: [RecoveryBrowseItem] = []
    @Published private(set) var selection: Set<String> = []
    @Published private(set) var isLoading = false
    @Published private(set) var isStartingRestore = false
    @Published var errorMessage: String?
    private var requestID: UUID?
    private var contextID = UUID()
    private var selectionRevision = UUID()

    var canStartRestore: Bool { !isLoading && !isStartingRestore && !selection.isEmpty }

    private struct RestoreSnapshot {
        let contextID: UUID
        let selectionRevision: UUID
        let paths: [String]
    }

    func load(path: String, fetch: (String) async throws -> [RecoveryBrowseItem]) async {
        let requestID = UUID()
        self.requestID = requestID
        contextID = requestID
        self.path = path
        items = []
        clearSelection()
        errorMessage = nil
        isLoading = true
        defer {
            if self.requestID == requestID && self.path == path {
                isLoading = false
                self.requestID = nil
            }
        }
        do {
            let result = try await fetch(path)
            guard self.requestID == requestID && self.path == path else { return }
            items = result
        } catch {
            guard self.requestID == requestID && self.path == path else { return }
            errorMessage = error.localizedDescription
        }
    }

    func toggle(_ path: String) {
        guard !isLoading, items.contains(where: { $0.path == path && !$0.isDirectory }) else { return }
        if selection.contains(path) {
            selection.remove(path)
        } else {
            guard selection.count < 100 else { return }
            selection.insert(path)
        }
        selectionRevision = UUID()
    }

    /// Navigation remains available while the captured request is being started.
    func restore<Value>(start: ([String]) async throws -> Value) async -> Value? {
        guard canStartRestore else { return nil }
        let snapshot = RestoreSnapshot(contextID: contextID, selectionRevision: selectionRevision,
                                       paths: selection.sorted())
        isStartingRestore = true
        defer { isStartingRestore = false }
        do {
            let result = try await start(snapshot.paths)
            if isCurrent(snapshot) { clearSelection() }
            return result
        } catch {
            if isCurrent(snapshot) { errorMessage = error.localizedDescription }
            return nil
        }
    }

    private func isCurrent(_ snapshot: RestoreSnapshot) -> Bool {
        contextID == snapshot.contextID && selectionRevision == snapshot.selectionRevision
            && selection.sorted() == snapshot.paths
    }

    private func clearSelection() {
        selection.removeAll()
        selectionRevision = UUID()
    }
}
