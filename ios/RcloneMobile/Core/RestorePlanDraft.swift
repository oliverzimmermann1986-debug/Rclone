import Foundation

/// Keeps a revision-bound draft separate from newly fetched server settings.
struct RestorePlanDraft {
    private(set) var context = ""
    private(set) var plan: RestorePlanResponse?
    private(set) var latestServerPlan: RestorePlanResponse?
    private(set) var hasConflict = false
    private(set) var previewSettings: RestorePlanSettings?
    private(set) var previewRevision: String?
    var settings = RestorePlanSettings(enabled: false, schedule: "manual", sampleFiles: 20,
                                       maxTotalMB: 256, maxScanFiles: 20_000)

    var isDirty: Bool { plan.map { settings != $0.settings } ?? false }
    var needsResolution: Bool { hasConflict || latestServerPlan != nil }
    var canSave: Bool {
        isDirty && !needsResolution && previewSettings == settings && previewRevision == plan?.revision
    }

    mutating func reset(context: String) {
        self = RestorePlanDraft()
        self.context = context
    }

    /// SwiftUI starts a view task again after navigation, even for the same ID.
    /// Only a real session/server change may discard the existing editor state.
    @discardableResult
    mutating func enterContext(_ context: String) -> Bool {
        guard self.context != context else { return false }
        reset(context: context)
        return true
    }

    @discardableResult
    mutating func receive(_ result: RestorePlanResponse, context: String) -> Bool {
        guard self.context == context else { return false }
        if isDirty || needsResolution {
            latestServerPlan = result
            hasConflict = true
            invalidatePreview()
        } else {
            accept(result)
        }
        return true
    }

    mutating func invalidatePreview() {
        previewSettings = nil
        previewRevision = nil
    }

    mutating func markConflict(context: String) {
        guard self.context == context else { return }
        hasConflict = true
        latestServerPlan = nil
        invalidatePreview()
    }

    @discardableResult
    mutating func acceptPreview(_ result: RestorePlanResponse, settings: RestorePlanSettings,
                                revision: String, context: String) -> Bool {
        guard self.context == context, !needsResolution, self.settings == settings,
              plan?.revision == revision, result.revision == revision else { return false }
        previewSettings = settings
        previewRevision = revision
        return true
    }

    /// Explicitly transfer only fields edited by the user onto the fetched revision.
    @discardableResult
    mutating func rebaseChanges(context: String) -> Bool {
        guard self.context == context, let baseline = plan?.settings, let latest = latestServerPlan else { return false }
        var merged = latest.settings
        if settings.enabled != baseline.enabled { merged.enabled = settings.enabled }
        if settings.schedule != baseline.schedule { merged.schedule = settings.schedule }
        if settings.sampleFiles != baseline.sampleFiles { merged.sampleFiles = settings.sampleFiles }
        if settings.maxTotalMB != baseline.maxTotalMB { merged.maxTotalMB = settings.maxTotalMB }
        if settings.maxScanFiles != baseline.maxScanFiles { merged.maxScanFiles = settings.maxScanFiles }
        accept(latest)
        settings = merged
        invalidatePreview()
        return true
    }

    @discardableResult
    mutating func discardChanges(context: String) -> Bool {
        guard self.context == context, let latest = latestServerPlan else { return false }
        accept(latest)
        return true
    }

    func saveRequest(context: String) -> RestorePlanUpdate? {
        guard self.context == context, canSave, let plan else { return nil }
        return RestorePlanUpdate(revision: plan.revision, settings: settings)
    }

    @discardableResult
    mutating func acceptSaved(_ result: RestorePlanResponse, context: String) -> Bool {
        guard self.context == context else { return false }
        accept(result)
        return true
    }

    private mutating func accept(_ result: RestorePlanResponse) {
        plan = result
        settings = result.settings
        latestServerPlan = nil
        hasConflict = false
        previewSettings = result.settings
        previewRevision = result.revision
    }
}
