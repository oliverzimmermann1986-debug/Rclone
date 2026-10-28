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
protocol WebAuthnBrowserSessionProtocol: AnyObject {
    func makeRegistrationBinding() throws -> WebAuthnRegistrationBinding
    func authenticate(baseURL: URL, method: String) async throws -> WebAuthnExchange
    func register(baseURL: URL, method: String, token: String, binding: WebAuthnRegistrationBinding) async throws
    func cancel()
}

@MainActor
final class WebAuthnBrowserSession: NSObject, WebAuthnBrowserSessionProtocol, ASWebAuthenticationPresentationContextProviding {
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

    private struct Operation {
        let id: UUID
        let session: ASWebAuthenticationSession
        let continuation: CheckedContinuation<URL, Error>
    }

    private var operation: Operation?

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

        let callbackURL = try await openBrowser(authenticationURL)
        try Task.checkCancellation()
        guard callbackURL.scheme == "rclonesync",
              callbackURL.host == "webauthn",
              let token = URLComponents(url: callbackURL, resolvingAgainstBaseURL: false)?
                .queryItems?.first(where: { $0.name == "token" })?.value,
              !token.isEmpty else { throw SessionError.invalidCallback }
        return WebAuthnExchange(token: token, verifier: verifier)
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

        let callbackURL = try await openBrowser(registrationURL)
        try Task.checkCancellation()
        guard callbackURL.scheme == "rclonesync",
              callbackURL.host == "webauthn-registration",
              URLComponents(url: callbackURL, resolvingAgainstBaseURL: false)?
                .queryItems?.first(where: { $0.name == "status" })?.value == "success" else {
            throw SessionError.invalidRegistrationCallback
        }
    }

    private func openBrowser(_ url: URL) async throws -> URL {
        let id = UUID()
        try Task.checkCancellation()
        return try await withTaskCancellationHandler {
            try Task.checkCancellation()
            return try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<URL, Error>) in
                cancel()
                let browserSession = ASWebAuthenticationSession(
                    url: url,
                    callbackURLScheme: "rclonesync"
                ) { [weak self] callbackURL, error in
                    Task { @MainActor in
                        if let error {
                            self?.finish(id: id, result: .failure(error))
                        } else if let callbackURL {
                            self?.finish(id: id, result: .success(callbackURL))
                        } else {
                            self?.finish(id: id, result: .failure(SessionError.invalidCallback))
                        }
                    }
                }
                browserSession.presentationContextProvider = self
                browserSession.prefersEphemeralWebBrowserSession = true
                operation = Operation(id: id, session: browserSession, continuation: continuation)
                if !browserSession.start() {
                    finish(id: id, result: .failure(SessionError.invalidCallback))
                }
            }
        } onCancel: {
            Task { @MainActor [weak self] in
                guard self?.operation?.id == id else { return }
                self?.cancel()
            }
        }
    }

    func cancel() {
        guard let operation else { return }
        self.operation = nil
        operation.session.cancel()
        operation.continuation.resume(throwing: CancellationError())
    }

    private func finish(id: UUID, result: Result<URL, Error>) {
        guard let operation, operation.id == id else { return }
        self.operation = nil
        operation.continuation.resume(with: result)
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
