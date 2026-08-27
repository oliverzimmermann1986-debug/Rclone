import Foundation
import Combine

private enum RefreshPayload {
    case overview(Result<OverviewResponse, Error>)
    case baseStorage(Result<StorageOverview, Error>)
    case detailedStorage(Result<StorageOverview, Error>)
    case config(Result<ConfigSnapshot, Error>)
    case jobs(Result<JobSearchResponse, Error>)
    case progress(Result<BackupProgress, Error>)
    case pbs(Result<PBSStatus, Error>)
}

enum ContentLoadState: Equatable {
    case idle
    case loading
    case loaded
    case failed(String)
}

enum StorageSizeStatus: Equatable {
    case idle
    case loading
    case loaded
    case partial
    case failed
    case stale
}

struct StorageSizeState: Equatable {
    let status: StorageSizeStatus
    let message: String?
    let lastUpdated: Date?

    static let idle = StorageSizeState(status: .idle, message: nil, lastUpdated: nil)
}

enum ConfigSaveIssue: Equatable {
    case conflict(String)
    case passwordRequired(String)
    case validation([String])
}

private struct PendingPushRevocation: Codable, Equatable {
    let server: String
    let token: String
}

@MainActor
final class AppModel: ObservableObject {
    enum Phase: Equatable {
        case checking
        case signedOut
        case signedIn
    }

    @Published private(set) var phase: Phase = .checking
    @Published private(set) var overview: OverviewResponse?
    @Published private(set) var storage: StorageOverview?
    @Published private(set) var config: ConfigSnapshot?
    @Published private(set) var jobDefinitions: [JobDefinition] = []
    @Published private(set) var jobs: [JobRecord] = []
    @Published private(set) var doctor: DoctorResponse?
    @Published private(set) var progress: BackupProgress?
    @Published private(set) var pbs: PBSStatus?
    @Published private(set) var overviewState: ContentLoadState = .idle
    @Published private(set) var storageState: ContentLoadState = .idle
    @Published private(set) var configState: ContentLoadState = .idle
    @Published private(set) var jobsState: ContentLoadState = .idle
    @Published private(set) var pbsState: ContentLoadState = .idle
    @Published private(set) var progressLastSuccessAt: Date?
    @Published private(set) var progressConsecutiveFailures = 0
    @Published private(set) var doctorLastCheckedAt: Date?
    @Published private(set) var doctorIsRefreshing = false
    @Published private(set) var storageSizesAreLoading = false
    @Published private(set) var storageSizeState: StorageSizeState = .idle
    @Published private(set) var isSavingConfig = false
    @Published private(set) var configSaveIssue: ConfigSaveIssue?
    @Published private(set) var configWarnings: [String] = []
    @Published private(set) var isRefreshing = false
    @Published var errorMessage: String?
    @Published var actionMessage: String?
    @Published private(set) var requestedRunID: Int?
    @Published private(set) var batchDefinitions: [BatchDefinitionState] = []
    @Published private(set) var batchIsRunning = false
    @Published private(set) var isDemoMode = false

    private(set) var client: (any APIClientProtocol)?
    private var registeredPushToken: String?
    private let pendingPushRevocationsKey = "pendingPushRevocations"
    private let defaults: UserDefaults
    private let clientFactory: (URL) -> any APIClientProtocol
    private let runPollInterval: Duration
    private var sessionGeneration = 0
    private var refreshGeneration = 0
    private var activities: Set<UUID> = []
    private var refreshTask: Task<Void, Never>?
    private var refreshOwner: UUID?
    private var runTrackingTask: Task<Void, Never>?
    private var pushSyncTask: Task<Void, Never>?
    private var pushSyncOwner: UUID?
    private var desiredPushToken: String?
    private var desiredPushEnvironment: String?
    private var knownPushTokens: Set<String> = []

    var serverAddress: String {
        if isDemoMode { return "Sichere Demo · keine Serververbindung" }
        return defaults.string(forKey: "serverAddress") ?? ""
    }

    var savedUsername: String {
        if isDemoMode { return "Vorschau" }
        return defaults.string(forKey: "username") ?? "admin"
    }

    var progressIsStale: Bool {
        guard progressConsecutiveFailures > 0 else { return false }
        if progressConsecutiveFailures >= 3 { return true }
        guard progress?.running == true, let progressLastSuccessAt else { return false }
        return Date().timeIntervalSince(progressLastSuccessAt) >= 15
    }

    init(
        defaults: UserDefaults = .standard,
        runPollInterval: Duration = .seconds(2),
        clientFactory: @escaping (URL) -> any APIClientProtocol = { APIClient(baseURL: $0) }
    ) {
        self.defaults = defaults
        self.runPollInterval = runPollInterval
        self.clientFactory = clientFactory
    }

    func restoreSession() async {
        if StorePreviewMode.isLaunchEnabled {
            enterDemoMode()
            return
        }
        let generation = beginSessionTransition()
        errorMessage = nil
        guard !serverAddress.isEmpty else {
            phase = .signedOut
            return
        }

        let activity = beginActivity()
        defer { endActivity(activity) }
        var candidate: (any APIClientProtocol)?
        do {
            let url = try APIClient.normalizedServerURL(serverAddress)
            let newClient = clientFactory(url)
            candidate = newClient
            if APIClient.requiresExplicitInsecureTransportConfirmation(url) {
                newClient.clearLocalSession()
                clearSessionState()
                errorMessage = "Eine gespeicherte HTTP-Verbindung wird aus Sicherheitsgründen nicht automatisch wiederhergestellt. Tippe erneut auf Verbinden und bestätige die unverschlüsselte Verbindung ausdrücklich."
                return
            }
            try await revokePendingPushRegistrationsBeforeRestore(
                using: newClient,
                server: url.absoluteString
            )
            try Task.checkCancellation()
            guard isCurrentSession(generation) else {
                newClient.clearLocalSession()
                return
            }
            let restoredConfig = try await newClient.getConfig()
            try Task.checkCancellation()
            guard isCurrentSession(generation) else { return }
            client = newClient
            config = restoredConfig
            jobDefinitions = restoredConfig.backup.jobs
            configState = .loaded
            phase = .signedIn
            await refresh()
        } catch is CancellationError {
            // A superseded restore must not overwrite the newer session state.
        } catch let urlError as URLError where urlError.code == .cancelled {
            // URLSession translates cooperative task cancellation to this error.
        } catch {
            guard isCurrentSession(generation) else { return }
            if error as? APIError == .unauthenticated {
                candidate?.clearLocalSession()
            }
            clearSessionState()
            errorMessage = userMessage(for: error)
        }
    }

