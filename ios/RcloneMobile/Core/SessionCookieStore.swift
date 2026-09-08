import Foundation
import Security

protocol SessionPersisting {
    func load(for server: URL) -> [HTTPCookie]
    func save(_ cookies: [HTTPCookie], for server: URL)
    func remove(for server: URL)
}

/// Session credentials remain on this device; passwords are never persisted.
struct SessionCookieStore: SessionPersisting {
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
        var request = query(server)
        request[kSecReturnData as String] = true
        request[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        guard SecItemCopyMatching(request as CFDictionary, &result) == errSecSuccess,
              let data = result as? Data,
              let records = try? JSONDecoder().decode([Record].self, from: data) else { return [] }
        return records.compactMap(\.cookie).filter { cookie in
            let domain = cookie.domain.trimmingCharacters(in: CharacterSet(charactersIn: "."))
            return domain.lowercased() == server.host?.lowercased()
                && (!cookie.isSecure || server.scheme == "https")
        }
    }

    func save(_ cookies: [HTTPCookie], for server: URL) {
        guard cookies.contains(where: { $0.name == APIClient.sessionCookie }),
              let data = try? JSONEncoder().encode(cookies.map(Record.init)) else { return }
        let key = query(server)
        let changes = [kSecValueData as String: data]
        let status = SecItemUpdate(key as CFDictionary, changes as CFDictionary)
        if status == errSecItemNotFound {
            var item = key
            item[kSecValueData as String] = data
            item[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
            SecItemAdd(item as CFDictionary, nil)
        }
    }

    func remove(for server: URL) {
        SecItemDelete(query(server) as CFDictionary)
    }
}
