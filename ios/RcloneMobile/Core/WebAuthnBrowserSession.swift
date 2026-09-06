import AuthenticationServices
import CryptoKit
import Security
import UIKit

struct WebAuthnExchange {
    let token: String
    let verifier: String
}

struct WebAuthnRegistrationBinding {
    let verifier: String
    let appChallenge: String
}

@MainActor
final class WebAuthnBrowserSession: NSObject, ASWebAuthenticationPresentationContextProviding {
    enum SessionError: LocalizedError {
        case invalidCallback
        case invalidRegistrationCallback
        case insecureServer

        var errorDescription: String? {
            switch self {
            case .invalidCallback:
                "Die sichere Anmeldung hat keinen gültigen Einmalcode zurückgegeben."
            case .invalidRegistrationCallback:
                "Die Passkey-Erstellung wurde nicht vollständig bestätigt."
            case .insecureServer:
                "Passkeys und Sicherheitsschlüssel benötigen eine öffentliche HTTPS-Adresse des Servers."
            }
        }
    }

    private var session: ASWebAuthenticationSession?

    func makeRegistrationBinding() throws -> WebAuthnRegistrationBinding {
        let verifier = try makeVerifier()
        return WebAuthnRegistrationBinding(
            verifier: verifier,
            appChallenge: Data(SHA256.hash(data: Data(verifier.utf8))).base64URLEncodedString()
        )
    }

    func authenticate(baseURL: URL, method: String) async throws -> WebAuthnExchange {
        guard baseURL.scheme?.lowercased() == "https" else {
            throw SessionError.insecureServer
        }
        let prefix = baseURL.absoluteString.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        guard var components = URLComponents(string: prefix + "/webauthn/native") else {
            throw APIError.invalidServer
        }
        let verifier = try makeVerifier()
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

    func register(
        baseURL: URL,
        method: String,
        token: String,
        binding: WebAuthnRegistrationBinding
    ) async throws {
        guard baseURL.scheme?.lowercased() == "https" else {
            throw SessionError.insecureServer
        }
        let prefix = baseURL.absoluteString.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        guard var components = URLComponents(string: prefix + "/webauthn/native/register") else {
            throw APIError.invalidServer
        }
        components.queryItems = [
            URLQueryItem(name: "method", value: method),
            URLQueryItem(name: "token", value: token)
        ]
        components.percentEncodedFragment = "verifier=" + binding.verifier
        guard let registrationURL = components.url else { throw APIError.invalidServer }

        try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                let browserSession = ASWebAuthenticationSession(
                    url: registrationURL,
                    callbackURLScheme: "rclonesync"
                ) { [weak self] callbackURL, error in
                    defer { self?.session = nil }
                    if let error {
                        continuation.resume(throwing: error)
                        return
                    }
                    guard let callbackURL,
                          callbackURL.scheme == "rclonesync",
                          callbackURL.host == "webauthn-registration",
                          URLComponents(url: callbackURL, resolvingAgainstBaseURL: false)?
                            .queryItems?.first(where: { $0.name == "status" })?.value == "success" else {
                        continuation.resume(throwing: SessionError.invalidRegistrationCallback)
                        return
                    }
                    continuation.resume(returning: ())
                }
                browserSession.presentationContextProvider = self
                browserSession.prefersEphemeralWebBrowserSession = true
                session = browserSession
                if !browserSession.start() {
                    session = nil
                    continuation.resume(throwing: SessionError.invalidRegistrationCallback)
                }
            }
        } onCancel: {
            Task { @MainActor [weak self] in
                self?.session?.cancel()
            }
        }
    }

    private func makeVerifier() throws -> String {
        var randomBytes = [UInt8](repeating: 0, count: 32)
        let randomStatus = randomBytes.withUnsafeMutableBytes { buffer in
            SecRandomCopyBytes(kSecRandomDefault, buffer.count, buffer.baseAddress!)
        }
        guard randomStatus == errSecSuccess else {
            throw SessionError.invalidCallback
        }
        return Data(randomBytes).base64URLEncodedString()
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