    func login(server: String, username: String, password: String) async {
        let generation = beginSessionTransition()
        let activity = beginActivity()
        errorMessage = nil
        defer { endActivity(activity) }

        var candidate: (any APIClientProtocol)?
        do {
            let url = try APIClient.normalizedServerURL(server)
            let newClient = clientFactory(url)
            candidate = newClient
            try await newClient.login(username: username, password: password)
            try Task.checkCancellation()
            guard isCurrentSession(generation) else {
                newClient.clearLocalSession()
                return
            }
            defaults.set(url.absoluteString, forKey: "serverAddress")
            defaults.set(username, forKey: "username")
            client = newClient
            await retryPendingPushRevocations(using: newClient, server: url.absoluteString)
            phase = .signedIn
            await refresh()
        } catch is CancellationError {
            candidate?.clearLocalSession()
        } catch let urlError as URLError where urlError.code == .cancelled {
            candidate?.clearLocalSession()
        } catch {
            candidate?.clearLocalSession()
            guard isCurrentSession(generation) else { return }
            errorMessage = userMessage(for: error)
        }
    }

    func refresh() async {
        guard let refreshClient = client else { return }
        refreshTask?.cancel()
        let session = sessionGeneration
        refreshGeneration += 1
        let refresh = refreshGeneration
        let owner = UUID()
        refreshOwner = owner
        if overview == nil { overviewState = .loading }
        if storage == nil { storageState = .loading }
        if config == nil { configState = .loading }
        if jobs.isEmpty && jobsState != .loaded { jobsState = .loading }
        if pbs == nil { pbsState = .loading }
        let task = Task { [weak self] in
            guard let self else { return }
            await self.performRefresh(client: refreshClient, session: session, refresh: refresh)
        }
        refreshTask = task
        await task.value
        if refreshOwner == owner {
            refreshTask = nil
            refreshOwner = nil
        }
    }

    private func performRefresh(client refreshClient: any APIClientProtocol, session: Int, refresh: Int) async {
        let activity = beginActivity()
        errorMessage = nil
        storageSizesAreLoading = true
        storageSizeState = StorageSizeState(
            status: .loading,
            message: storageSizeState.message,
            lastUpdated: storageSizeState.lastUpdated
        )
        defer {
            endActivity(activity)
            if isCurrent(session: session, refresh: refresh) {
                storageSizesAreLoading = false
            }
        }

        var firstError: Error?
        await withTaskGroup(of: RefreshPayload.self) { group in
            group.addTask { .overview(await Self.capture { try await refreshClient.getOverview() }) }
            group.addTask { .baseStorage(await Self.capture { try await refreshClient.getStorage(includeSizes: false, forceRefresh: false) }) }
            group.addTask { .detailedStorage(await Self.capture { try await refreshClient.getStorage(includeSizes: true, forceRefresh: false) }) }
            group.addTask { .config(await Self.capture { try await refreshClient.getConfig() }) }
            group.addTask { .jobs(await Self.capture { try await refreshClient.getJobs(limit: 50) }) }
            group.addTask { .progress(await Self.capture { try await refreshClient.getProgress() }) }
            group.addTask { .pbs(await Self.capture { try await refreshClient.getPBSStatus() }) }

            for await payload in group {
                guard isCurrent(session: session, refresh: refresh) else { continue }
                switch payload {
                case let .overview(result):
                    switch result {
                    case let .success(value):
                        overview = value
                        overviewState = .loaded
                    case let .failure(error):
                        overviewState = .failed(userMessage(for: error))
                        handle(error, firstError: &firstError)
                    }
                case let .baseStorage(result):
                    switch result {
                    case let .success(base):
                        storage = base.preservingSizes(from: storage)
                        storageState = .loaded
                    case let .failure(error):
                        storageState = .failed(userMessage(for: error))
                        handle(error, firstError: &firstError)
                    }
                case let .detailedStorage(result):
                    storageSizesAreLoading = false
                    // Remote size calculation is optional and may be slow. Its
                    // failure must never discard usable base storage or old sizes.
                    if case let .success(detailed) = result {
                        storage = detailed.preservingSizes(from: storage)
                        acceptStorageMeasurement(detailed)
                    } else if case let .failure(error) = result,
                              error as? APIError == .unauthenticated {
                        handle(error, firstError: &firstError)
                    } else if case let .failure(error) = result {
                        markStorageMeasurementFailure(error)
                    }
                case let .config(result):
                    switch result {
                    case let .success(value):
                        config = value
                        jobDefinitions = value.backup.jobs
                        configState = .loaded
                    case let .failure(error):
                        configState = .failed(userMessage(for: error))
                        handle(error, firstError: &firstError)
                    }
                case let .jobs(result):
                    switch result {
                    case let .success(response):
                        jobs = response.items
                        jobsState = .loaded
                    case let .failure(error):
                        jobsState = .failed(userMessage(for: error))
                        handle(error, firstError: &firstError)
                    }
                case let .progress(result):
                    switch result {
                    case let .success(value):
                        acceptProgress(value)
                    case let .failure(error):
                        recordProgressFailure()
                        handle(error, firstError: &firstError)
                    }
                case let .pbs(result):
                    switch result {
                    case let .success(value):
                        pbs = value
                        pbsState = .loaded
                    case let .failure(error):
                        pbsState = .failed(userMessage(for: error))
                        handle(error, firstError: &firstError)
                    }
                }

                if phase == .signedOut {
                    group.cancelAll()
                }
            }
        }

        guard isCurrent(session: session, refresh: refresh) else { return }
        if let firstError {
            errorMessage = userMessage(for: firstError)
        }
    }

