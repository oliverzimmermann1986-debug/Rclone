import Foundation
import XCTest
@testable import RcloneMobile

final class RemoteSetupTests: XCTestCase {
    func testQuestionDecodesDefaultAndSensitiveInput() throws {
        let data = Data(#"{"id":"draft","name":"cloud","step":2,"ready":false,"error":"","option":{"name":"config_token","help":"rclone authorize drive","secret":true,"required":true,"default":"","exclusive":false,"examples":[]}}"#.utf8)
        let question = try JSONDecoder().decode(RemoteSetupSession.self, from: data)
        XCTAssertFalse(question.ready)
        XCTAssertEqual(question.step, 2)
        XCTAssertTrue(question.option.secret)
        XCTAssertTrue(question.option.required)
        XCTAssertEqual(question.option.defaultValue, "")
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
}
