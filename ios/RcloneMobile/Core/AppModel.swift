import Foundation
import Combine

private enum RefreshPayload {
    case overview(Result<OverviewResponse, Error>)
    case baseStorage(Result<StorageOverview, Error>)
    case detailedStorage(Result<StorageOverview, Error>)
    case config(Result<ConfigSnapshot, Error>)
    case definitions(Result<[JobDefinition], Error>)
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

enum ConfigSaveIssue: Equatable {
    case conflict(String)
    case passwordRequired(String)
    case validation([String])
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
    @Published private(set) var progressLastSuccessAt: Date?
    @Published private(set) var progressConsecutiveFailures = 0
    @Published private(set) var doctorLastCheckedAt: Date?
    @Published private(set) var doctorIsRefreshing = false
    @Published private(set) var isSavingConfig = false
    @Published private(set) var configSaveIssue: ConfigSaveIssue?
    @Published private(set) var configWarnings: [String] = []
    @Published private(set) var isRefreshing = false
    @Published var errorMessage: String?
    @Published var actionMessage: String?

    private(set) var client: (any APIClientProtocol)?
    private let defaults: UserDefaults
    private let clientFactory: (URL) -> any APIClientProtocol
    private var sessionGeneration = 0
    private var refreshGeneration = 0
    private var activities: Set<UUID> = []
    private var refreshTask: Task<Void, Never>?
    private var refreshOwner: UUID?

    var serverAddress: String {
        defaults.string(forKey: "serverAddress") ?? ""
    }

    var savedUsername: String {
        defaults.string(forKey: "username") ?? "admin"
    }

    var progressIsStale: Bool {
        guard progressConsecutiveFailures > 0 else { return false }
        if progressConsecutiveFailures >= 3 { return true }
        guard progress?.running == true, let progressLastSuccessAt else { return false }
        return Date().timeIntervalSince(progressLastSuccessAt) >= 15
    }

    init(
        defaults: UserDefaults = .standard,
        clientFactory: @escaping (URL) -> any APIClientProtocol = { APIClient(baseURL: $0) }
    ) {
        self.defaults = defaults
        self.clientFactory = clientFactory
    }

