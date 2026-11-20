import Foundation
import Combine
import AuthenticationServices

struct ServerOverviewSnapshot {
    let overview: OverviewResponse
    let storage: StorageOverview?
    let progress: BackupProgress?
    let pbs: PBSStatus?
    let lockdown: LockdownStatusResponse?
    let receivedAt: Date
    let missing: [String]

    var incidents: [ProtectionIncident] {
        ProtectionIncident.collect(overview: overview, storage: storage)
    }

    var runningCount: Int? {
        guard let progress, let pbs else { return nil }
        return (progress.running ? 1 : 0) + (pbs.running ? 1 : 0)
    }

    var needsAttention: Bool {
        lockdown?.active == true || !incidents.isEmpty
            || (overview.system.dataDisk.percentUsed ?? 0) >= 90
            || storage?.pairs.contains { ($0.localDisk?.percentUsed ?? 0) >= 90 } == true
            || !overview.services.scheduler.configuredEnabled
            || overview.services.scheduler.control?.paused == true
            || overview.pairs.total == 0 || overview.pairs.scheduled == 0
            || overview.jobs.lastSuccess == nil
    }

    var hasError: Bool {
        lockdown?.active == true || incidents.contains { $0.severity == "error" }
    }
}

struct ServerOverviewEntry: Identifiable {
    enum State: Equatable {
        case waiting, loading, loaded, needsLogin, unavailable(String)
    }

    var id: String { profile.id }
    var profile: SavedServerProfile
    var state: State = .waiting
    var snapshot: ServerOverviewSnapshot?

    func isCurrent(at now: Date) -> Bool {
        guard state == .loaded, let snapshot else { return false }
        let age = now.timeIntervalSince(snapshot.receivedAt)
        return age >= 0 && age < 75
    }

    func status(at now: Date) -> String {
        switch state {
        case .waiting: return "Noch nicht abgefragt"
        case .loading: return "Wird aktualisiert"
        case .needsLogin: return "Anmeldung nötig"
        case .unavailable: return "Status nicht verfügbar"
        case .loaded:
            guard isCurrent(at: now), let snapshot else { return "Stand veraltet" }
            if snapshot.hasError { return "Handlungsbedarf" }
            if !snapshot.missing.isEmpty { return "Unvollständiger Stand" }
            return snapshot.needsAttention ? "Hinweise prüfen" : "Schutz aktiv"
        }
    }
}

/// Reads each origin through its own cookie jar. No operational actions are exposed here.
@MainActor
final class ServerOverviewModel: ObservableObject {
    private typealias FetchResult = (String, Result<ServerOverviewSnapshot, Error>)
    @Published private(set) var entries: [ServerOverviewEntry] = []
    @Published private(set) var isRefreshing = false
    @Published private(set) var isConnecting = false
    @Published private(set) var connectionError: String?

    private let defaults: UserDefaults
    private let clientFactory: (URL) -> any APIClientProtocol
    private let hasStoredSession: (URL) -> Bool
    private let browserFactory: @MainActor () -> any WebAuthnBrowserSessionProtocol
    private var clients: [String: (username: String, client: any APIClientProtocol)] = [:]
    private var generation = 0
    private var authenticationGeneration = 0
    private var browser: (any WebAuthnBrowserSessionProtocol)?

    init(
        defaults: UserDefaults = .standard,
        clientFactory: @escaping (URL) -> any APIClientProtocol = {
            APIClient(baseURL: $0, waitsForConnectivity: false, resourceTimeout: 20)
        },
        hasStoredSession: @escaping (URL) -> Bool = {
            SessionCookieStore().load(for: $0).contains { $0.name == APIClient.sessionCookie }
        },
        browserFactory: @escaping @MainActor () -> any WebAuthnBrowserSessionProtocol = { WebAuthnBrowserSession() }
    ) {
        self.defaults = defaults
        self.clientFactory = clientFactory
        self.hasStoredSession = hasStoredSession
        self.browserFactory = browserFactory
    }

