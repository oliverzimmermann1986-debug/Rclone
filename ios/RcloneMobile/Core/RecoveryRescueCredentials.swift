import Foundation

struct RecoveryRescueCredentials {
    var passphrase = ""
    var password = ""

    // Match the API's Unicode code-point length bounds.
    var hasValidPassphrase: Bool { (12...1024).contains(passphrase.unicodeScalars.count) }
    var canImport: Bool { hasValidPassphrase && (1...1024).contains(password.unicodeScalars.count) }

    mutating func clear() { passphrase = ""; password = "" }

    func importRequest(envelope: [String: JSONValue], mappings: [String: String]) -> RecoveryRescueRequest? {
        guard canImport else { return nil }
        return RecoveryRescueRequest(envelope: envelope, passphrase: passphrase,
                                    currentPassword: password, mappings: mappings)
    }
}