    func restoreSession() async {
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
        defer { endActivity(activity) }

        var firstError: Error?
        await withTaskGroup(of: RefreshPayload.self) { group in
            group.addTask { .overview(await Self.capture { try await refreshClient.getOverview() }) }
            group.addTask { .baseStorage(await Self.capture { try await refreshClient.getStorage(includeSizes: false, forceRefresh: false) }) }
            group.addTask { .detailedStorage(await Self.capture { try await refreshClient.getStorage(includeSizes: true, forceRefresh: false) }) }
            group.addTask { .config(await Self.capture { try await refreshClient.getConfig() }) }
            group.addTask { .definitions(await Self.capture { try await refreshClient.getJobDefinitions() }) }
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
                    // Remote size calculation is optional and may be slow. Its
                    // failure must never discard usable base storage or old sizes.
                    if case let .success(detailed) = result {
                        storage = detailed.preservingSizes(from: storage)
                    } else if case let .failure(error) = result,
                              error as? APIError == .unauthenticated {
                        handle(error, firstError: &firstError)
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
                case let .definitions(result):
                    switch result {
                    case let .success(value):
                        jobDefinitions = value
                    case let .failure(error):
                        if error as? APIError == .unauthenticated {
                            handle(error, firstError: &firstError)
                        }
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
                    case let .success(value): pbs = value
                    case let .failure(error): handle(error, firstError: &firstError)
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
        defer { endActivity(activity) }
        do {
            let detailed = try await currentClient.getStorage(
                includeSizes: true,
                forceRefresh: true
            )
            guard isCurrentSession(session) else { return }
            storage = detailed.preservingSizes(from: storage)
            storageState = .loaded
        } catch APIError.unauthenticated {
            guard isCurrentSession(session) else { return }
            signOutLocally()
        } catch {
            guard isCurrentSession(session) else { return }
            errorMessage = userMessage(for: error)
        }
    }

    func refreshDoctor() async {
        guard let currentClient = client else { return }
        let session = sessionGeneration
        doctorIsRefreshing = true
        defer {
            if isCurrentSession(session) { doctorIsRefreshing = false }
        }
        do {
            let newDoctor = try await currentClient.getDoctor()
            guard isCurrentSession(session) else { return }
            doctor = newDoctor
            doctorLastCheckedAt = Date()
        } catch {
            guard isCurrentSession(session) else { return }
            errorMessage = userMessage(for: error)
        }
    }

    func reloadConfiguration() async {
        guard let currentClient = client else { return }
        let session = sessionGeneration
        let activity = beginActivity()
        defer { endActivity(activity) }
        do {
            async let configRequest = currentClient.getConfig()
            async let definitionsRequest = currentClient.getJobDefinitions()
            let (newConfig, newDefinitions) = try await (configRequest, definitionsRequest)
            guard isCurrentSession(session) else { return }
            config = newConfig
            jobDefinitions = newDefinitions
            configState = .loaded
            configSaveIssue = nil
            configWarnings = []
        } catch APIError.unauthenticated {
            guard isCurrentSession(session) else { return }
            signOutLocally()
        } catch {
            guard isCurrentSession(session) else { return }
            errorMessage = userMessage(for: error)
        }
    }

    @discardableResult
    func saveConfiguration(
        pairs: [PairConfig],
        definitions: [JobDefinition],
        currentPassword: String? = nil
    ) async -> Bool {
        guard let currentClient = client, let currentConfig = config else { return false }
        let session = sessionGeneration
        isSavingConfig = true
        configSaveIssue = nil
        configWarnings = []
        defer {
            if isCurrentSession(session) { isSavingConfig = false }
        }
        do {
            let response = try await currentClient.updateConfig(
                currentConfig.replacing(pairs: pairs, jobs: definitions),
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
            actionMessage = dryRun ? "Probelauf wurde gestartet." : "Job wurde gestartet."
            await refresh()
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

    func runBackup(pair: String? = nil, dryRun: Bool = false) async -> Bool {
        guard let currentClient = client else { return false }
        let session = sessionGeneration
        do {
            let response = try await currentClient.runBackup(pair: pair, dryRun: dryRun)
            guard isCurrentSession(session) else { return false }
            actionMessage = dryRun ? "Probelauf wurde gestartet." : "Sicherung wurde gestartet."
            await refresh()
            return response.ok
        } catch {
            guard isCurrentSession(session) else { return false }
            errorMessage = userMessage(for: error)
            return false
        }
    }

    func pauseScheduler(minutes: Int) async {
        guard let currentClient = client else { return }
        let session = sessionGeneration
        do {
            _ = try await currentClient.pauseScheduler(minutes: minutes)
            guard isCurrentSession(session) else { return }
            actionMessage = "Zeitpläne wurden für \(minutes) Minuten pausiert."
            await refresh()
        } catch {
            guard isCurrentSession(session) else { return }
            errorMessage = userMessage(for: error)
        }
    }

    func cancelBackup() async -> Bool {
        guard let currentClient = client else { return false }
        let session = sessionGeneration
        do {
            let response = try await currentClient.cancelBackup()
            guard isCurrentSession(session) else { return false }
            actionMessage = response.ok ? "Abbruch wurde angefordert." : (response.error ?? "Kein laufender Job.")
            await refreshProgress()
            return response.ok
        } catch {
            guard isCurrentSession(session) else { return false }
            errorMessage = userMessage(for: error)
            return false
        }
    }

    func resumeScheduler() async {
        guard let currentClient = client else { return }
        let session = sessionGeneration
        do {
            _ = try await currentClient.resumeScheduler()
            guard isCurrentSession(session) else { return }
            actionMessage = "Zeitpläne laufen wieder."
            await refresh()
        } catch {
            guard isCurrentSession(session) else { return }
            errorMessage = userMessage(for: error)
        }
    }

    func runPBS(target: String?) async {
        guard let currentClient = client else { return }
        let session = sessionGeneration
        do {
            let response = try await currentClient.runPBS(target: target)
            guard isCurrentSession(session) else { return }
            actionMessage = response.ok ? "PBS-Sicherung wurde gestartet." : (response.error ?? "PBS-Sicherung konnte nicht starten.")
            let newPBS = try await currentClient.getPBSStatus()
            guard isCurrentSession(session) else { return }
            pbs = newPBS
        } catch {
            guard isCurrentSession(session) else { return }
            errorMessage = userMessage(for: error)
        }
    }

    func cancelPBS() async {
        guard let currentClient = client else { return }
        let session = sessionGeneration
        do {
            let response = try await currentClient.cancelPBS()
            guard isCurrentSession(session) else { return }
            actionMessage = response.ok ? "PBS-Abbruch wurde angefordert." : (response.error ?? "Kein laufender PBS-Job.")
            let newPBS = try await currentClient.getPBSStatus()
            guard isCurrentSession(session) else { return }
            pbs = newPBS
        } catch {
            guard isCurrentSession(session) else { return }
            errorMessage = userMessage(for: error)
        }
    }

    func logout() async {
        let exitingClient = client
        beginSessionTransition()
        errorMessage = nil
        clearSessionState()
        do {
            if let result = try await exitingClient?.logout(), !result.globalRevocation {
                errorMessage = result.detail ?? "Du wurdest auf diesem Gerät abgemeldet, aber andere Sitzungen konnten nicht sicher beendet werden. Bitte später erneut anmelden und noch einmal abmelden."
            }
        } catch {
            errorMessage = "Du wurdest auf diesem Gerät abgemeldet. Ob andere Sitzungen beendet wurden, konnte nicht bestätigt werden: \(userMessage(for: error))"
        }
        exitingClient?.clearLocalSession()
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

    private func recordProgressFailure() {
        progressConsecutiveFailures = min(progressConsecutiveFailures + 1, 999)
    }

    private func signOutLocally() {
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
        isSavingConfig = false
        configSaveIssue = nil
        configWarnings = []
        pbs = nil
        overviewState = .idle
        storageState = .idle
        configState = .idle
        jobsState = .idle
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
                return "Der Server hat nicht innerhalb von 30 Sekunden geantwortet. Prüfe Adresse, WLAN und die lokale Netzwerkfreigabe für Rclone Sync."
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
                sourceSize: pair.sourceSize ?? old.sourceSize,
                targetSize: pair.targetSize ?? old.targetSize
            )
        })
    }
}
