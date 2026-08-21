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
    case configConflict(message: String, currentRevision: String?)
    case configRevisionRequired(message: String, currentRevision: String?)
    case configReauthenticationRequired(message: String)
    case configValidation(errors: [String])
    case revisionConflict(message: String, currentRevision: String?)
    case reauthenticationRequired(message: String)

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
        case let .configConflict(message, _), let .configRevisionRequired(message, _):
            message
        case let .configReauthenticationRequired(message):
            message
        case let .configValidation(errors):
            errors.isEmpty ? "Die Konfiguration ist ungültig." : errors.joined(separator: "\n")
        case let .revisionConflict(message, _), let .reauthenticationRequired(message):
            message
        }
    }
}

protocol APIClientProtocol: AnyObject {
    func login(username: String, password: String) async throws
    func getOverview() async throws -> OverviewResponse
    func getStorage(includeSizes: Bool, forceRefresh: Bool) async throws -> StorageOverview
    func getConfig() async throws -> ConfigSnapshot
    func getJobDefinitions() async throws -> [JobDefinition]
    func updateConfig(_ config: ConfigSnapshot, currentPassword: String?) async throws -> ConfigSaveResponse
    func getJobDefinitionPlan(id: String, dryRun: Bool) async throws -> JobPlan
    func runJobDefinition(id: String, dryRun: Bool) async throws -> ActionResponse
    func runQuickSync(_ request: QuickSyncRequest) async throws -> ActionResponse
    func checkPair(name: String) async throws -> ActionResponse
    func runRestoreTest(pair: String?) async throws -> ActionResponse
    func browseLocal(path: String) async throws -> BrowseResponse
    func getAuditEvents(limit: Int) async throws -> AuditResponse
    func getMaintenanceLogs(limit: Int) async throws -> MaintenanceLogsResponse
    func getDatabaseStatus() async throws -> DatabaseStatus
    func pruneDatabase(days: Int, keepLatest: Int) async throws -> DatabasePruneResponse
    func getConfigSnapshots() async throws -> SnapshotListResponse
    func createConfigSnapshot() async throws -> SnapshotCreateResponse
    func restoreConfigSnapshot(_ request: SnapshotRestoreRequest) async throws -> SnapshotRestoreResponse
    func getFilterFile() async throws -> FilterFile
    func saveFilterFile(_ request: FilterFileSaveRequest) async throws -> FilterFileSaveResponse
    func changePassword(current: String, new: String) async throws -> PasswordChangeResponse
    func testWebhook(id: String) async throws -> ActionResponse
    func downloadSupportBundle() async throws -> URL
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

    func getJobDefinitions() async throws -> [JobDefinition] {
        try await get("/api/jobs/definitions")
    }

    func updateConfig(
        _ config: ConfigSnapshot,
        currentPassword: String? = nil
    ) async throws -> ConfigSaveResponse {
        try await put(
            "/api/config",
            body: ConfigUpdateRequest(config: config, currentPassword: currentPassword)
        )
    }

    func getJobDefinitionPlan(id: String, dryRun: Bool = true) async throws -> JobPlan {
        try await get(
            "/api/jobs/definitions/\(Self.pathEncode(id))/plan?dry_run=\(dryRun)"
        )
    }

    func runJobDefinition(id: String, dryRun: Bool = false) async throws -> ActionResponse {
        try await post(
            "/api/jobs/definitions/\(Self.pathEncode(id))/run?dry_run=\(dryRun)"
        )
    }

    func runQuickSync(_ request: QuickSyncRequest) async throws -> ActionResponse {
        try await post("/api/jobs/backup/quick", body: request)
    }

    func checkPair(name: String) async throws -> ActionResponse {
        try await post("/api/jobs/backup/check/\(Self.pathEncode(name))")
    }

    func runRestoreTest(pair: String? = nil) async throws -> ActionResponse {
        let query = pair.map { "?pairs=\(Self.queryEncode($0))" } ?? ""
        return try await post("/api/jobs/backup/restore-test\(query)")
    }

    func browseLocal(path: String = "") async throws -> BrowseResponse {
        try await get("/api/browse/local?path=\(Self.queryEncode(path))")
    }

    func getAuditEvents(limit: Int = 100) async throws -> AuditResponse {
        try await get("/api/maintenance/audit?limit=\(limit)")
    }

