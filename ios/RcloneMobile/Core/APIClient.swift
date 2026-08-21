import Foundation

enum APIError: LocalizedError, Equatable {
    case invalidServer
    case unauthenticated
    case invalidResponse
    case server(status: Int, message: String)
    case loginFailed
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
        case .missingCSRF:
            "Die Sicherheitssitzung fehlt. Bitte erneut anmelden."
        }
    }
}

final class APIClient {
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
        configuration.waitsForConnectivity = true
        configuration.timeoutIntervalForRequest = 120
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
              let host = components.host else {
            throw APIError.invalidServer
        }
        if scheme == "http", isLocalHost(host), components.port == nil {
            components.port = 8001
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
        let (loginData, loginResponse) = try await session.data(from: url(for: "/login"))
        try validate(loginResponse, data: loginData, allowed: 200..<300)
        guard let html = String(data: loginData, encoding: .utf8),
              let nonce = Self.loginNonce(in: html) else {
            throw APIError.invalidResponse
        }

        var request = URLRequest(url: url(for: "/login"))
        request.httpMethod = "POST"
        request.setValue("application/x-www-form-urlencoded; charset=utf-8", forHTTPHeaderField: "Content-Type")
        request.setValue(origin, forHTTPHeaderField: "Origin")
        request.httpBody = [
            "username": username,
            "password": password,
            "login_csrf": nonce
        ]
        .map { "\(Self.formEncode($0.key))=\(Self.formEncode($0.value))" }
        .sorted()
        .joined(separator: "&")
        .data(using: .utf8)

        let (data, response) = try await session.data(for: request)
        try validate(response, data: data, allowed: 200..<400)
        guard cookie(named: Self.sessionCookie) != nil else { throw APIError.loginFailed }
        _ = try await getConfig()
    }

    func getOverview() async throws -> OverviewResponse {
        try await get("/api/diagnostics/overview")
    }

    func getStorage(includeSizes: Bool = true) async throws -> StorageOverview {
        try await get("/api/storage/overview?include_remote=\(includeSizes ? "true" : "false")")
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

    func logout() async throws {
        var request = URLRequest(url: url(for: "/logout"))
        request.httpMethod = "POST"
        request.setValue(origin, forHTTPHeaderField: "Origin")
        try addCSRF(to: &request)
        let (data, response) = try await session.data(for: request)
        try validate(response, data: data, allowed: 200..<400)
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
            ?? cookieStorage.cookies?.first { $0.name == name && $0.domain.contains(baseURL.host ?? "") }
    }

    private func clearCookies() {
        cookieStorage.cookies?.filter { $0.domain.contains(baseURL.host ?? "") }.forEach(cookieStorage.deleteCookie)
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

    private static func loginNonce(in html: String) -> String? {
        let pattern = #"name=[\"']login_csrf[\"'][^>]*value=[\"']([^\"']+)[\"']"#
        guard let regex = try? NSRegularExpression(pattern: pattern),
              let match = regex.firstMatch(in: html, range: NSRange(html.startIndex..., in: html)),
              let range = Range(match.range(at: 1), in: html) else { return nil }
        return String(html[range])
    }

    private static func formEncode(_ value: String) -> String {
        value.addingPercentEncoding(withAllowedCharacters: CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "-._~"))) ?? ""
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
