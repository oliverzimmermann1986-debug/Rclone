import Foundation
import Darwin

/// The chosen destination, not a claim that the server or certificate was verified.
struct ServerLoginTarget: Equatable {
    let url: URL

    /// A persisted profile already stores a base URL, not a browser page.
    init(serverURL: URL) throws {
        url = try APIClient.normalizedServerURL(serverURL.absoluteString)
    }

    init(address: String) throws {
        let input = try APIClient.normalizedServerURL(address)
        guard var components = URLComponents(url: input, resolvingAgainstBaseURL: false) else {
            throw APIError.invalidServer
        }
        var path = components.percentEncodedPath.split(separator: "/", omittingEmptySubsequences: true)
        // Interpret the pasted browser page only at this input boundary. The
        // API client must continue to preserve an already selected base path.
        if path.last == "login" { path.removeLast() }
        components.percentEncodedPath = path.isEmpty ? "" : "/" + path.joined(separator: "/")
        guard let selected = components.url else { throw APIError.invalidServer }
        url = selected
    }

    var address: String { url.absoluteString }
    var usesHTTPS: Bool { url.scheme == "https" }

    var isTailscale: Bool {
        url.host?.lowercased().hasSuffix(".ts.net") == true
    }

    var supportsPasskeys: Bool {
        guard usesHTTPS, let host = url.host, !host.isEmpty else { return false }
        // WebAuthn RP IDs use domain names. A trusted IP certificate alone
        // does not make an IP address a valid relying-party domain.
        if host.contains(":") { return false }
        var ipv4 = in_addr()
        return host.withCString { inet_pton(AF_INET, $0, &ipv4) } != 1
    }
}
