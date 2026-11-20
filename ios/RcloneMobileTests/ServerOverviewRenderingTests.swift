import SwiftUI
import UIKit
import XCTest
@testable import RcloneMobile

@MainActor
final class ServerOverviewRenderingTests: XCTestCase {
    func testLightPhone() async throws { try await capture(style: .light, size: .large) }
    func testDarkPhone() async throws { try await capture(style: .dark, size: .large) }
    func testAccessibilityText() async throws { try await capture(style: .light, size: .accessibility3) }

    private func capture(style: UIUserInterfaceStyle, size: DynamicTypeSize) async throws {
        let scene = try XCTUnwrap(UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first)
        let previous = scene.windows.first(where: \.isKeyWindow)
        let fixture = try StorePreviewData.load(bundle: Bundle(for: Self.self))
        let now = Date()
        let snapshot = ServerOverviewSnapshot(overview: fixture.overview, storage: fixture.storage,
            progress: fixture.progress, pbs: fixture.pbs, lockdown: nil, receivedAt: now, missing: ["Laufstatus"])
        let rows = [
            ServerOverviewEntry(profile: SavedServerProfile(name: "NAS zuhause", address: "https://nas.example/sicherpfad",
                username: "admin", lastUsedAt: 0), state: .loaded, snapshot: snapshot),
            ServerOverviewEntry(profile: SavedServerProfile(name: "Externe Sicherung", address: "https://extern.example",
                username: "admin", lastUsedAt: 0), state: .unavailable("Keine Verbindung"), snapshot: snapshot),
            ServerOverviewEntry(profile: SavedServerProfile(name: "Weitere Sicherung", address: "https://weiter.example",
                username: "anderes-konto", lastUsedAt: 0), state: .needsLogin)
        ]
        let view = NavigationStack {
            List(rows) { entry in ServerOverviewRow(entry: entry, now: now) }
                .navigationTitle("Alle Server")
        }
        .environment(\.colorScheme, style == .dark ? .dark : .light)
        .environment(\.dynamicTypeSize, size)
        let host = UIHostingController(rootView: view)
        let container = UIViewController()
        container.addChild(host)
        container.view.addSubview(host.view)
        host.view.frame = CGRect(x: 0, y: 0, width: 390, height: 844)
        host.view.autoresizingMask = []
        host.didMove(toParent: container)
        let window = UIWindow(windowScene: scene)
        window.frame = host.view.frame
        window.overrideUserInterfaceStyle = style
        window.rootViewController = container
        window.makeKeyAndVisible()
        defer {
            window.isHidden = true
            host.willMove(toParent: nil)
            host.view.removeFromSuperview()
            host.removeFromParent()
            window.rootViewController = nil
            previous?.makeKey()
        }
        await Task.yield()
        try await Task.sleep(for: .milliseconds(250))
        host.view.layoutIfNeeded()
        window.layoutIfNeeded()
        XCTAssertEqual(host.view.bounds.width, 390, accuracy: 1)
        let format = UIGraphicsImageRendererFormat()
        format.scale = 2
        let image = UIGraphicsImageRenderer(bounds: host.view.bounds, format: format).image { _ in
            XCTAssertTrue(host.view.drawHierarchy(in: host.view.bounds, afterScreenUpdates: true))
        }
        let attachment = XCTAttachment(image: image)
        attachment.name = "all-servers-\(style.rawValue)-\(size)"
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
