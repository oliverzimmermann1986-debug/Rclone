import Foundation

struct RemoteProvider: Decodable, Identifiable {
    let name: String
    let description: String
    var id: String { name }
}

struct RemoteProvidersResponse: Decodable {
    let providers: [RemoteProvider]
}

struct RemoteSetupSession: Decodable {
    let id: String
    let name: String
    let step: Int
    let ready: Bool
    let error: String
    let option: RemoteSetupOption
}

struct RemoteSetupOption: Decodable {
    struct Example: Decodable, Identifiable {
        let value: String
        let help: String
        var id: String { value }
    }
    let name: String
    let labelDe: String?
    let help: String
    let hintDe: String?
    let authUrl: String?
    let directAuthUrl: String?
    let authorizeCommand: String?
    let secret: Bool
    let required: Bool
    let defaultValue: String
    let exclusive: Bool
    let examples: [Example]

    enum CodingKeys: String, CodingKey {
        case name, help, secret, required, exclusive, examples
        case labelDe = "label_de"
        case hintDe = "hint_de"
        case authUrl = "auth_url"
        case directAuthUrl = "direct_auth_url"
        case authorizeCommand = "authorize_command"
        case defaultValue = "default"
    }
}

struct RemoteSetupStart: Encodable {
    let name: String
    let provider: String
    var express: Bool? = nil
    var scope: String? = nil
    var rootFolderId: String? = nil
    var teamDrive: Bool? = nil

    enum CodingKeys: String, CodingKey {
        case name, provider, express, scope
        case rootFolderId = "root_folder_id"
        case teamDrive = "team_drive"
    }
}

struct RemoteSetupAnswer: Encodable {
    let answer: String
    let step: Int
}

struct RemoteSetupSaved: Decodable {
    let ok: Bool
    let name: String
    let path: String
}

struct GoogleDriveServiceAccountRequest: Encodable {
    let name: String
    let serviceAccountJson: String
    let scope: String
    let rootFolderId: String

    enum CodingKeys: String, CodingKey {
        case name, scope
        case serviceAccountJson = "service_account_json"
        case rootFolderId = "root_folder_id"
    }
}

struct GoogleDriveServiceAccountSaved: Decodable {
    let ok: Bool
    let name: String
    let path: String
    let clientEmail: String

    enum CodingKeys: String, CodingKey {
        case ok, name, path
        case clientEmail = "client_email"
    }
}