    func refreshProgress() async {
        guard let currentClient = client else { return }
        let session = sessionGeneration
        do {
            let newProgress = try await currentClient.getProgress()
            guard isCurrentSession(session) else { return }
            let completedRunningJob = progress?.running == true && newProgress.running == false
            acceptProgress(newProgress)
            if completedRunningJob {
                let response = try await currentClient.getJobs(limit: 50)
                guard isCurrentSession(session) else { return }
                jobs = response.items
                jobsState = .loaded
            }
        } catch APIError.unauthenticated {
            guard isCurrentSession(session) else { return }
            signOutLocally()
        } catch {
            // Der Poll darf eine anderweitig nutzbare Ansicht nicht mit Meldungen fluten.
            guard isCurrentSession(session) else { return }
            recordProgressFailure()
        }
    }

    func refreshStorageSizes() async {
        guard let currentClient = client else { return }
        let session = sessionGeneration
        let activity = beginActivity()
        storageSizesAreLoading = true
        storageSizeState = StorageSizeState(
            status: .loading,
            message: storageSizeState.message,
            lastUpdated: storageSizeState.lastUpdated
        )
        defer {
            endActivity(activity)
            if isCurrentSession(session) { storageSizesAreLoading = false }
        }
        do {
            let detailed = try await currentClient.getStorage(
                includeSizes: true,
                forceRefresh: true
            )
            guard isCurrentSession(session) else { return }
            storage = detailed.preservingSizes(from: storage)
            storageState = .loaded
            acceptStorageMeasurement(detailed)
        } catch APIError.unauthenticated {
            guard isCurrentSession(session) else { return }
            signOutLocally()
        } catch {
            guard isCurrentSession(session) else { return }
            markStorageMeasurementFailure(error)
            errorMessage = userMessage(for: error)
        }
    }

    func refreshDoctor() async {
        doctorIsRefreshing = true
        defer {
            doctorIsRefreshing = false
        }
        do {
            let newDoctor = try await withCurrentClient { try await $0.getDoctor() }
            doctor = newDoctor
            doctorLastCheckedAt = Date()
        } catch is CancellationError {
        } catch {
            errorMessage = userMessage(for: error)
        }
    }

    func enterDemoMode() {
        beginSessionTransition()
        clearSessionState()
        do {
            let fixture = try StorePreviewData.load()
            overview = fixture.overview
            storage = fixture.storage
            config = fixture.config
            jobDefinitions = fixture.config.backup.jobs
            jobs = fixture.jobs
            doctor = fixture.doctor
            progress = fixture.progress
            pbs = fixture.pbs
            overviewState = .loaded
            storageState = .loaded
            configState = .loaded
            jobsState = .loaded
            pbsState = .loaded
            storageSizeState = StorageSizeState(
                status: .loaded,
                message: nil,
                lastUpdated: Date(timeIntervalSince1970: fixture.overview.generatedAt)
            )
            progressLastSuccessAt = Date(timeIntervalSince1970: fixture.overview.generatedAt)
            doctorLastCheckedAt = Date(timeIntervalSince1970: fixture.doctor.generatedAt)
            isDemoMode = true
            phase = .signedIn
        } catch {
            errorMessage = "Die lokale Vorschau konnte nicht geladen werden."
            phase = .signedOut
        }
    }

    func reloadConfiguration() async {
        let activity = beginActivity()
        defer { endActivity(activity) }
        do {
            let newConfig = try await withCurrentClient { try await $0.getConfig() }
            config = newConfig
            jobDefinitions = newConfig.backup.jobs
            configState = .loaded
            configSaveIssue = nil
            configWarnings = []
        } catch is CancellationError {
        } catch {
            errorMessage = userMessage(for: error)
        }
    }

    @discardableResult
    func saveConfiguration(
        pairs: [PairConfig],
        definitions: [JobDefinition],
        baseRevision: String? = nil,
        currentPassword: String? = nil
    ) async -> Bool {
        guard let currentClient = client, let currentConfig = config else { return false }
        let draftRevision = baseRevision ?? currentConfig.revision
        guard draftRevision == currentConfig.revision else {
            configSaveIssue = .conflict(
                "Die Serverkonfiguration wurde geändert, während dieser Entwurf offen war. Lade den Serverstand neu und übernimm deine Änderungen erneut."
            )
            return false
        }
        let session = sessionGeneration
        isSavingConfig = true
        configSaveIssue = nil
        configWarnings = []
        defer {
            if isCurrentSession(session) { isSavingConfig = false }
        }
        do {
            let response = try await currentClient.updateConfig(
                currentConfig.replacing(pairs: pairs, jobs: definitions, revision: draftRevision),
                currentPassword: currentPassword
            )
            guard isCurrentSession(session) else { return false }
            config = response.config
            jobDefinitions = response.config.backup.jobs
            configWarnings = response.warnings
            actionMessage = response.warnings.isEmpty
                ? "Konfiguration gespeichert."
                : "Konfiguration gespeichert – Hinweise bitte prüfen."
            await refresh()
            return response.ok
        } catch APIError.configConflict(let message, _) {
            guard isCurrentSession(session) else { return false }
            configSaveIssue = .conflict(message)
        } catch APIError.configRevisionRequired(let message, _) {
            guard isCurrentSession(session) else { return false }
            configSaveIssue = .conflict(message)
        } catch APIError.configReauthenticationRequired(let message) {
            guard isCurrentSession(session) else { return false }
            configSaveIssue = .passwordRequired(message)
        } catch APIError.configValidation(let errors) {
            guard isCurrentSession(session) else { return false }
            configSaveIssue = .validation(errors)
        } catch APIError.unauthenticated {
            guard isCurrentSession(session) else { return false }
            signOutLocally()
        } catch {
            guard isCurrentSession(session) else { return false }
            errorMessage = userMessage(for: error)
        }
        return false
    }

