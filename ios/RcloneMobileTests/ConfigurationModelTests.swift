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
          "notifications":{
            "apns":{"enabled":true,"topic":"de.oliverzimmermann.rclonesync"},
            "delivery_policy":{"future_field":"preserved"}
          },
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
        let notifications = try XCTUnwrap(root["notifications"] as? [String: Any])
        let apns = try XCTUnwrap(notifications["apns"] as? [String: Any])
        let backup = try XCTUnwrap(root["backup"] as? [String: Any])
        let tuning = try XCTUnwrap(backup["tuning"] as? [String: Any])
        let pair = try XCTUnwrap((backup["pairs"] as? [[String: Any]])?.first)

        XCTAssertEqual((root["schema_version"] as? NSNumber)?.intValue, 3)
        XCTAssertEqual((web["session_version"] as? NSNumber)?.intValue, 7)
        XCTAssertEqual(apns["enabled"] as? Bool, true)
        XCTAssertEqual(
            (notifications["delivery_policy"] as? [String: Any])?["future_field"] as? String,
            "preserved"
        )
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

    func testLegacyMinimalJobDefinitionUsesServerDefaults() throws {
        let data = Data(#"{"name":"Altbestand","schedule":"0 4 * * *"}"#.utf8)

        let decoded = try JSONDecoder().decode(JobDefinition.self, from: data)

        XCTAssertEqual(decoded.id, "")
        XCTAssertEqual(decoded.name, "Altbestand")
        XCTAssertTrue(decoded.enabled)
        XCTAssertEqual(decoded.dataPathIDs, [])
        XCTAssertEqual(decoded.schedule, "0 4 * * *")
        XCTAssertEqual(decoded.executionMode, "sequential")
        XCTAssertEqual(decoded.maxParallel, 1)
        XCTAssertEqual(decoded.retryMinutes, 60)
    }

    func testConfigSnapshotWithLegacyMinimalJobStillDecodes() throws {
        let data = Data(#"""
        {
          "_revision":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
          "backup":{"pairs":[],"jobs":[{"name":"Altbestand","schedule":"manual"}]}
        }
        """#.utf8)

        let snapshot = try JSONDecoder().decode(ConfigSnapshot.self, from: data)

        XCTAssertEqual(snapshot.backup.jobs.first?.name, "Altbestand")
        XCTAssertEqual(snapshot.backup.jobs.first?.executionMode, "sequential")
    }

    func testLegacyPairScheduleBecomesVisibleCanonicalJobWhenJobsKeyIsAbsent() throws {
        let data = Data(#"""
        {
          "_revision":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
          "backup":{
            "default_schedule":"0 3 * * *","scheduler_retry_minutes":30,
            "pairs":[{
              "name":"Fotos","local":"/srv/fotos","remote":"cloud:fotos",
              "direction":"push","mode":"copy","enabled":true,"schedule":"15 4 * * *"
            }]
          }
        }
        """#.utf8)

        let snapshot = try JSONDecoder().decode(ConfigSnapshot.self, from: data)
        let pair = try XCTUnwrap(snapshot.backup.pairs.first)
        let job = try XCTUnwrap(snapshot.backup.jobs.first)

        XCTAssertEqual(pair.id, "abe37acc76a750729c2d1f31a6a22dd7")
        XCTAssertEqual(job.id, "cee145c79935563a9e9ddcc8f9fb423f")
        XCTAssertEqual(job.name, "Fotos")
        XCTAssertEqual(job.dataPathIDs, [pair.id])
        XCTAssertEqual(job.schedule, "15 4 * * *")
        XCTAssertEqual(job.retryMinutes, 30)
    }

    func testExplicitEmptyJobsRemainsEmptyInsteadOfRecreatingLegacyJobs() throws {
        let data = Data(#"""
        {
          "_revision":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
          "backup":{
            "default_schedule":"0 3 * * *",
            "pairs":[{"name":"Fotos","local":"/srv/fotos","remote":"cloud:fotos","schedule":"15 4 * * *"}],
            "jobs":[]
          }
        }
        """#.utf8)

        let snapshot = try JSONDecoder().decode(ConfigSnapshot.self, from: data)

        XCTAssertTrue(snapshot.backup.jobs.isEmpty)
    }

    func testPBSEditPreservesSecretPlaceholderAndUnknownFields() throws {
        let data = Data(#"""
        {
          "_revision":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
          "backup":{"pairs":[],"jobs":[]},
          "pbs":{
            "enabled":true,"repository":"token@pbs@host:store","namespace":"rclone",
            "backup_id":"main","fingerprint":"","password":"***SET***","timeout_hours":4,
            "keep":{"keep_last":3,"keep_daily":7,"keep_weekly":4,"keep_monthly":12,"keep_yearly":3},
            "custom_server_flag":"preserve-server",
            "targets":[{
              "id":"11111111111111111111111111111111","name":"Daten","paths":["/srv/data"],
              "schedule":"0 4 * * *","namespace":"","backup_id":"data","require_mountpoint":true,
              "mountpoint":"/srv","sentinel_file":".mounted","min_files":1,
              "custom_target_flag":"preserve-target"
            }]
          }
        }
        """#.utf8)
        let snapshot = try JSONDecoder().decode(ConfigSnapshot.self, from: data)
        var pbs = snapshot.pbsConfiguration
        pbs.enabled = false
        pbs.targets[0].schedule = "manual"

        let encoded = try JSONEncoder().encode(snapshot.replacingPBSConfiguration(pbs))
        let root = try XCTUnwrap(JSONSerialization.jsonObject(with: encoded) as? [String: Any])
        let section = try XCTUnwrap(root["pbs"] as? [String: Any])
        let target = try XCTUnwrap((section["targets"] as? [[String: Any]])?.first)

        XCTAssertEqual(section["password"] as? String, "***SET***")
        XCTAssertEqual(section["custom_server_flag"] as? String, "preserve-server")
        XCTAssertEqual(target["custom_target_flag"] as? String, "preserve-target")
        XCTAssertEqual(target["schedule"] as? String, "manual")
    }
}

@MainActor
final class ConfigurationDraftStoreTests: XCTestCase {
    func testUnsavedSecondPairIsImmediatelyAvailableToJobDraft() {
        let photos = pair(id: "photos", name: "Fotos")
        let recipes = pair(id: "recipes", name: "Rezepte")
        let store = ConfigurationDraftStore()
        store.load(from: snapshot(revision: "revision-1", pairs: [photos]))

        store.upsertPair(recipes, at: nil)
        store.upsertDefinition(
            JobDefinition(id: "daily", name: "Täglich", dataPathIDs: [recipes.id]),
            at: nil
        )

        XCTAssertEqual(store.pairs.map(\.name), ["Fotos", "Rezepte"])
        XCTAssertEqual(store.definitions.first?.dataPathIDs, [recipes.id])
        XCTAssertEqual(store.baseRevision, "revision-1")
        XCTAssertTrue(store.isDirty)
    }

    func testDirtyDraftSurvivesBackgroundReloadUntilExplicitDiscard() {
        let photos = pair(id: "photos", name: "Fotos")
        let recipes = pair(id: "recipes", name: "Rezepte")
        let store = ConfigurationDraftStore()
        store.load(from: snapshot(revision: "revision-1", pairs: [photos]))
        store.upsertPair(recipes, at: nil)

        store.load(from: snapshot(revision: "revision-2", pairs: [photos]))
        XCTAssertEqual(store.pairs.map(\.name), ["Fotos", "Rezepte"])
        XCTAssertEqual(store.baseRevision, "revision-1")

        store.load(from: snapshot(revision: "revision-2", pairs: [photos]), force: true)
        XCTAssertEqual(store.pairs.map(\.name), ["Fotos"])
        XCTAssertEqual(store.baseRevision, "revision-2")
        XCTAssertFalse(store.isDirty)
    }

    private func pair(id: String, name: String) -> PairConfig {
        PairConfig(
            stableID: id,
            name: name,
            local: "/mnt/\(name.lowercased())",
            remote: "cloud:\(name)"
        )
    }

    private func snapshot(
        revision: String,
        pairs: [PairConfig],
        jobs: [JobDefinition] = []
    ) -> ConfigSnapshot {
        ConfigSnapshot(
            revision: revision,
            backup: BackupConfig(
                enabled: true,
                timezone: "Europe/Berlin",
                defaultSchedule: nil,
                pairs: pairs,
                jobs: jobs
            )
        )
    }
}
