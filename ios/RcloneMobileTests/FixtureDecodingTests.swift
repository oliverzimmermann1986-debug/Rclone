import XCTest
@testable import RcloneMobile

final class FixtureDecodingTests: XCTestCase {
    func testOverviewDecodesBackendShape() throws {
        let data = Data(#"""
        {
          "app":{"version":"1.7.1","timezone":"Europe/Berlin"},
          "system":{
            "hostname":"backup","platform":"Linux","kernel":"6.8","python":"3.12",
            "virtualization":"lxc","addresses":["10.0.0.4"],"uptime_seconds":86400,
            "cpu":{"count":2,"capacity":2.0,"source":"cgroup-v2","load_1":0.2,"load_5":0.1,"load_15":0.1,"load_percent":10.0},
            "memory":{"total_bytes":1073741824,"available_bytes":536870912,"used_bytes":536870912,"percent_used":50.0,"source":"cgroup-v2"},
            "pids":{"current":42,"max":512,"percent_used":8.2,"source":"cgroup-v2"},
            "data_disk":{"path":"/data","total_bytes":1000,"used_bytes":250,"free_bytes":750,"percent_used":25.0}
          },
          "services":{
            "web":{"enabled":"enabled","active":"active"},
            "scheduler":{"enabled":"enabled","active":"active","configured_enabled":true,"control":{"paused":false}}
          },
          "pairs":{"total":1,"enabled":1,"scheduled":1,"manual":0,"destructive":0,"health":[]},
          "jobs":{"last":null,"last_success":null,"last_error":null,"stats_24h":{}},
          "alerts":[],"generated_at":1720000000
        }
        """#.utf8)

        let overview = try JSONDecoder().decode(OverviewResponse.self, from: data)
        XCTAssertEqual(overview.app.version, "1.7.1")
        XCTAssertEqual(overview.system.hostname, "backup")
        XCTAssertEqual(overview.pairs.enabled, 1)
    }

    func testStorageDecodesCopyCountsAndSizes() throws {
        let data = Data(#"""
        {"pairs":[{"name":"Fotos","local":"/mnt/fotos","remote":"cloud:Fotos","direction":"push","source":"/mnt/fotos","target":"cloud:Fotos","source_size":{"path":"/mnt/fotos","count":12,"bytes":2048},"target_size":{"path":"cloud:Fotos","count":9,"bytes":1024}}]}
        """#.utf8)

        let storage = try JSONDecoder().decode(StorageOverview.self, from: data)
        XCTAssertEqual(storage.pairs.first?.sourceSize?.count, 12)
        XCTAssertEqual(storage.pairs.first?.targetSize?.bytes, 1024)
    }

    func testServerURLAddsHTTPS() throws {
        let url = try APIClient.normalizedServerURL("backup.example.de")
        XCTAssertEqual(url.absoluteString, "https://backup.example.de")
    }

    func testLocalIPv4UsesHTTPAndBackendPort() throws {
        let url = try APIClient.normalizedServerURL("192.168.1.97")
        XCTAssertEqual(url.absoluteString, "http://192.168.1.97:8001")
    }

    func testLocalIPv4KeepsExplicitPort() throws {
        let url = try APIClient.normalizedServerURL("192.168.1.97:9000")
        XCTAssertEqual(url.absoluteString, "http://192.168.1.97:9000")
    }

    func testExplicitLocalHTTPAddsBackendPort() throws {
        let url = try APIClient.normalizedServerURL("http://192.168.1.97")
        XCTAssertEqual(url.absoluteString, "http://192.168.1.97:8001")
    }

    func testExplicitHTTPSIsNotRewritten() throws {
        let url = try APIClient.normalizedServerURL("https://192.168.1.97")
        XCTAssertEqual(url.absoluteString, "https://192.168.1.97")
    }
}