    func jobDefinitionPlan(id: String) async -> JobPlan? {
        guard let currentClient = client else { return nil }
        let session = sessionGeneration
        do {
            let plan = try await currentClient.getJobDefinitionPlan(id: id, dryRun: true)
            guard isCurrentSession(session) else { return nil }
            return plan
        } catch APIError.unauthenticated {
            guard isCurrentSession(session) else { return nil }
            signOutLocally()
        } catch {
            guard isCurrentSession(session) else { return nil }
            errorMessage = userMessage(for: error)
        }
        return nil
    }

    @discardableResult
    func runJobDefinition(id: String, dryRun: Bool) async -> Bool {
        guard let currentClient = client else { return false }
        let session = sessionGeneration
        do {
            let response = try await currentClient.runJobDefinition(id: id, dryRun: dryRun)
            guard isCurrentSession(session) else { return false }
            actionMessage = response.ok
                ? (dryRun ? "Probelauf wurde gestartet." : "Job wurde gestartet.")
                : (response.error ?? "Job konnte nicht gestartet werden.")
            if response.ok {
                beginRunTracking(response)
                await refreshVisibleRunData()
            }
            return response.ok
        } catch APIError.unauthenticated {
            guard isCurrentSession(session) else { return false }
            signOutLocally()
        } catch {
            guard isCurrentSession(session) else { return false }
            errorMessage = userMessage(for: error)
        }
        return false
    }

    func runAllJobDefinitions(dryRun: Bool = false) async -> Bool {
        do {
            let response = try await withCurrentClient {
                try await $0.runAllJobDefinitions(dryRun: dryRun)
            }
            actionMessage = response.ok
                ? (dryRun ? "Probeläufe für alle Jobs wurden gestartet." : "Alle aktiven Jobs wurden gestartet.")
                : (response.error ?? "Jobs konnten nicht gestartet werden.")
            if response.ok {
                batchDefinitions = response.definitions
                beginRunTracking(response)
                await refreshVisibleRunData()
            }
            return response.ok
        } catch is CancellationError {
            return false
        } catch {
            errorMessage = userMessage(for: error)
            return false
        }
    }

    func runQuickSync(_ request: QuickSyncRequest) async -> Bool {
        await runOperationalAction(
            success: request.dryRun ? "Quick-Sync-Probelauf wurde gestartet." : "Quick Sync wurde gestartet."
        ) { client in
            try await client.runQuickSync(request)
        }
    }

    func checkDataPath(name: String) async -> Bool {
        await runOperationalAction(success: "Datenweg-Prüfung wurde gestartet.") { client in
            try await client.checkPair(name: name)
        }
    }

    func runRestoreTest(pair: String?) async -> Bool {
        await runOperationalAction(
            success: pair == nil ? "Systemweiter Wiederherstellungstest wurde gestartet." : "Wiederherstellungstest wurde gestartet."
        ) { client in
            try await client.runRestoreTest(pair: pair)
        }
    }

    func savePBSConfiguration(
        _ configuration: PBSConfiguration,
        currentPassword: String? = nil
    ) async -> Bool {
        guard let currentConfig = config else { return false }
        let saved = await saveCompleteConfig(
            currentConfig.replacingPBSConfiguration(configuration),
            currentPassword: currentPassword
        )
        if saved { await refresh() }
        return saved
    }

    func changePassword(current: String, new: String) async -> Bool {
        do {
            let response = try await withCurrentClient {
                try await $0.changePassword(current: current, new: new)
            }
            guard response.ok else { return false }
            signOutLocally()
            errorMessage = "Passwort geändert. Bitte melde dich mit dem neuen Passwort erneut an."
            return true
        } catch {
            errorMessage = userMessage(for: error)
            return false
        }
    }

    func requireFreshLogin(_ message: String) {
        signOutLocally()
        errorMessage = message
    }

    func withCurrentClient<Value>(
        _ operation: (any APIClientProtocol) async throws -> Value
    ) async throws -> Value {
        guard let currentClient = client else { throw APIError.unauthenticated }
        let session = sessionGeneration
        do {
            let value = try await operation(currentClient)
            guard isCurrentSession(session) else { throw CancellationError() }
            return value
        } catch APIError.unauthenticated {
            guard isCurrentSession(session) else { throw CancellationError() }
            signOutLocally()
            throw APIError.unauthenticated
        }
    }

    private func runOperationalAction(
        success: String,
        _ operation: (any APIClientProtocol) async throws -> ActionResponse
    ) async -> Bool {
        do {
            let response = try await withCurrentClient(operation)
            actionMessage = response.ok ? success : (response.error ?? "Aktion konnte nicht gestartet werden.")
            if response.ok {
                beginRunTracking(response)
                await refreshVisibleRunData()
            }
            return response.ok
        } catch is CancellationError {
            return false
        } catch {
            errorMessage = userMessage(for: error)
            return false
        }
    }