    func refresh(
        profiles: [SavedServerProfile],
        activeAddress: String,
        activeUsername: String,
        activeClient: (any APIClientProtocol)?
    ) async {
        generation += 1
        let refresh = generation
        isRefreshing = true
        defer { if refresh == generation { isRefreshing = false } }
        let old = Dictionary(entries.map { ($0.id, $0) }, uniquingKeysWith: { _, last in last })
        var seen = Set<String>()
        entries = profiles.filter { seen.insert($0.id).inserted }.map { profile in
            // An account change must not retain another account's snapshot.
            var entry = old[profile.id].flatMap { $0.profile.username == profile.username ? $0 : nil }
                ?? ServerOverviewEntry(profile: profile)
            entry.profile = profile
            entry.state = .loading
            return entry
        }
        let accounts = Dictionary(entries.map { ($0.id, $0.profile.username) }, uniquingKeysWith: { _, last in last })
        clients = clients.filter { accounts[$0.key] == $0.value.username }

        var requests: [(String, any APIClientProtocol)] = []
        for entry in entries {
            let profile = entry.profile
            if profile.address == activeAddress, profile.username == activeUsername, let activeClient {
                requests.append((entry.id, activeClient))
            } else if let cached = clients[entry.id], cached.username == profile.username {
                requests.append((entry.id, cached.client))
            } else if let url = try? APIClient.normalizedServerURL(profile.address),
                      url.scheme == "https",
                      !(defaults.stringArray(forKey: "sessionOnlyServers") ?? []).contains(url.absoluteString),
                      hasStoredSession(url) {
                let client = clientFactory(url)
                clients[entry.id] = (profile.username, client)
                requests.append((entry.id, client))
            } else {
                update(entry.id) { $0.state = .needsLogin; $0.snapshot = nil }
            }
        }

        // At most three servers are queried at once. Results appear independently.
        await withTaskGroup(of: FetchResult.self) { group in
            var next = 0
            func enqueue() {
                guard next < requests.count, !Task.isCancelled else { return }
                let (id, client) = requests[next]
                next += 1
                group.addTask { (id, await Self.capture { try await Self.fetch(client) }) }
            }
            for _ in 0..<min(3, requests.count) { enqueue() }
            while let (id, result) = await group.next() {
                guard refresh == generation, !Task.isCancelled else { group.cancelAll(); continue }
                switch result {
                case let .success(snapshot):
                    update(id) { $0.snapshot = snapshot; $0.state = .loaded }
                case let .failure(error) where error as? APIError == .unauthenticated:
                    clients.removeValue(forKey: id)
                    update(id) { $0.snapshot = nil; $0.state = .needsLogin }
                case let .failure(error):
                    update(id) { $0.state = .unavailable(Self.message(for: error)) }
                }
                enqueue()
            }
        }
    }

    func connect(
        target: ServerLoginTarget, username: String, password: String?, passkeyMethod: String? = nil,
        rememberSession: Bool, insecureTransportConfirmed: Bool, activeAddress: String,
        prepareConnection: () async -> Void = {},
        onConnected: (URL, String, Bool) -> Void
    ) async -> Bool {
        guard !isConnecting else { return false }
        connectionError = nil
        guard target.address != activeAddress else {
            connectionError = "Dieser Server ist bereits die aktive Verbindung."
            return false
        }
        guard target.usesHTTPS || insecureTransportConfirmed else {
            connectionError = "Bestätige zuerst die unverschlüsselte HTTP-Verbindung."
            return false
        }
        guard (passkeyMethod != nil || !username.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty),
              passkeyMethod == nil || ["passkey", "security_key"].contains(passkeyMethod ?? "") else {
            connectionError = "Prüfe Benutzername und Anmeldemethode."
            return false
        }
        authenticationGeneration += 1
        let authentication = authenticationGeneration
        isConnecting = true
        defer {
            if authentication == authenticationGeneration { isConnecting = false; browser = nil }
        }
        do {
            await prepareConnection()
            try Task.checkCancellation()
            guard authentication == authenticationGeneration else { return false }
            let client = clientFactory(target.url)
            client.deferSessionPersistenceUntilAccepted()
            var authenticatedUsername = username
            if let passkeyMethod {
                guard target.supportsPasskeys else { throw APIError.invalidServer }
                let browser = browserFactory()
                self.browser = browser
                let exchange = try await browser.authenticate(baseURL: target.url, method: passkeyMethod)
                try Task.checkCancellation()
                guard authentication == authenticationGeneration else { return false }
                try await client.exchangeWebAuthnToken(exchange.token, verifier: exchange.verifier)
                let configuration = try await client.getConfig()
                guard let configuredUsername = configuration.configuredUsername else {
                    throw APIError.incompatibleResponse(resource: "Kontozuordnung")
                }
                authenticatedUsername = configuredUsername
            } else {
                try await client.login(username: username, password: password ?? "")
            }
            try Task.checkCancellation()
            guard authentication == authenticationGeneration else { return false }
            _ = try await client.getOverview()
            try Task.checkCancellation()
            guard authentication == authenticationGeneration else { return false }
            generation += 1
            isRefreshing = false
            client.setSessionPersistence(rememberSession)
            clients[target.address] = (authenticatedUsername, client)
            onConnected(target.url, authenticatedUsername, rememberSession)
            return true
        } catch is CancellationError {
            return false
        } catch let error as URLError where error.code == .cancelled {
            return false
        } catch let error as ASWebAuthenticationSessionError where error.code == .canceledLogin {
            return false
        } catch {
            if authentication == authenticationGeneration { connectionError = Self.message(for: error) }
            return false
        }
    }

