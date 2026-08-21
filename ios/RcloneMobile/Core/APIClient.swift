import Foundation

enum APIError: LocalizedError, Equatable {
    case invalidServer
    case unauthenticated
    case invalidResponse
    case server(status: Int, message: String)
    case loginFailed
    case loginRateLimited(retryAfterSeconds: Int)
    case loginSecurityFailed
    case missingCSRF

    var errorDescription: String? {
        switch self {
        case .invalidServer:
            "Bitte eine gültige HTTP- oder HTTPS-Adresse eingeben."
        case .unauthenticated:
            "Die Sitzung ist abgelaufen. Bitte erneut anmelden."
        case .invalidResponse:
            "Der Server hat eine unerwartete Antwort gesendet."
        case let .server(_, message):
            message
        case .loginFailed:
            "Benutzername oder Passwort ist falsch."
        case let .loginRateLimited(seconds):
            "Zu viele Anmeldeversuche. Bitte in \(seconds) Sekunden erneut versuchen."
        case .loginSecurityFailed:
            "Die Sicherheitsprüfung der Anmeldung ist fehlgeschlagen. Prüfe die Serveradresse und versuche es erneut."
        case .missingCSRF:
            "Die Sicherheitssitzung fehlt. Bitte erneut anmelden."
        }
    }
}

protocol APIClientProtocol: AnyObject {
    func login(username: String, password: String) async throws
    func getOverview() async throws -> OverviewResponse
    func getStorage(includeSizes: Bool, forceRefresh: Bool) async throws -> StorageOverview
    func getConfig() async throws -> ConfigSnapshot
    func getJobs(limit: Int) async throws -> JobSearchResponse
    func getJob(id: Int) async throws -> JobRecord
    func getJobLog(id: Int) async throws -> JobLogResponse
    func getDoctor() async throws -> DoctorResponse
    func getProgress() async throws -> BackupProgress
    func getPBSStatus() async throws -> PBSStatus
    func runBackup(pair: String?, dryRun: Bool) async throws -> ActionResponse
    func cancelBackup() async throws -> ActionResponse
    func runPBS(target: String?) async throws -> ActionResponse
    func cancelPBS() async throws -> ActionResponse
    func pauseScheduler(minutes: Int) async throws -> SchedulerControl
    func resumeScheduler() async throws -> SchedulerControl
    func logout() async throws -> LogoutResult
    func clearLocalSession()
}

struct NativeLoginChallenge: Decodable, Equatable {
    let status: String
    let loginCSRF: String

    private enum CodingKeys: String, CodingKey {
        case status
        case loginCSRF = "login_csrf"
    }
}

struct NativeLoginResponse: Decodable, Equatable {
    let status: String
    let retryAfterSeconds: Int?

    private enum CodingKeys: String, CodingKey {
        case status
        case retryAfterSeconds = "retry_after_seconds"
    }
}

private struct NativeLoginRequest: Encodable {
    let username: String
    let password: String
    let loginCSRF: String

    private enum CodingKeys: String, CodingKey {
        case username, password
        case loginCSRF = "login_csrf"
    }
}

final class APIClient: APIClientProtocol {
    static let sessionCookie = "rclone_sync_session"
    static let csrfCookie = "rclone_sync_csrf"

    let baseURL: URL
    private let session: URLSession
    private let cookieStorage: HTTPCookieStorage
    private let decoder: JSONDecoder

    init(baseURL: URL, session: URLSession? = nil, cookieStorage: HTTPCookieStorage = .shared) {
        self.baseURL = baseURL
        self.cookieStorage = cookieStorage
        let configuration = URLSessionConfiguration.default
        configuration.httpCookieStorage = cookieStorage
        configuration.httpShouldSetCookies = true
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        // Local-network privacy can briefly leave a request without a usable
        // route while iOS presents its permission dialog. Keep the request alive
        // long enough to continue after approval, but retain a bounded resource
        // timeout and the login screen's explicit cancel action.
        configuration.waitsForConnectivity = true
        configuration.timeoutIntervalForRequest = 15
        configuration.timeoutIntervalForResource = 30
        self.session = session ?? URLSession(configuration: configuration)
        self.decoder = JSONDecoder()
    }

