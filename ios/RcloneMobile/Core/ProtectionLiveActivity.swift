import Foundation
#if canImport(ActivityKit)
import ActivityKit

@MainActor
final class ProtectionLiveActivityCoordinator {
    private var activity: Activity<ProtectionActivityAttributes>?

    func accept(_ progress: BackupProgress, hostname: String) {
        guard ActivityAuthorizationInfo().areActivitiesEnabled else { return }
        let pair = progress.pairs?.first(where: { !["done", "ok"].contains($0.status.lowercased()) })
            ?? progress.pairs?.last
        let state = ProtectionActivityAttributes.ContentState(
            pair: pair?.name ?? "Sicherung",
            status: pair?.status ?? (progress.running ? "running" : "done"),
            percent: pair?.percent,
            transferred: pair?.transferred,
            error: pair?.error
        )
        if progress.running {
            if let activity {
                Task { await activity.update(ActivityContent(state: state, staleDate: Date().addingTimeInterval(60))) }
            } else if let jobID = progress.jobID {
                do {
                    activity = try Activity.request(
                        attributes: ProtectionActivityAttributes(hostname: hostname, jobID: jobID),
                        content: ActivityContent(state: state, staleDate: Date().addingTimeInterval(60)),
                        pushType: nil
                    )
                } catch {
                    // Live Activities are an optional surface; the run itself is unaffected.
                }
            }
        } else {
            end(state)
        }
    }

    func endAll() {
        let state = ProtectionActivityAttributes.ContentState(
            pair: "Sicherung", status: "ended", percent: nil, transferred: nil, error: nil
        )
        end(state)
    }

    private func end(_ state: ProtectionActivityAttributes.ContentState) {
        guard let activity else { return }
        self.activity = nil
        Task {
            await activity.end(
                ActivityContent(state: state, staleDate: nil),
                dismissalPolicy: .default
            )
        }
    }
}
#else
@MainActor
final class ProtectionLiveActivityCoordinator {
    func accept(_ progress: BackupProgress, hostname: String) {}
    func endAll() {}
}
#endif
