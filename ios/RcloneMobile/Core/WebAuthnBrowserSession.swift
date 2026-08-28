import AuthenticationServices
import CryptoKit
import Security
import UIKit

struct WebAuthnExchange {
    let token: String
    let verifier: String
}

@MainActor
final class WebAuthnBrowserSession: NSObject, ASWebAuthenticationPresentationContextProviding {
    enum SessionError: LocalizedError {
        case invalidCallback
        case insecureServer

        var errorDescription: String? {
            switch self {
            case .invalidCallback:
                "Die sichere Anmeldung hat keinen gültigen Einmalcode zurückgegeben."
            case .insecureServer:
                "Passkeys und Sicherheitsschlüssel benötigen eine öffentliche HTTPS-Adresse des Servers."
            }
        }
    }

    private var session: ASWebAuthenticationSession?

    func authenticate(baseURL: URL, method: String) async throws -> WebAuthnExchange {
        guard baseURL.scheme?.lowercased() == "https" else {
            throw SessionError.insecureServer
        }
        let prefix = baseURL.absoluteString.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        guard var components = URLComponents(string: prefix + "/webauthn/native") else {
            throw APIError.invalidServer
        }
        var randomBytes = [UInt8](repeating: 0, count: 32)
        let randomStatus = randomBytes.withUnsafeMutableBytes { buffer in
            SecRandomCopyBytes(kSecRandomDefault, buffer.count, buffer.baseAddress!)
        }
        guard randomStatus == errSecSuccess else {
            throw SessionError.invalidCallback
        }
        let verifier = Data(randomBytes).base64URLEncodedString()
        let appChallenge = Data(SHA256.hash(data: Data(verifier.utf8))).base64URLEncodedString()
        components.queryItems = [
            URLQueryItem(name: "method", value: method),
            URLQueryItem(name: "app_challenge", value: appChallenge)
        ]
        guard let authenticationURL = components.url else { throw APIError.invalidServer }

        return try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                let browserSession = ASWebAuthenticationSession(
                    url: authenticationURL,
                    callbackURLScheme: "rclonesync"
                ) { [weak self] callbackURL, error in
                    defer { self?.session = nil }
                    if let error {
                        continuation.resume(throwing: error)
                        return
                    }
                    guard let callbackURL,
                          callbackURL.scheme == "rclonesync",
                          callbackURL.host == "webauthn",
                          let token = URLComponents(url: callbackURL, resolvingAgainstBaseURL: false)?
                            .queryItems?.first(where: { $0.name == "token" })?.value,
                          !token.isEmpty else {
                        continuation.resume(throwing: SessionError.invalidCallback)
                        return
                    }
                    continuation.resume(returning: WebAuthnExchange(token: token, verifier: verifier))
                }
                browserSession.presentationContextProvider = self
                browserSession.prefersEphemeralWebBrowserSession = true
                session = browserSession
                if !browserSession.start() {
                    session = nil
                    continuation.resume(throwing: SessionError.invalidCallback)
                }
            }
        } onCancel: {
            Task { @MainActor [weak self] in
                self?.session?.cancel()
            }
        }
    }

    func presentationAnchor(for session: ASWebAuthenticationSession) -> ASPresentationAnchor {
        let scenes = UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }
        return scenes.flatMap(\.windows).first(where: \.isKeyWindow)
            ?? scenes.first?.windows.first
            ?? ASPresentationAnchor()
    }
}

private extension Data {
    func base64URLEncodedString() -> String {
        base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
    }
}