    private func saveCompleteConfig(
        _ snapshot: ConfigSnapshot,
        currentPassword: String?
    ) async -> Bool {
        guard let currentClient = client else { return false }
        let session = sessionGeneration
        isSavingConfig = true
        configSaveIssue = nil
        configWarnings = []
        defer { if isCurrentSession(session) { isSavingConfig = false } }
        do {
            let response = try await currentClient.updateConfig(snapshot, currentPassword: currentPassword)
            guard isCurrentSession(session) else { return false }
            config = response.config
            jobDefinitions = response.config.backup.jobs
            configWarnings = response.warnings
            actionMessage = "Konfiguration gespeichert."
            return response.ok
        } catch APIError.configConflict(let message, _) {
            guard isCurrentSession(session) else { return false }
            configSaveIssue = .conflict(message)
        } catch APIError.configRevisionRequired(let message, _) {
            guard isCurrentSession(session) else { return false }
            configSaveIssue = .conflict(message)
        } catch APIError.configReauthenticationRequired(let message) {
            guard isCurrentSession(session) else { return false }
            configSaveIssue = .passwordRequired(message)
        } catch APIError.configValidation(let errors) {
            guard isCurrentSession(session) else { return false }
            configSaveIssue = .validation(errors)
        } catch APIError.unauthenticated {
            guard isCurrentSession(session) else { return false }
            signOutLocally()
        } catch {
            guard isCurrentSession(session) else { return false }
            errorMessage = userMessage(for: error)
        }
        return false
    }

    func runBackup(pair: String? = nil, dryRun: Bool = false) async -> Bool {
        do {
            let response = try await withCurrentClient {
                try await $0.runBackup(pair: pair, dryRun: dryRun)
            }
            actionMessage = response.ok
                ? (dryRun ? "Probelauf wurde gestartet." : "Sicherung wurde gestartet.")
                : (response.error ?? "Sicherung konnte nicht gestartet werden.")
            if response.ok {
                beginRunTracking(response)
                await refreshVisibleRunData()
            }
            return response.ok
        } catch is CancellationError {
            return false
        } catch {
            errorMessage = userMessage(for: error)
            return false
        }
    }

    func pauseScheduler(minutes: Int) async {
        do {
            _ = try await withCurrentClient { try await $0.pauseScheduler(minutes: minutes) }
            actionMessage = "Zeitpläne wurden für \(minutes) Minuten pausiert."
            await refresh()
        } catch is CancellationError {
        } catch {
            errorMessage = userMessage(for: error)
        }
    }

    func cancelBackup() async -> Bool {
        do {
            let response = try await withCurrentClient { try await $0.cancelBackup() }
            actionMessage = response.ok ? "Abbruch wurde angefordert." : (response.error ?? "Kein laufender Job.")
            await refreshProgress()
            return response.ok
        } catch is CancellationError {
            return false
        } catch {
            errorMessage = userMessage(for: error)
            return false
        }
    }

    func resumeScheduler() async {
        do {
            _ = try await withCurrentClient { try await $0.resumeScheduler() }
            actionMessage = "Zeitpläne laufen wieder."
            await refresh()
        } catch is CancellationError {
        } catch {
            errorMessage = userMessage(for: error)
        }
    }

    func runPBS(target: String?) async {
        do {
            let response = try await withCurrentClient { try await $0.runPBS(target: target) }
            actionMessage = response.ok ? "PBS-Sicherung wurde gestartet." : (response.error ?? "PBS-Sicherung konnte nicht starten.")
            let newPBS = try await withCurrentClient { try await $0.getPBSStatus() }
            pbs = newPBS
        } catch is CancellationError {
        } catch {
            errorMessage = userMessage(for: error)
        }
    }

    func cancelPBS() async {
        do {
            let response = try await withCurrentClient { try await $0.cancelPBS() }
            actionMessage = response.ok ? "PBS-Abbruch wurde angefordert." : (response.error ?? "Kein laufender PBS-Job.")
            let newPBS = try await withCurrentClient { try await $0.getPBSStatus() }
            pbs = newPBS
        } catch is CancellationError {
        } catch {
            errorMessage = userMessage(for: error)
        }
    }

    func registerPushDevice(token: String, environment: String) async {
        guard phase == .signedIn, let currentClient = client else { return }
        if desiredPushToken == token, desiredPushEnvironment == environment {
            if registeredPushToken == token { return }
            if let pushSyncTask {
                await pushSyncTask.value
                return
            }
        }
        let session = sessionGeneration
        let server = serverAddress
        if let previous = desiredPushToken ?? registeredPushToken, previous != token {
            rememberPendingPushRevocation(server: server, token: previous)
            knownPushTokens.insert(previous)
        }
        desiredPushToken = token
        desiredPushEnvironment = environment
        knownPushTokens.insert(token)

        let preceding = pushSyncTask
        let owner = UUID()
        pushSyncOwner = owner
        let task = Task { [weak self] in
            await preceding?.value
            guard let self else { return }
            await self.reconcilePushRegistration(
                token: token,
                environment: environment,
                client: currentClient,
                server: server,
                session: session
            )
        }
        pushSyncTask = task
        await task.value
        if pushSyncOwner == owner {
            pushSyncTask = nil
            pushSyncOwner = nil
        }
    }

