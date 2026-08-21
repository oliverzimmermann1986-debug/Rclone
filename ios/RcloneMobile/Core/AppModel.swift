import Foundation
import Combine

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
    @Published var isRefreshing = false
    @Published var errorMessage: String?
    @Published var actionMessage: String?

    private(set) var client: APIClient?
    private let defaults: UserDefaults

    var serverAddress: String {
        defaults.string(forKey: "serverAddress") ?? ""
    }

    var savedUsername: String {
        defaults.string(forKey: "username") ?? "admin"
    }

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
    }

    func restoreSession() async {
        guard !serverAddress.isEmpty else {
            phase = .signedOut
            return
        }
        do {
            let url = try APIClient.normalizedServerURL(serverAddress)
            let client = APIClient(baseURL: url)
            self.client = client
            config = try await client.getConfig()
            phase = .signedIn
            await refresh()
        } catch {
            client = nil
            phase = .signedOut
        }
    }

    func login(server: String, username: String, password: String) async {
        guard !isRefreshing else { return }
        isRefreshing = true
        errorMessage = nil
        defer { isRefreshing = false }
        do {
            let url = try APIClient.normalizedServerURL(server)
            let candidate = APIClient(baseURL: url)
            try await candidate.login(username: username, password: password)
            defaults.set(url.absoluteString, forKey: "serverAddress")
            defaults.set(username, forKey: "username")
            client = candidate
            phase = .signedIn
            await refresh()
        } catch is CancellationError {
            // Cancelling is an explicit user action, not an error condition.
        } catch let urlError as URLError where urlError.code == .cancelled {
            // URLSession translates task cancellation to NSURLErrorCancelled.
        } catch {
            errorMessage = userMessage(for: error)
        }
    }

    func refresh() async {
        guard let client else { return }
        isRefreshing = true
        errorMessage = nil
        defer { isRefreshing = false }
        do {
            async let overviewRequest = client.getOverview()
            async let storageRequest = client.getStorage(includeSizes: true)
            async let configRequest = client.getConfig()
            async let jobsRequest = client.getJobs()
            async let progressRequest = client.getProgress()
            async let pbsRequest = client.getPBSStatus()
            let (newOverview, newStorage, newConfig, newJobs, newProgress, newPBS) = try await (
                overviewRequest,
                storageRequest,
                configRequest,
                jobsRequest,
                progressRequest,
                pbsRequest
            )
            overview = newOverview
            storage = newStorage
            config = newConfig
            jobs = newJobs.items
            progress = newProgress
            pbs = newPBS
        } catch APIError.unauthenticated {
            signOutLocally()
        } catch {
            errorMessage = userMessage(for: error)
        }
    }

    func refreshProgress() async {
        guard let client else { return }
        do {
            progress = try await client.getProgress()
            if progress?.running == false, jobs.first?.status == "running" {
                let response = try await client.getJobs(limit: 50)
                jobs = response.items
            }
        } catch APIError.unauthenticated {
            signOutLocally()
        } catch {
            // Der Poll darf eine anderweitig nutzbare Ansicht nicht mit Meldungen fluten.
        }
    }

    func refreshDoctor() async {
        guard let client else { return }
        do {
            doctor = try await client.getDoctor()
        } catch {
            errorMessage = userMessage(for: error)
        }
    }

    func runBackup(pair: String? = nil, dryRun: Bool = false) async -> Bool {
        guard let client else { return false }
        do {
            let response = try await client.runBackup(pair: pair, dryRun: dryRun)
            actionMessage = dryRun ? "Probelauf wurde gestartet." : "Sicherung wurde gestartet."
            await refresh()
            return response.ok
        } catch {
            errorMessage = userMessage(for: error)
            return false
        }
    }

    func pauseScheduler(minutes: Int) async {
        guard let client else { return }
        do {
            _ = try await client.pauseScheduler(minutes: minutes)
            actionMessage = "Zeitpläne wurden für \(minutes) Minuten pausiert."
            await refresh()
        } catch {
            errorMessage = userMessage(for: error)
        }
    }

    func cancelBackup() async -> Bool {
        guard let client else { return false }
        do {
            let response = try await client.cancelBackup()
            actionMessage = response.ok ? "Abbruch wurde angefordert." : (response.error ?? "Kein laufender Job.")
            await refreshProgress()
            return response.ok
        } catch {
            errorMessage = userMessage(for: error)
            return false
        }
    }

    func resumeScheduler() async {
        guard let client else { return }
        do {
            _ = try await client.resumeScheduler()
            actionMessage = "Zeitpläne laufen wieder."
            await refresh()
        } catch {
            errorMessage = userMessage(for: error)
        }
    }

    func runPBS(target: String?) async {
        guard let client else { return }
        do {
            let response = try await client.runPBS(target: target)
            actionMessage = response.ok ? "PBS-Sicherung wurde gestartet." : (response.error ?? "PBS-Sicherung konnte nicht starten.")
            pbs = try await client.getPBSStatus()
        } catch {
            errorMessage = userMessage(for: error)
        }
    }

    func cancelPBS() async {
        guard let client else { return }
        do {
            let response = try await client.cancelPBS()
            actionMessage = response.ok ? "PBS-Abbruch wurde angefordert." : (response.error ?? "Kein laufender PBS-Job.")
            pbs = try await client.getPBSStatus()
        } catch {
            errorMessage = userMessage(for: error)
        }
    }

    func logout() async {
        do { try await client?.logout() } catch { /* lokale Abmeldung bleibt möglich */ }
        signOutLocally()
    }

    func dismissMessages() {
        errorMessage = nil
        actionMessage = nil
    }

    private func signOutLocally() {
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
