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
    let help: String
    let secret: Bool
    let required: Bool
    let defaultValue: String
    let exclusive: Bool
    let examples: [Example]

    enum CodingKeys: String, CodingKey {
        case name, help, secret, required, exclusive, examples
        case defaultValue = "default"
    }
}

struct RemoteSetupStart: Encodable {
    let name: String
    let provider: String
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
