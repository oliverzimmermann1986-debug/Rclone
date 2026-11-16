import SwiftUI
import UIKit
import XCTest
@testable import RcloneMobile

/// Render only draft bindings. No API client, OAuth listener, tokens or secret fixture values.
@MainActor
final class GoogleDriveLoginRenderingTests: XCTestCase {
    func testGoogleDriveFormsLight() async throws {
        for mode in GoogleDriveFixtureMode.allCases {
            try await capture(mode: mode, style: .light, typeSize: .large, name: "google-drive-light-\(mode.rawValue)")
        }
    }

    func testGoogleDriveFormsDark() async throws {
        for mode in GoogleDriveFixtureMode.allCases {
            try await capture(mode: mode, style: .dark, typeSize: .large, name: "google-drive-dark-\(mode.rawValue)")
        }
    }

    func testGoogleDriveFormsAccessibility() async throws {
        for mode in GoogleDriveFixtureMode.allCases {
            try await capture(mode: mode, style: .light, typeSize: .accessibility3, name: "google-drive-accessibility-\(mode.rawValue)")
        }
    }

    private func capture(mode: GoogleDriveFixtureMode, style: UIUserInterfaceStyle, typeSize: DynamicTypeSize, name: String) async throws {
        let scene = try XCTUnwrap(UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first)
        let previousKeyWindow = scene.windows.first(where: \.isKeyWindow)
        let window = UIWindow(windowScene: scene)
        window.frame = CGRect(x: 0, y: 0, width: 390, height: 844)
        window.overrideUserInterfaceStyle = style
        let host = UIHostingController(rootView: GoogleDriveFormFixture(mode: mode)
            .environment(\.colorScheme, style == .dark ? .dark : .light)
            .environment(\.dynamicTypeSize, typeSize))
        let container = UIViewController()
        container.addChild(host)
        container.view.addSubview(host.view)
        host.view.frame = CGRect(x: 0, y: 0, width: 390, height: 844)
        host.view.autoresizingMask = []
        host.didMove(toParent: container)
        window.rootViewController = container
        window.makeKeyAndVisible()
        defer {
            window.isHidden = true
            host.willMove(toParent: nil)
            host.view.removeFromSuperview()
            host.removeFromParent()
            window.rootViewController = nil
            previousKeyWindow?.makeKey()
        }
        await Task.yield()
        try await Task.sleep(for: .milliseconds(250))
        host.view.layoutIfNeeded()
        window.layoutIfNeeded()
        attach(view: host.view, name: name + "-top")
        try await revealFinalLink(in: host.view)
        attach(view: host.view, name: name + "-bottom")
    }

    private func revealFinalLink(in view: UIView) async throws {
        if let collection = findCollectionView(in: view) {
            // Form uses self-sizing collection cells; contentSize initially contains estimates.
            XCTAssertGreaterThan(collection.numberOfSections, 0)
            var fullyVisiblePasses = 0
            for _ in 0..<8 {
                collection.layoutIfNeeded()
                let last = try XCTUnwrap(lastItem(in: collection))
                collection.scrollToItem(at: last, at: .bottom, animated: false)
                await Task.yield()
                try await Task.sleep(for: .milliseconds(100))
                view.layoutIfNeeded()
                collection.layoutIfNeeded()
                if let cell = collection.cellForItem(at: last),
                   collection.indexPathsForVisibleItems.contains(last),
                   visibleContent(in: collection).contains(cell.frame) {
                    fullyVisiblePasses += 1
                    if fullyVisiblePasses >= 2 { break }
                } else {
                    fullyVisiblePasses = 0
                }
            }
            let last = try XCTUnwrap(lastItem(in: collection))
            XCTAssertGreaterThan(collection.numberOfItems(inSection: last.section), 0)
            let cell = try XCTUnwrap(collection.cellForItem(at: last), "The final Google-Freigabestatus link row must be materialized.")
            XCTAssertTrue(collection.indexPathsForVisibleItems.contains(last), "The final link row must be visible.")
            XCTAssertTrue(visibleContent(in: collection).contains(cell.frame), "The complete final link row must fit inside the captured viewport.")
            return
        }
        let scroll = try XCTUnwrap(findScrollView(in: view), "The Google form must expose a scrollable viewport.")
        for _ in 0..<4 {
            let bottom = max(-scroll.adjustedContentInset.top,
                scroll.contentSize.height - scroll.bounds.height + scroll.adjustedContentInset.bottom)
            scroll.setContentOffset(CGPoint(x: 0, y: bottom), animated: false)
            await Task.yield()
            try await Task.sleep(for: .milliseconds(100))
            view.layoutIfNeeded()
            scroll.layoutIfNeeded()
        }
        let finalOffset = max(-scroll.adjustedContentInset.top,
            scroll.contentSize.height - scroll.bounds.height + scroll.adjustedContentInset.bottom)
        XCTAssertEqual(scroll.contentOffset.y, finalOffset, accuracy: 1, "The final link must be reachable at the actual form bottom.")
    }