    func logout() async {
        if isDemoMode {
            beginSessionTransition()
            clearSessionState()
            return
        }
        let exitingClient = client
        let exitingServer = serverAddress
        let pendingRegistration = pushSyncTask
        var tokens = knownPushTokens
        if let desiredPushToken { tokens.insert(desiredPushToken) }
        if let registeredPushToken { tokens.insert(registeredPushToken) }
        for token in tokens {
            rememberPendingPushRevocation(server: exitingServer, token: token)
        }
        desiredPushToken = nil
        desiredPushEnvironment = nil

        // Wechsel die sichtbare Sitzung sofort. Der kurze Lifecycle-Transport
        // widerruft Push und Server-Session anschließend ohne Connectivity-Wait.
        beginSessionTransition()
        errorMessage = nil
        clearSessionState()
        var warnings: [String] = []
        await pendingRegistration?.value
        if let exitingClient {
            for token in tokens {
                do {
                    _ = try await exitingClient.unregisterPushDevice(token: token)
                    removePendingPushRevocation(server: exitingServer, token: token)
                } catch {
                    rememberPendingPushRevocation(server: exitingServer, token: token)
                }
            }
            if pendingPushRevocations().contains(where: { $0.server == exitingServer && tokens.contains($0.token) }) {
                warnings.append("Die Push-Registrierung konnte offline nicht sofort vom Server entfernt werden. Sie wird beim nächsten Login erneut widerrufen und läuft serverseitig automatisch aus.")
            }
            do {
                let result = try await exitingClient.logout()
                if !result.globalRevocation {
                    warnings.append(result.detail ?? "Andere Sitzungen konnten nicht sicher beendet werden. Bitte später erneut anmelden und noch einmal abmelden.")
                }
            } catch {
                warnings.append("Ob andere Sitzungen beendet wurden, konnte nicht bestätigt werden: \(userMessage(for: error))")
            }
            exitingClient.clearLocalSession()
        }
        errorMessage = warnings.isEmpty ? nil : warnings.joined(separator: "\n\n")
        registeredPushToken = nil
        knownPushTokens.removeAll()
        pushSyncTask = nil
        pushSyncOwner = nil
    }

    func retryJob(id: Int) async -> Bool {
        await runOperationalAction(success: "Job wurde erneut gestartet.") { client in
            try await client.retryJob(id: id, dryRun: false)
        }
    }

    func requestRunNavigation(id: Int) {
        guard id > 0 else { return }
        requestedRunID = id
    }

    func consumeRequestedRun(id: Int) {
        guard requestedRunID == id else { return }
        requestedRunID = nil
    }

    private func reconcilePushRegistration(
        token: String,
        environment: String,
        client currentClient: any APIClientProtocol,
        server: String,
        session: Int
    ) async {
        let version = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? ""
        await retryPendingPushRevocations(using: currentClient, server: server)
        guard isCurrentSession(session), desiredPushToken == token else {
            rememberPendingPushRevocation(server: server, token: token)
            return
        }
        do {
            _ = try await currentClient.registerPushDevice(
                token: token,
                environment: environment,
                appVersion: version
            )
            if isCurrentSession(session), desiredPushToken == token,
               desiredPushEnvironment == environment, phase == .signedIn {
                registeredPushToken = token
                removePendingPushRevocation(server: server, token: token)
            } else {
                do {
                    _ = try await currentClient.unregisterPushDevice(token: token)
                    removePendingPushRevocation(server: server, token: token)
                } catch {
                    rememberPendingPushRevocation(server: server, token: token)
                }
            }
        } catch let APIError.server(status, _) where status == 404 || status == 405 {
            // Ältere Server bleiben ohne Push weiterhin vollständig nutzbar.
        } catch APIError.unauthenticated {
            if isCurrentSession(session) { signOutLocally() }
            rememberPendingPushRevocation(server: server, token: token)
        } catch {
            rememberPendingPushRevocation(server: server, token: token)
            if isCurrentSession(session), desiredPushToken == token {
                errorMessage = "Push-Benachrichtigungen konnten nicht eingerichtet werden: \(userMessage(for: error))"
            }
        }
    }

    private func beginRunTracking(_ response: ActionResponse) {
        let expectedDefinitionIDs = Set(response.definitions.map(\.definitionID).filter { !$0.isEmpty })
        let expectedJobIDs = Set([response.jobID].compactMap { $0 })
        guard !expectedDefinitionIDs.isEmpty || !expectedJobIDs.isEmpty else { return }
        runTrackingTask?.cancel()
        batchDefinitions = response.definitions
        batchIsRunning = true
        let session = sessionGeneration
        let startedAt = Date().timeIntervalSince1970
        runTrackingTask = Task { [weak self] in
            guard let self else { return }
            await self.trackRun(
                expectedDefinitionIDs: expectedDefinitionIDs,
                expectedJobIDs: expectedJobIDs,
                startedAt: startedAt,
                session: session
            )
        }
    }

    private func trackRun(
        expectedDefinitionIDs: Set<String>,
        expectedJobIDs: Set<Int>,
        startedAt: Double,
        session: Int
    ) async {
        var quietPolls = 0
        var observedDefinitions: Set<String> = []
        while !Task.isCancelled, isCurrentSession(session), let currentClient = client {
            do {
                try await Task.sleep(for: runPollInterval)
                async let progressRequest = currentClient.getProgress()
                async let jobsRequest = currentClient.getJobs(limit: 100)
                let (newProgress, response) = try await (progressRequest, jobsRequest)
                guard isCurrentSession(session) else { return }
                acceptProgress(newProgress)
                jobs = response.items
                jobsState = .loaded

                let relevant = response.items.filter {
                    $0.startedAt >= startedAt - 5 && (
                        expectedJobIDs.contains($0.id)
                        || $0.definitionID.map(expectedDefinitionIDs.contains) == true
                    )
                }
                observedDefinitions.formUnion(relevant.compactMap(\.definitionID))
                if !batchDefinitions.isEmpty {
                    batchDefinitions = batchDefinitions.map { definition in
                        guard let run = relevant.first(where: { $0.definitionID == definition.definitionID }) else {
                            return definition
                        }
                        return BatchDefinitionState(
                            definitionID: definition.definitionID,
                            definitionName: definition.definitionName,
                            state: run.status,
                            jobID: run.id
                        )
                    }
                }

                let observedJobs = Set(relevant.map(\.id))
                let hasRelevantRunning = relevant.contains { $0.status == "running" }
                let allDefinitionsObserved = expectedDefinitionIDs.isSubset(of: observedDefinitions)
                let allJobsObserved = expectedJobIDs.isSubset(of: observedJobs)
                if !newProgress.running && !hasRelevantRunning {
                    quietPolls += 1
                } else {
                    quietPolls = 0
                }
                if (allDefinitionsObserved && allJobsObserved && quietPolls >= 1) || quietPolls >= 3 {
                    if !allDefinitionsObserved {
                        batchDefinitions = batchDefinitions.map { definition in
                            guard !observedDefinitions.contains(definition.definitionID) else { return definition }
                            return BatchDefinitionState(
                                definitionID: definition.definitionID,
                                definitionName: definition.definitionName,
                                state: "nicht gestartet",
                                jobID: nil
                            )
                        }
                    }
                    batchIsRunning = false
                    await refreshVisibleRunData()
                    return
                }
            } catch APIError.unauthenticated {
                guard isCurrentSession(session) else { return }
                signOutLocally()
                return
            } catch is CancellationError {
                return
            } catch {
                guard isCurrentSession(session) else { return }
                recordProgressFailure()
            }
        }
    }

