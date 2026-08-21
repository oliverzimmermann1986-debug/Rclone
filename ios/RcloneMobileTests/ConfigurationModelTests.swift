import Foundation
import XCTest
@testable import RcloneMobile

final class ConfigurationModelTests: XCTestCase {
    func testEditingBackupPreservesUnmodeledServerSectionsAndPairFields() throws {
        let data = Data(#"""
        {
          "_revision":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
          "schema_version":3,
          "web":{"username":"admin","session_version":7},
          "paths":{"data_dir":"/data","logs_dir":"/logs","temp_dir":"/tmp"},
          "backup":{
            "enabled":true,
            "timezone":"Europe/Berlin",
            "tuning":{"transfers":4},
            "pairs":[{
              "id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","name":"Alt","local":"/mnt/a",
              "remote":"cloud:a","direction":"push","mode":"copy","enabled":true,
              "allow_delete":false,"include":"*.jpg"
            }],
            "jobs":[{
              "id":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","name":"Täglich","enabled":true,
              "data_path_ids":["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"],"schedule":"0 3 * * *",
              "execution_mode":"sequential","max_parallel":1,"retry_minutes":60
            }]
          }
        }
        """#.utf8)
        let snapshot = try JSONDecoder().decode(ConfigSnapshot.self, from: data)
        let original = try XCTUnwrap(snapshot.backup.pairs.first)
        let edited = original.replacing(
            name: "Neu", local: original.local, remote: original.remote,
            direction: original.direction, mode: original.mode, enabled: original.enabled,
            allowDelete: original.allowDelete, maxDelete: original.maxDelete,
            backupDir: original.backupDir, minLocalFiles: original.minLocalFiles,
            minRemoteFiles: original.minRemoteFiles,
            requireMountpoint: original.requireMountpoint,
            mountpoint: original.mountpoint, sentinelFile: original.sentinelFile
        )
        let encoded = try JSONEncoder().encode(
            snapshot.replacing(pairs: [edited], jobs: snapshot.backup.jobs)
        )
        let root = try XCTUnwrap(
            JSONSerialization.jsonObject(with: encoded) as? [String: Any]
        )
        let web = try XCTUnwrap(root["web"] as? [String: Any])
        let backup = try XCTUnwrap(root["backup"] as? [String: Any])
        let tuning = try XCTUnwrap(backup["tuning"] as? [String: Any])
        let pair = try XCTUnwrap((backup["pairs"] as? [[String: Any]])?.first)

        XCTAssertEqual((root["schema_version"] as? NSNumber)?.intValue, 3)
        XCTAssertEqual((web["session_version"] as? NSNumber)?.intValue, 7)
        XCTAssertEqual((tuning["transfers"] as? NSNumber)?.intValue, 4)
        XCTAssertEqual(pair["name"] as? String, "Neu")
        XCTAssertEqual(pair["include"] as? String, "*.jpg")
        XCTAssertEqual(root["_revision"] as? String, String(repeating: "a", count: 64))
    }

    func testJobDefinitionRoundTripsCanonicalOrderingFields() throws {
        let definition = JobDefinition(
            id: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            name: "Parallel",
            enabled: true,
            dataPathIDs: ["first", "second"],
            schedule: "manual",
            executionMode: "parallel",
            maxParallel: 2,
            retryMinutes: 30
        )
        let decoded = try JSONDecoder().decode(
            JobDefinition.self,
            from: JSONEncoder().encode(definition)
        )

        XCTAssertEqual(decoded.dataPathIDs, ["first", "second"])
        XCTAssertEqual(decoded.executionMode, "parallel")
        XCTAssertEqual(decoded.maxParallel, 2)
        XCTAssertEqual(decoded.retryMinutes, 30)
    }

    func testWebhookEditPreservesUnknownNotificationAndHookFields() throws {
        let data = Data(#"""
        {
          "_revision":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
          "backup":{"pairs":[],"jobs":[]},
          "notifications":{
            "timeout_seconds":9,
            "webhooks":[{
              "id":"webhook01","enabled":true,"type":"generic",
              "url":"https://example.invalid/hook","events":["sync_error"],
              "custom_header":"preserve-me"
            }]
          }
        }
        """#.utf8)
        let snapshot = try JSONDecoder().decode(ConfigSnapshot.self, from: data)
        let original = try XCTUnwrap(snapshot.webhooks.first)
        let edited = original.replacing(
            enabled: false,
            type: original.type,
            url: original.url,
            events: original.events
        )
        let encoded = try JSONEncoder().encode(snapshot.replacingWebhooks([edited]))
        let root = try XCTUnwrap(JSONSerialization.jsonObject(with: encoded) as? [String: Any])
        let notifications = try XCTUnwrap(root["notifications"] as? [String: Any])
        let hook = try XCTUnwrap((notifications["webhooks"] as? [[String: Any]])?.first)

        XCTAssertEqual((notifications["timeout_seconds"] as? NSNumber)?.intValue, 9)
        XCTAssertEqual(hook["enabled"] as? Bool, false)
        XCTAssertEqual(hook["custom_header"] as? String, "preserve-me")
    }
}
