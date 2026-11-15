import Foundation
import XCTest
@testable import RcloneMobile

final class RemoteSetupTests: XCTestCase {
    func testQuestionDecodesDefaultAndSensitiveInput() throws {
        let data = Data(#"{"id":"draft","name":"cloud","step":2,"ready":false,"error":"","option":{"name":"config_token","label_de":"Google-Anmeldung","help":"rclone authorize drive","hint_de":"Im Browser anmelden","auth_url":"http://127.0.0.1:53682/auth?state=abc","direct_auth_url":"https://accounts.google.com/o/oauth2/auth?state=abc","authorize_command":"rclone authorize \"drive\"","secret":true,"required":true,"default":"","exclusive":false,"examples":[]}}"#.utf8)
        let question = try JSONDecoder().decode(RemoteSetupSession.self, from: data)
        XCTAssertFalse(question.ready)
        XCTAssertEqual(question.step, 2)
        XCTAssertTrue(question.option.secret)
        XCTAssertTrue(question.option.required)
        XCTAssertEqual(question.option.defaultValue, "")
        XCTAssertEqual(question.option.directAuthUrl, "https://accounts.google.com/o/oauth2/auth?state=abc")
        XCTAssertEqual(question.option.labelDe, "Google-Anmeldung")
    }

    func testAnswerPreservesOAuthTokenAndStepWithoutExtraFields() throws {
        let token = #"{"access_token":"fixture","refresh_token":"fixture"}"#
        let data = try JSONEncoder().encode(RemoteSetupAnswer(answer: token, step: 7))
        let body = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        XCTAssertEqual(body["answer"] as? String, token)
        XCTAssertEqual(body["step"] as? Int, 7)
        XCTAssertEqual(Set(body.keys), ["answer", "step"])
    }

    func testReadyQuestionAndSavedRemoteDecode() throws {
        let ready = Data(#"{"id":"draft","name":"cloud","step":4,"ready":true,"error":"","option":{"name":"","help":"","secret":false,"required":false,"default":"","exclusive":false,"examples":[]}}"#.utf8)
        XCTAssertTrue(try JSONDecoder().decode(RemoteSetupSession.self, from: ready).ready)
        let saved = Data(#"{"ok":true,"name":"cloud","path":"cloud:"}"#.utf8)
        XCTAssertEqual(try JSONDecoder().decode(RemoteSetupSaved.self, from: saved).path, "cloud:")
    }

    func testICloudProvidersAndSetupModelsEncodeDecodeCleanly() throws {
        let legacyProviders = Data(#"{"providers":[{"name":"iclouddrive","description":"Apple iCloud"}]}"#.utf8)
        let decodedLegacy = try JSONDecoder().decode(RemoteProvidersResponse.self, from: legacyProviders)
        XCTAssertEqual(decodedLegacy.providers.first?.name, "iclouddrive")
        XCTAssertTrue(decodedLegacy.icloudRemotes.isEmpty)

        let withRemotes = Data(#"{"providers":[{"name":"iclouddrive","description":"Apple iCloud"}],"icloud_remotes":[{"name":"icloud","service":"drive","apple_id":"max@icloud.com"}]}"#.utf8)
        let decodedWithRemotes = try JSONDecoder().decode(RemoteProvidersResponse.self, from: withRemotes)
        XCTAssertEqual(decodedWithRemotes.icloudRemotes, [ExistingICloudRemote(name: "icloud", service: "drive", appleId: "max@icloud.com")])

        let start = RemoteSetupStart(
            name: "icloud",
            provider: "iclouddrive",
            express: true,
            icloudService: "photos",
            appleId: "max@icloud.com",
            applePassword: "secret",
            reconnect: true
        )
        let startData = try JSONEncoder().encode(start)
        let startBody = try XCTUnwrap(JSONSerialization.jsonObject(with: startData) as? [String: Any])
        XCTAssertEqual(startBody["provider"] as? String, "iclouddrive")
        XCTAssertEqual(startBody["icloud_service"] as? String, "photos")
        XCTAssertEqual(startBody["apple_id"] as? String, "max@icloud.com")
        XCTAssertEqual(startBody["apple_password"] as? String, "secret")
        XCTAssertEqual(startBody["reconnect"] as? Bool, true)

        let twoFA = Data(#"{"id":"s1","name":"icloud","step":1,"ready":false,"error":"","option":{"name":"config_2fa","label_de":"6-stelliger Apple-2FA-Bestätigungscode","help":"Enter 2FA","input_mode":"numeric","secret":false,"required":true,"default":"","exclusive":false,"examples":[]}}"#.utf8)
        let decodedTwoFA = try JSONDecoder().decode(RemoteSetupSession.self, from: twoFA)
        XCTAssertEqual(decodedTwoFA.option.inputMode, "numeric")
        XCTAssertFalse(decodedTwoFA.option.secret)

        let reconnected = Data(#"{"ok":true,"name":"icloud","path":"icloud:","reconnected":true}"#.utf8)
        XCTAssertEqual(try JSONDecoder().decode(RemoteSetupSaved.self, from: reconnected).reconnected, true)
    }
}