    func getMaintenanceLogs(limit: Int = 200) async throws -> MaintenanceLogsResponse {
        try await get("/api/maintenance/logs?limit=\(limit)")
    }

    func getDatabaseStatus() async throws -> DatabaseStatus {
        try await get("/api/maintenance/database")
    }

    func pruneDatabase(days: Int, keepLatest: Int) async throws -> DatabasePruneResponse {
        try await post("/api/maintenance/database/prune?days=\(days)&keep_latest=\(keepLatest)")
    }

    func getConfigSnapshots() async throws -> SnapshotListResponse {
        try await get("/api/maintenance/config/snapshots")
    }

    func createConfigSnapshot() async throws -> SnapshotCreateResponse {
        try await post("/api/maintenance/config/snapshots")
    }

    func restoreConfigSnapshot(_ request: SnapshotRestoreRequest) async throws -> SnapshotRestoreResponse {
        try await post("/api/maintenance/config/snapshots/restore", body: request)
    }

    func getFilterFile() async throws -> FilterFile {
        try await get("/api/config/filter-file")
    }

    func saveFilterFile(_ request: FilterFileSaveRequest) async throws -> FilterFileSaveResponse {
        try await put("/api/config/filter-file", body: request)
    }

    func changePassword(current: String, new: String) async throws -> PasswordChangeResponse {
        try await post(
            "/api/config/change-password",
            body: PasswordChangeRequest(currentPassword: current, newPassword: new)
        )
    }

    func testWebhook(id: String) async throws -> ActionResponse {
        try await post("/api/config/test-webhook", body: WebhookTestRequest(id: id))
    }

    func downloadSupportBundle() async throws -> URL {
        var request = URLRequest(url: url(for: "/api/maintenance/support-bundle"))
        request.httpMethod = "GET"
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else { throw APIError.invalidResponse }
        if http.statusCode == 401 { throw APIError.unauthenticated }
        try validate(response, data: data, allowed: 200..<300)
        let target = FileManager.default.temporaryDirectory
            .appendingPathComponent("rclone-sync-support-\(UUID().uuidString).zip")
        try data.write(to: target, options: .atomic)
        return target
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

    private func put<Body: Encodable, T: Decodable>(_ path: String, body: Body) async throws -> T {
        var request = URLRequest(url: url(for: path))
        request.httpMethod = "PUT"
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
            if let structured = Self.structuredError(
                status: http.statusCode,
                path: request.url?.path ?? "",
                data: data
            ) {
                throw structured
            }
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

    private static func queryEncode(_ value: String) -> String {
        value.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed.subtracting(CharacterSet(charactersIn: "&=+?#"))) ?? value
    }

    private static func structuredError(status: Int, path: String, data: Data) -> APIError? {
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return nil }
        let detail = object["detail"] as? [String: Any]
        let plainDetail = object["detail"] as? String
        let validationDetails = object["detail"] as? [[String: Any]]
        if status == 422, let validationDetails {
            let errors = validationDetails.compactMap { $0["msg"] as? String }
            return .configValidation(errors: errors.isEmpty ? ["Eingaben sind ungültig."] : errors)
        }
        let message = detail?["message"] as? String ?? plainDetail ?? "Anfrage konnte nicht abgeschlossen werden."
        let currentRevision = detail?["current_revision"] as? String
        if status == 403, path.hasSuffix("/change-password") || path.hasSuffix("/snapshots/restore") {
            return .reauthenticationRequired(message: message)
        }
        if status == 409, path.hasSuffix("/filter-file") || path.hasSuffix("/snapshots/restore") {
            return .revisionConflict(message: message, currentRevision: currentRevision)
        }
        guard let detail else { return nil }
        if status == 409, detail["reload_required"] as? Bool == true {
            return .configConflict(message: message, currentRevision: currentRevision)
        }
        if status == 428, detail["reload_required"] as? Bool == true {
            return .configRevisionRequired(message: message, currentRevision: currentRevision)
        }
        if status == 403, detail["reauth_required"] as? Bool == true {
            return .configReauthenticationRequired(message: message)
        }
        if status == 422 {
            return .configValidation(errors: detail["errors"] as? [String] ?? [message])
        }
        return nil
    }
}
