import Foundation

struct RemoteProvider: Decodable, Identifiable {
    let name: String
    let description: String
    var id: String { name }
}

struct ExistingICloudRemote: Decodable, Identifiable, Equatable {
    let name: String
    let service: String
    let appleId: String
    var id: String { name }

    enum CodingKeys: String, CodingKey {
        case name, service
        case appleId = "apple_id"
    }
}

struct ExistingGoogleDriveRemote: Decodable, Identifiable, Equatable {
    let name: String
    let hasOwnClient: Bool
    var id: String { name }

    enum CodingKeys: String, CodingKey {
        case name
        case hasOwnClient = "has_own_client"
    }
}

struct RemoteProvidersResponse: Decodable {
    let providers: [RemoteProvider]
    let icloudRemotes: [ExistingICloudRemote]
    let driveRemotes: [ExistingGoogleDriveRemote]
    let driveReconnectSupported: Bool

    enum CodingKeys: String, CodingKey {
        case providers
        case icloudRemotes = "icloud_remotes"
        case driveRemotes = "drive_remotes"
        case driveReconnectSupported = "drive_reconnect_supported"
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        providers = try container.decode([RemoteProvider].self, forKey: .providers)
        icloudRemotes = try container.decodeIfPresent([ExistingICloudRemote].self, forKey: .icloudRemotes) ?? []
        driveRemotes = try container.decodeIfPresent([ExistingGoogleDriveRemote].self, forKey: .driveRemotes) ?? []
        driveReconnectSupported = try container.decodeIfPresent(Bool.self, forKey: .driveReconnectSupported) ?? false
    }
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
    let inputMode: String?
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
        case inputMode = "input_mode"
        case defaultValue = "default"
    }
}

struct RemoteSetupStart: Encodable {
    let name: String
    let provider: String
    var express: Bool? = nil
    var clientId: String? = nil
    var clientSecret: String? = nil
    var requireOwnClient: Bool? = nil
    var scope: String? = nil
    var rootFolderId: String? = nil
    var teamDrive: Bool? = nil
    var icloudService: String? = nil
    var appleId: String? = nil
    var applePassword: String? = nil
    var reconnect: Bool? = nil

    enum CodingKeys: String, CodingKey {
        case name, provider, express, scope, reconnect
        case clientId = "client_id"
        case clientSecret = "client_secret"
        case requireOwnClient = "require_own_client"
        case rootFolderId = "root_folder_id"
        case teamDrive = "team_drive"
        case icloudService = "icloud_service"
        case appleId = "apple_id"
        case applePassword = "apple_password"
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
    let reconnected: Bool?
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
