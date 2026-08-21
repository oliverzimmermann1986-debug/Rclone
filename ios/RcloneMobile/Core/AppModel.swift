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
    @Published private(set) var jobs: [JobRecord] = []
    @Published private(set) var doctor: DoctorResponse?
    @Published private(set) var progress: BackupProgress?
    @Published private(set) var pbs: PBSStatus?
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
            group.addTask { .baseStorage(await Self.capture { try await refreshClient.getStorage(includeSizes: false) }) }
            group.addTask { .detailedStorage(await Self.capture { try await refreshClient.getStorage(includeSizes: true) }) }
            group.addTask { .config(await Self.capture { try await refreshClient.getConfig() }) }
            group.addTask { .jobs(await Self.capture { try await refreshClient.getJobs(limit: 50) }) }
            group.addTask { .progress(await Self.capture { try await refreshClient.getProgress() }) }
            group.addTask { .pbs(await Self.capture { try await refreshClient.getPBSStatus() }) }

            for await payload in group {
                guard isCurrent(session: session, refresh: refresh) else { continue }
                switch payload {
                case let .overview(result):
                    switch result {
                    case let .success(value): overview = value
                    case let .failure(error): handle(error, firstError: &firstError)
                    }
                case let .baseStorage(result):
                    switch result {
                    case let .success(base):
                        storage = base.preservingSizes(from: storage)
                    case let .failure(error):
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
                    case let .success(value): config = value
                    case let .failure(error): handle(error, firstError: &firstError)
                    }
                case let .jobs(result):
                    switch result {
                    case let .success(response): jobs = response.items
                    case let .failure(error): handle(error, firstError: &firstError)
                    }
                case let .progress(result):
                    switch result {
                    case let .success(value): progress = value
                    case let .failure(error): handle(error, firstError: &firstError)
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
            progress = newProgress
            if newProgress.running == false, jobs.first?.status == "running" {
                let response = try await currentClient.getJobs(limit: 50)
                guard isCurrentSession(session) else { return }
                jobs = response.items
            }
        } catch APIError.unauthenticated {
            guard isCurrentSession(session) else { return }
            signOutLocally()
        } catch {
            // Der Poll darf eine anderweitig nutzbare Ansicht nicht mit Meldungen fluten.
        }
    }

    func refreshDoctor() async {
        guard let currentClient = client else { return }
        let session = sessionGeneration
        do {
            let newDoctor = try await currentClient.getDoctor()
            guard isCurrentSession(session) else { return }
            doctor = newDoctor
        } catch {
            guard isCurrentSession(session) else { return }
            errorMessage = userMessage(for: error)
        }
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
        clearSessionState()
        do { try await exitingClient?.logout() } catch { /* defer im Client löscht lokale Cookies */ }
        exitingClient?.clearLocalSession()
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
        jobs = []
        doctor = nil
        progress = nil
        pbs = nil
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
