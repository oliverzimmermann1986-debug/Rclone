import Foundation
import OSLog
import Security

protocol SessionPersisting {
    func load(for server: URL) -> [HTTPCookie]
    func save(_ cookies: [HTTPCookie], for server: URL)
    func remove(for server: URL)
    func save(_ cookies: [HTTPCookie], for server: URL, replacingSession expected: String?) -> Bool
    func remove(for server: URL, matchingSession expected: String)
}

extension SessionPersisting {
    func save(_ cookies: [HTTPCookie], for server: URL, replacingSession expected: String?) -> Bool {
        guard load(for: server).first(where: { $0.name == APIClient.sessionCookie })?.value == expected else { return false }
        save(cookies, for: server)
        return true
    }

    func remove(for server: URL, matchingSession expected: String) {
        guard load(for: server).first(where: { $0.name == APIClient.sessionCookie })?.value == expected else { return }
        remove(for: server)
    }
}

/// Session credentials remain on this device; passwords are never persisted.
struct SessionCookieStore: SessionPersisting {
    enum StorageError: Error, LocalizedError, Equatable {
        case invalidCookieData
        case keychain(operation: String, status: OSStatus)

        var errorDescription: String? {
            switch self {
            case .invalidCookieData:
                return "Die gespeicherte Sitzung konnte nicht gelesen werden."
            case let .keychain(operation, status):
                return "Schlüsselbund: \(operation) fehlgeschlagen (OSStatus \(status))."
            }
        }
    }

    private static let logger = Logger(subsystem: "de.sicherpfad.server-session", category: "Keychain")
    // All instances in the app share this synchronous boundary. Conditional
    // compare/delete and compare/save must never race another client's write.
    private static let storageLock = NSLock()

    private struct Record: Codable {
        let name: String
        let value: String
        let domain: String
        let path: String
        let secure: Bool
        let expiresAt: Date

        init(_ cookie: HTTPCookie) {
            name = cookie.name
            value = cookie.value
            domain = cookie.domain
            path = cookie.path
            secure = cookie.isSecure
            expiresAt = cookie.expiresDate ?? Date().addingTimeInterval(24 * 60 * 60)
        }

        var cookie: HTTPCookie? {
            guard expiresAt > Date() else { return nil }
            var properties: [HTTPCookiePropertyKey: Any] = [
                .name: name, .value: value, .domain: domain, .path: path,
                .expires: expiresAt
            ]
            if secure { properties[.secure] = "TRUE" }
            return HTTPCookie(properties: properties)
        }
    }

    private func query(_ server: URL) -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: "de.sicherpfad.server-session",
         kSecAttrAccount as String: server.absoluteString]
    }

    func load(for server: URL) -> [HTTPCookie] {
        Self.storageLock.lock()
        defer { Self.storageLock.unlock() }
        do { return try loadUnlocked(for: server) }
        catch let error as StorageError where error == .invalidCookieData {
            // Remove the exact corrupt state observed under the same lock.
            // A newer client's valid save must never be deleted during repair.
            SecItemDelete(query(server) as CFDictionary)
            Self.logger.error("\(error.localizedDescription, privacy: .public)")
            return []
        }
        catch {
            // No cookie values, account addresses or credential data enter logs.
            Self.logger.error("\(error.localizedDescription, privacy: .public)")
            return []
        }
    }

    func loadChecked(for server: URL) throws -> [HTTPCookie] {
        Self.storageLock.lock()
        defer { Self.storageLock.unlock() }
        return try loadUnlocked(for: server)
    }

    private func loadUnlocked(for server: URL) throws -> [HTTPCookie] {
        var request = query(server)
        request[kSecReturnData as String] = true
        request[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        let status = SecItemCopyMatching(request as CFDictionary, &result)
        if status == errSecItemNotFound { return [] }
        guard status == errSecSuccess else { throw StorageError.keychain(operation: "Lesen", status: status) }
        guard let data = result as? Data else { throw StorageError.invalidCookieData }
        return try Self.decodeCookies(data, for: server)
    }

    static func encodeCookies(_ cookies: [HTTPCookie]) throws -> Data {
        try JSONEncoder().encode(cookies.map(Record.init))
    }

    static func decodeCookies(_ data: Data, for server: URL) throws -> [HTTPCookie] {
        guard let records = try? JSONDecoder().decode([Record].self, from: data) else {
            throw StorageError.invalidCookieData
        }
        return records.compactMap(\.cookie).filter { cookie in
            let domain = cookie.domain.trimmingCharacters(in: CharacterSet(charactersIn: "."))
            return domain.lowercased() == server.host?.lowercased()
                && (!cookie.isSecure || server.scheme == "https")
        }
    }

    func save(_ cookies: [HTTPCookie], for server: URL) {
        do { try saveChecked(cookies, for: server) }
        catch { Self.logger.error("\(error.localizedDescription, privacy: .public)") }
    }

    func saveChecked(_ cookies: [HTTPCookie], for server: URL) throws {
        Self.storageLock.lock()
        defer { Self.storageLock.unlock() }
        try saveUnlocked(cookies, for: server)
    }

    private func saveUnlocked(_ cookies: [HTTPCookie], for server: URL) throws {
        guard cookies.contains(where: { $0.name == APIClient.sessionCookie }) else { return }
        let data = try Self.encodeCookies(cookies)
        let key = query(server)
        let changes = [kSecValueData as String: data]
        var status = SecItemUpdate(key as CFDictionary, changes as CFDictionary)
        if status == errSecItemNotFound {
            var item = key
            item[kSecValueData as String] = data
            item[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
            status = SecItemAdd(item as CFDictionary, nil)
            if status == errSecDuplicateItem {
                // Another client can create the same server item between update
                // and add. Retrying update preserves the existing credential.
                status = SecItemUpdate(key as CFDictionary, changes as CFDictionary)
            }
        }
        guard status == errSecSuccess else { throw StorageError.keychain(operation: "Speichern", status: status) }
    }

    func remove(for server: URL) {
        Self.storageLock.lock()
        defer { Self.storageLock.unlock() }
        SecItemDelete(query(server) as CFDictionary)
    }

    func save(_ cookies: [HTTPCookie], for server: URL, replacingSession expected: String?) -> Bool {
        Self.storageLock.lock()
        defer { Self.storageLock.unlock() }
        do {
            guard try loadUnlocked(for: server).first(where: { $0.name == APIClient.sessionCookie })?.value == expected else { return false }
            guard cookies.contains(where: { $0.name == APIClient.sessionCookie }) else { return false }
            try saveUnlocked(cookies, for: server)
            return true
        } catch {
            Self.logger.error("\(error.localizedDescription, privacy: .public)")
            return false
        }
    }

    func remove(for server: URL, matchingSession expected: String) {
        Self.storageLock.lock()
        defer { Self.storageLock.unlock() }
        do {
            guard try loadUnlocked(for: server).first(where: { $0.name == APIClient.sessionCookie })?.value == expected else { return }
            SecItemDelete(query(server) as CFDictionary)
        } catch {
            Self.logger.error("\(error.localizedDescription, privacy: .public)")
        }
    }
}