    static func normalizedServerURL(_ rawValue: String) throws -> URL {
        var value = rawValue.trimmingCharacters(in: .whitespacesAndNewlines)
        let suppliedScheme = value.contains("://")
        if !suppliedScheme {
            guard let host = URLComponents(string: "//" + value)?.host else {
                throw APIError.invalidServer
            }
            value = (isLocalHost(host) ? "http://" : "https://") + value
        }
        guard var components = URLComponents(string: value),
              let scheme = components.scheme?.lowercased(),
              ["http", "https"].contains(scheme),
              components.host != nil,
              components.user == nil,
              components.password == nil else {
            throw APIError.invalidServer
        }
        components.path = ""
        components.query = nil
        components.fragment = nil
        guard let url = components.url else { throw APIError.invalidServer }
        return url
    }

    private static func isLocalHost(_ host: String) -> Bool {
        let normalized = host.lowercased()
        if normalized == "localhost" || normalized.hasSuffix(".local") || !normalized.contains(".") {
            return true
        }
        let octets = normalized.split(separator: ".", omittingEmptySubsequences: false).compactMap { UInt8($0) }
        if octets.count == 4 {
            return octets[0] == 10
                || octets[0] == 127
                || (octets[0] == 169 && octets[1] == 254)
                || (octets[0] == 172 && (16...31).contains(octets[1]))
                || (octets[0] == 192 && octets[1] == 168)
        }
        return normalized == "::1"
            || normalized.hasPrefix("fc")
            || normalized.hasPrefix("fd")
            || ["fe8", "fe9", "fea", "feb"].contains { normalized.hasPrefix($0) }
    }