    private func lastItem(in collection: UICollectionView) -> IndexPath? {
        for section in (0..<collection.numberOfSections).reversed() {
            let items = collection.numberOfItems(inSection: section)
            if items > 0 { return IndexPath(item: items - 1, section: section) }
        }
        return nil
    }

    private func visibleContent(in scroll: UIScrollView) -> CGRect {
        scroll.bounds.inset(by: scroll.adjustedContentInset).insetBy(dx: -1, dy: -1)
    }

    private func findCollectionView(in view: UIView) -> UICollectionView? {
        if let collection = view as? UICollectionView { return collection }
        return view.subviews.lazy.compactMap { self.findCollectionView(in: $0) }.first
    }

    private func attach(view: UIView, name: String) {
        XCTAssertEqual(view.bounds.width, 390, accuracy: 1)
        let format = UIGraphicsImageRendererFormat()
        format.scale = 2
        var rendered = false
        let image = UIGraphicsImageRenderer(bounds: view.bounds, format: format).image { _ in
            rendered = view.drawHierarchy(in: view.bounds, afterScreenUpdates: true)
        }
        XCTAssertTrue(rendered)
        guard let png = image.pngData() else { XCTFail("Google Drive form evidence must be PNG"); return }
        let attachment = XCTAttachment(data: png, uniformTypeIdentifier: "public.png")
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }

    private func findScrollView(in view: UIView) -> UIScrollView? {
        if let scroll = view as? UIScrollView { return scroll }
        return view.subviews.lazy.compactMap { self.findScrollView(in: $0) }.first
    }
}

private enum GoogleDriveFixtureMode: String, CaseIterable {
    case newConnection = "new", sharedRenew = "shared-renew", storedOwn = "stored-own", legacy
}

private struct GoogleDriveFormFixture: View {
    let mode: GoogleDriveFixtureMode
    @State private var name: String
    @State private var reconnect: Bool
    @State private var clientId = ""
    @State private var clientSecret = ""
    @State private var useStoredClient: Bool

    init(mode: GoogleDriveFixtureMode) {
        self.mode = mode
        _name = State(initialValue: mode == .storedOwn ? "demo-drive-eigen" : (mode == .sharedRenew ? "demo-drive" : "demo-drive-neu"))
        _reconnect = State(initialValue: mode == .sharedRenew || mode == .storedOwn)
        _useStoredClient = State(initialValue: mode == .storedOwn)
    }

    var body: some View {
        NavigationStack {
            Form {
                GoogleDriveLoginForm(
                    driveRemotes: mode == .legacy ? [] : [
                        ExistingGoogleDriveRemote(name: "demo-drive", hasOwnClient: false),
                        ExistingGoogleDriveRemote(name: "demo-drive-eigen", hasOwnClient: true)
                    ],
                    driveReconnectSupported: mode != .legacy,
                    name: $name, reconnect: $reconnect,
                    clientId: $clientId, clientSecret: $clientSecret, useStoredClient: $useStoredClient
                )
            }
            .navigationTitle("Google Drive anmelden")
            .navigationBarTitleDisplayMode(.inline)
        }
    }
}