    private func refreshVisibleRunData() async {
        do {
            async let overviewRequest = withCurrentClient { try await $0.getOverview() }
            async let jobsRequest = withCurrentClient { try await $0.getJobs(limit: 100) }
            async let progressRequest = withCurrentClient { try await $0.getProgress() }
            async let storageRequest = withCurrentClient {
                try await $0.getStorage(includeSizes: false, forceRefresh: false)
            }
            let (newOverview, jobResponse, newProgress, baseStorage) = try await (
                overviewRequest, jobsRequest, progressRequest, storageRequest
            )
            overview = newOverview
            overviewState = .loaded
            jobs = jobResponse.items
            jobsState = .loaded
            acceptProgress(newProgress)
            storage = baseStorage.preservingSizes(from: storage)
            storageState = .loaded
        } catch is CancellationError {
        } catch {
            if phase == .signedIn { errorMessage = userMessage(for: error) }
        }
    }

    private func pendingPushRevocations() -> [PendingPushRevocation] {
        guard let data = defaults.data(forKey: pendingPushRevocationsKey) else {
            return []
        }
        return (try? JSONDecoder().decode([PendingPushRevocation].self, from: data)) ?? []
    }

    private func savePendingPushRevocations(_ items: [PendingPushRevocation]) {
        if items.isEmpty {
            defaults.removeObject(forKey: pendingPushRevocationsKey)
            return
        }
        if let data = try? JSONEncoder().encode(items) {
            defaults.set(data, forKey: pendingPushRevocationsKey)
        }
    }

    private func rememberPendingPushRevocation(server: String, token: String) {
        let candidate = PendingPushRevocation(server: server, token: token)
        var items = pendingPushRevocations()
        if !items.contains(candidate) {
            items.append(candidate)
        }
        savePendingPushRevocations(items)
    }

    private func removePendingPushRevocation(server: String, token: String) {
        savePendingPushRevocations(
            pendingPushRevocations().filter {
                $0.server != server || $0.token != token
            }
        )
    }

    private func retryPendingPushRevocations(
        using client: any APIClientProtocol,
        server: String
    ) async {
        let matching = pendingPushRevocations().filter { $0.server == server }
        guard !matching.isEmpty else { return }
        var remaining = pendingPushRevocations()
        for item in matching {
            do {
                _ = try await client.unregisterPushDevice(token: item.token)
                remaining.removeAll { $0 == item }
            } catch {
                // Die Server-Lease begrenzt den Restzeitraum. Ein fehlgeschlagener
                // Nachholversuch darf die eigentliche Anmeldung nicht blockieren.
            }
        }
        savePendingPushRevocations(remaining)
    }

    /// A stored cookie must not become an active app session while revocations
    /// from an earlier logout are still outstanding. Unlike an explicit login,
    /// startup restore has no fresh user confirmation, so any cleanup failure is
    /// handled fail-closed and the persisted cookie is discarded by the caller.
    private func revokePendingPushRegistrationsBeforeRestore(
        using client: any APIClientProtocol,
        server: String
    ) async throws {
        let matching = pendingPushRevocations().filter { $0.server == server }
        guard !matching.isEmpty else { return }
        var remaining = pendingPushRevocations()
        for item in matching {
            _ = try await client.unregisterPushDevice(token: item.token)
            remaining.removeAll { $0 == item }
            savePendingPushRevocations(remaining)
        }
    }

    func cancelSessionRestore() {
        guard phase == .checking else { return }
        beginSessionTransition()
        clearSessionState()
        errorMessage = "Verbindungsprüfung abgebrochen. Du kannst die Serveradresse prüfen und es erneut versuchen."
    }

    func changeServerDuringRestore() {
        guard phase == .checking else { return }
        beginSessionTransition()
        clearSessionState()
        errorMessage = nil
    }

    func dismissMessages() {
        errorMessage = nil
        actionMessage = nil
    }

    @discardableResult
    private func beginSessionTransition() -> Int {
        refreshTask?.cancel()
        refreshTask = nil
        refreshOwner = nil
        sessionGeneration += 1
        refreshGeneration += 1
        return sessionGeneration
    }

    private func isCurrentSession(_ generation: Int) -> Bool {
        generation == sessionGeneration
    }

    private func isCurrent(session: Int, refresh: Int) -> Bool {
        session == sessionGeneration && refresh == refreshGeneration && client != nil
    }

    private func beginActivity() -> UUID {
        let id = UUID()
        activities.insert(id)
        isRefreshing = true
        return id
    }

    private func endActivity(_ id: UUID) {
        activities.remove(id)
        isRefreshing = !activities.isEmpty
    }

    private static func capture<Value>(_ operation: () async throws -> Value) async -> Result<Value, Error> {
        do {
            return .success(try await operation())
        } catch {
            return .failure(error)
        }
    }

    private func handle(_ error: Error, firstError: inout Error?) {
        if error as? APIError == .unauthenticated {
            signOutLocally()
        } else if !(error is CancellationError), firstError == nil {
            firstError = error
        }
    }