    func login(username: String, password: String) async throws {
        clearCookies()
        let challenge: NativeLoginChallenge = try await get("/api/auth/login")
        guard challenge.status == "csrf_ready", !challenge.loginCSRF.isEmpty else {
            throw APIError.loginSecurityFailed
        }

        var request = URLRequest(url: url(for: "/api/auth/login"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue(origin, forHTTPHeaderField: "Origin")
        request.httpBody = try JSONEncoder().encode(
            NativeLoginRequest(
                username: username,
                password: password,
                loginCSRF: challenge.loginCSRF
            )
        )

        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse,
              let result = try? decoder.decode(NativeLoginResponse.self, from: data) else {
            throw APIError.invalidResponse
        }
        switch result.status {
        case "success" where (200..<300).contains(http.statusCode):
            break
        case "invalid_credentials":
            throw APIError.loginFailed
        case "rate_limited":
            let headerSeconds = Int(http.value(forHTTPHeaderField: "Retry-After") ?? "")
            throw APIError.loginRateLimited(
                retryAfterSeconds: max(1, result.retryAfterSeconds ?? headerSeconds ?? 1)
            )
        case "csrf_failed", "origin_failed":
            throw APIError.loginSecurityFailed
        default:
            throw APIError.server(
                status: http.statusCode,
                message: "Der Server unterstützt die sichere native Anmeldung nicht vollständig."
            )
        }
        guard cookie(named: Self.sessionCookie) != nil else { throw APIError.loginFailed }
        _ = try await getConfig()
    }

    func getOverview() async throws -> OverviewResponse {
        try await get("/api/diagnostics/overview")
    }

    func getStorage(includeSizes: Bool = true, forceRefresh: Bool = false) async throws -> StorageOverview {
        try await get(
            "/api/storage/overview?include_remote=\(includeSizes ? "true" : "false")&refresh_sizes=\(forceRefresh ? "true" : "false")"
        )
    }

    func getConfig() async throws -> ConfigSnapshot {
        try await get("/api/config")
    }

    func getJobs(limit: Int = 50) async throws -> JobSearchResponse {
        try await get("/api/jobs/search?limit=\(limit)")
    }

    func getJob(id: Int) async throws -> JobRecord {
        try await get("/api/jobs/\(id)")
    }

    func getJobLog(id: Int) async throws -> JobLogResponse {
        try await get("/api/jobs/\(id)/log?tail=600")
    }

    func getDoctor() async throws -> DoctorResponse {
        try await get("/api/diagnostics/doctor")
    }

    func getProgress() async throws -> BackupProgress {
        try await get("/api/jobs/backup/progress")
    }

    func getPBSStatus() async throws -> PBSStatus {
        try await get("/api/pbs/status")
    }

    func runBackup(pair: String? = nil, dryRun: Bool = false) async throws -> ActionResponse {
        if let pair {
            return try await post("/api/jobs/backup/run-pair/\(Self.pathEncode(pair))?dry_run=\(dryRun)")
        }
        return try await post("/api/jobs/backup/run?dry_run=\(dryRun)")
    }

    func cancelBackup() async throws -> ActionResponse {
        try await post("/api/jobs/backup/cancel")
    }

    func runPBS(target: String?) async throws -> ActionResponse {
        try await post("/api/pbs/run", body: PBSRunRequest(target: target))
    }

    func cancelPBS() async throws -> ActionResponse {
        try await post("/api/pbs/cancel")
    }

    func pauseScheduler(minutes: Int) async throws -> SchedulerControl {
        try await post("/api/jobs/scheduler/pause", body: SchedulerPauseRequest(minutes: minutes))
    }

    func resumeScheduler() async throws -> SchedulerControl {
        try await post("/api/jobs/scheduler/resume")
    }

    func logout() async throws -> LogoutResult {
        defer { clearCookies() }
        var request = URLRequest(url: url(for: "/logout"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue(origin, forHTTPHeaderField: "Origin")
        try addCSRF(to: &request)
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else { throw APIError.invalidResponse }
        if (200..<400).contains(http.statusCode) {
            return LogoutResult(
                globalRevocation: true,
                localSessionCleared: true,
                detail: nil
            )
        }
        if let partial = try? decoder.decode(LogoutResult.self, from: data),
           partial.localSessionCleared {
            return partial
        }
        try validate(response, data: data, allowed: 200..<400)
        throw APIError.invalidResponse
    }

    func clearLocalSession() {
        clearCookies()
    }

    private func get<T: Decodable>(_ path: String) async throws -> T {
        var request = URLRequest(url: url(for: path))
        request.httpMethod = "GET"
        return try await send(request)
    }

    private func post<T: Decodable>(_ path: String) async throws -> T {
        var request = URLRequest(url: url(for: path))
        request.httpMethod = "POST"
        request.setValue(origin, forHTTPHeaderField: "Origin")
        try addCSRF(to: &request)
        return try await send(request)
    }

    private func post<Body: Encodable, T: Decodable>(_ path: String, body: Body) async throws -> T {
        var request = URLRequest(url: url(for: path))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue(origin, forHTTPHeaderField: "Origin")
        try addCSRF(to: &request)
        request.httpBody = try JSONEncoder().encode(body)
        return try await send(request)
    }

    private func send<T: Decodable>(_ request: URLRequest) async throws -> T {
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else { throw APIError.invalidResponse }
        if http.statusCode == 401 { throw APIError.unauthenticated }
        if !(200..<300).contains(http.statusCode) {
            throw APIError.server(status: http.statusCode, message: Self.errorMessage(data) ?? "Serverfehler (HTTP \(http.statusCode))")
        }
        do {
            return try decoder.decode(T.self, from: data)
        } catch {
            throw APIError.invalidResponse
        }
    }

    private func addCSRF(to request: inout URLRequest) throws {
        guard let token = cookie(named: Self.csrfCookie)?.value else { throw APIError.missingCSRF }
        request.setValue(token, forHTTPHeaderField: "X-CSRF-Token")
    }

    private func cookie(named name: String) -> HTTPCookie? {
        cookieStorage.cookies(for: baseURL)?.first { $0.name == name }
    }

    private func clearCookies() {
        let appCookieNames = Set([Self.sessionCookie, Self.csrfCookie])
        cookieStorage.cookies(for: baseURL)?
            .filter { appCookieNames.contains($0.name) }
            .forEach(cookieStorage.deleteCookie)
    }

    private var origin: String {
        var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false)
        components?.path = ""
        components?.query = nil
        components?.fragment = nil
        return components?.string?.trimmingCharacters(in: CharacterSet(charactersIn: "/")) ?? baseURL.absoluteString
    }

    private func url(for path: String) -> URL {
        let prefix = baseURL.absoluteString.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        return URL(string: prefix + (path.hasPrefix("/") ? path : "/" + path))!
    }

    private func validate(_ response: URLResponse, data: Data, allowed: Range<Int>) throws {
        guard let http = response as? HTTPURLResponse else { throw APIError.invalidResponse }
        guard allowed.contains(http.statusCode) else {
            throw APIError.server(status: http.statusCode, message: Self.errorMessage(data) ?? "HTTP \(http.statusCode)")
        }
    }

    private static func pathEncode(_ value: String) -> String {
        value.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed.subtracting(CharacterSet(charactersIn: "/?#"))) ?? value
    }

    private static func errorMessage(_ data: Data) -> String? {
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let detail = object["detail"] else { return nil }
        if let text = detail as? String { return text }
        if let dictionary = detail as? [String: Any], let message = dictionary["message"] as? String { return message }
        return nil
    }
}