    func cancelConnection() {
        authenticationGeneration += 1
        browser?.cancel()
        browser = nil
        isConnecting = false
        connectionError = nil
    }

    func remove(_ profile: SavedServerProfile) {
        cancelConnection()
        generation += 1
        isRefreshing = false
        clients.removeValue(forKey: profile.id)
        entries.removeAll { $0.id == profile.id }
    }

    func reset() {
        generation += 1
        isRefreshing = false
        cancelConnection()
        clients.removeAll()
        entries.removeAll()
    }

    func showDemo(overview: OverviewResponse?, storage: StorageOverview?, progress: BackupProgress?, pbs: PBSStatus?) {
        reset()
        guard let overview else { return }
        let profile = SavedServerProfile(name: "Beispielserver", address: "https://demo.example", username: "Vorschau", lastUsedAt: 0)
        entries = [ServerOverviewEntry(profile: profile, state: .loaded, snapshot: ServerOverviewSnapshot(
            overview: overview, storage: storage, progress: progress, pbs: pbs,
            lockdown: LockdownStatusResponse(active: false), receivedAt: Date(), missing: []
        ))]
    }

    private func update(_ id: String, _ change: (inout ServerOverviewEntry) -> Void) {
        guard let index = entries.firstIndex(where: { $0.id == id }) else { return }
        change(&entries[index])
    }

    private static func fetch(_ client: any APIClientProtocol) async throws -> ServerOverviewSnapshot {
        // Read-only, inexpensive endpoints; no remote scans or backup starts.
        let overview = try await client.getOverview()
        async let storage = capture { try await client.getStorage(includeSizes: false, forceRefresh: false) }
        async let progress = capture { try await client.getProgress() }
        async let pbs = capture { try await client.getPBSStatus() }
        async let lockdown = capture { try await client.getLockdownStatus() }
        let results = await (storage, progress, pbs, lockdown)
        var missing: [String] = []
        func value<T>(_ result: Result<T, Error>, _ label: String) throws -> T? {
            switch result {
            case let .success(value): return value
            case let .failure(error) where error as? APIError == .unauthenticated: throw APIError.unauthenticated
            case .failure: missing.append(label); return nil
            }
        }
        let storageValue = try value(results.0, "Kopien und Restore-Nachweise")
        let progressValue = try value(results.1, "Laufstatus")
        let pbsValue = try value(results.2, "PBS-Status")
        let lockdownValue = try value(results.3, "Lockdown-Status")
        let now = Date()
        if let storageValue, storageValue.pairs.count < overview.pairs.total {
            missing.append("Vollständige Kopienübersicht")
        }
        if overview.system.dataDisk.freeBytes == nil || overview.system.dataDisk.error != nil {
            missing.append("Speicherzustand des Datenbereichs")
        }
        if !overview.generatedAt.isFinite || abs(now.timeIntervalSince1970 - overview.generatedAt) > 300 {
            missing.append("Aktueller Serverzeitstempel")
        }
        try Task.checkCancellation()
        return ServerOverviewSnapshot(overview: overview, storage: storageValue, progress: progressValue,
            pbs: pbsValue, lockdown: lockdownValue, receivedAt: now, missing: missing)
    }

    private static func capture<T>(_ action: () async throws -> T) async -> Result<T, Error> {
        do { return .success(try await action()) } catch { return .failure(error) }
    }

    private static func message(for error: Error) -> String {
        if let error = error as? URLError {
            switch error.code {
            case .timedOut: return "Der Server antwortet nicht rechtzeitig. Adresse und Netzwerk prüfen."
            case .notConnectedToInternet, .networkConnectionLost: return "Keine Netzwerkverbindung."
            case .cannotFindHost, .cannotConnectToHost, .dnsLookupFailed: return "Serveradresse oder Netzwerk nicht erreichbar."
            default: break
            }
        }
        return (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
    }
}