    private func acceptProgress(_ value: BackupProgress) {
        progress = value
        progressLastSuccessAt = Date()
        progressConsecutiveFailures = 0
    }

    private func acceptStorageMeasurement(_ value: StorageOverview) {
        guard let summary = value.measurement else {
            let measuredAt = value.pairs
                .flatMap { [$0.sourceSize?.measuredAt, $0.targetSize?.measuredAt] }
                .compactMap { $0 }
                .max()
            storageSizeState = StorageSizeState(
                status: measuredAt == nil ? .failed : .loaded,
                message: measuredAt == nil ? "Der Server hat keine Größenmessung geliefert." : nil,
                lastUpdated: measuredAt.map { Date(timeIntervalSince1970: $0) }
            )
            return
        }
        let status: StorageSizeStatus
        switch summary.state {
        case "loaded": status = .loaded
        case "partial": status = .partial
        case "stale": status = .stale
        case "failed": status = storageSizeState.lastUpdated == nil ? .failed : .stale
        case "loading": status = .loading
        default: status = .failed
        }
        storageSizeState = StorageSizeState(
            status: status,
            message: summary.measurementError,
            lastUpdated: summary.measuredAt.map { Date(timeIntervalSince1970: $0) }
                ?? storageSizeState.lastUpdated
        )
    }

    private func markStorageMeasurementFailure(_ error: Error) {
        let previousDate = storageSizeState.lastUpdated
        storageSizeState = StorageSizeState(
            status: previousDate == nil ? .failed : .stale,
            message: userMessage(for: error),
            lastUpdated: previousDate
        )
    }

    private func recordProgressFailure() {
        progressConsecutiveFailures = min(progressConsecutiveFailures + 1, 999)
    }

    private func signOutLocally() {
        for token in knownPushTokens {
            rememberPendingPushRevocation(server: serverAddress, token: token)
        }
        desiredPushToken = nil
        desiredPushEnvironment = nil
        registeredPushToken = nil
        knownPushTokens.removeAll()
        client?.clearLocalSession()
        beginSessionTransition()
        clearSessionState()
    }

    private func clearSessionState() {
        client = nil
        overview = nil
        storage = nil
        config = nil
        jobDefinitions = []
        jobs = []
        doctor = nil
        progress = nil
        progressLastSuccessAt = nil
        progressConsecutiveFailures = 0
        doctorLastCheckedAt = nil
        doctorIsRefreshing = false
        storageSizesAreLoading = false
        storageSizeState = .idle
        isSavingConfig = false
        configSaveIssue = nil
        configWarnings = []
        pbs = nil
        overviewState = .idle
        storageState = .idle
        configState = .idle
        jobsState = .idle
        pbsState = .idle
        runTrackingTask?.cancel()
        runTrackingTask = nil
        batchDefinitions = []
        batchIsRunning = false
        isDemoMode = false
        phase = .signedOut
    }

    private func userMessage(for error: Error) -> String {
        if let urlError = error as? URLError {
            switch urlError.code {
            case .cannotConnectToHost, .cannotFindHost, .dnsLookupFailed:
                return "Server nicht erreichbar. Prüfe die vollständige Adresse und unter Einstellungen → Datenschutz & Sicherheit → Lokales Netzwerk die Freigabe für Rclone Sync."
            case .notConnectedToInternet, .networkConnectionLost:
                return "Das lokale Netzwerk ist nicht verfügbar. Prüfe WLAN und erlaube Rclone Sync unter Einstellungen → Datenschutz & Sicherheit → Lokales Netzwerk."
            case .timedOut:
                return "Der Server hat nicht innerhalb des Zeitlimits geantwortet. Prüfe Adresse, WLAN und die lokale Netzwerkfreigabe für Rclone Sync."
            case .appTransportSecurityRequiresSecureConnection:
                return "iOS blockiert diese HTTP-Adresse. Verwende eine lokale IP-Adresse oder eine HTTPS-Adresse."
            case .secureConnectionFailed, .serverCertificateUntrusted, .serverCertificateHasBadDate, .serverCertificateHasUnknownRoot:
                return "Die sichere Verbindung konnte nicht geprüft werden. Kontrolliere HTTPS-Adresse und Zertifikat."
            default:
                break
            }
        }
        if let localized = error as? LocalizedError, let message = localized.errorDescription {
            return message
        }
        return error.localizedDescription
    }
}

private extension StorageOverview {
    func preservingSizes(from previous: StorageOverview?) -> StorageOverview {
        guard let previous else { return self }
        let oldPairs = Dictionary(uniqueKeysWithValues: previous.pairs.map { ($0.name, $0) })
        let incomingMeasurement = measurement?.state == "loading" ? nil : measurement
        return StorageOverview(pairs: pairs.map { pair in
            guard let old = oldPairs[pair.name] else { return pair }
            return StoragePair(
                name: pair.name,
                local: pair.local,
                remote: pair.remote,
                direction: pair.direction,
                source: pair.source,
                target: pair.target,
                localDisk: pair.localDisk,
                lastSync: pair.lastSync,
                lastTransferred: pair.lastTransferred,
                sourceSize: preservingMeasurement(pair.sourceSize, previous: old.sourceSize),
                targetSize: preservingMeasurement(pair.targetSize, previous: old.targetSize)
            )
        }, measurement: incomingMeasurement ?? previous.measurement)
    }

    private func preservingMeasurement(_ incoming: PathSize?, previous: PathSize?) -> PathSize? {
        guard let incoming else { return previous }
        guard incoming.count == nil, incoming.bytes == nil, let previous else { return incoming }
        return PathSize(
            path: incoming.path ?? previous.path,
            count: previous.count,
            bytes: previous.bytes,
            error: incoming.error,
            measuredAt: previous.measuredAt,
            measurementStatus: "stale",
            measurementError: incoming.measurementError ?? incoming.error
        )
    }
}
