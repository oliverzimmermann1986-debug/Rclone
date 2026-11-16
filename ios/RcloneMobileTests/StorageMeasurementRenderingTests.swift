import SwiftUI
import UIKit
import XCTest
@testable import RcloneMobile

/// Only synthetic data and a dedicated window. No AppModel, API client or network.
@MainActor
final class StorageMeasurementRenderingTests: XCTestCase {
    func testLightNarrowPhone() async throws {
        try await capture(style: .light, typeSize: .large, name: "storage-light-standard")
    }

    func testDarkNarrowPhone() async throws {
        try await capture(style: .dark, typeSize: .large, name: "storage-dark-standard")
    }

    func testLightAccessibilityText() async throws {
        try await capture(style: .light, typeSize: .accessibility3, name: "storage-light-accessibility")
    }

    func testDarkAccessibilityText() async throws {
        try await capture(style: .dark, typeSize: .accessibility3, name: "storage-dark-accessibility")
    }

    private func capture(style: UIUserInterfaceStyle, typeSize: DynamicTypeSize, name: String) async throws {
        let scene = try XCTUnwrap(UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first,
            "The iOS host must provide a window scene for rendered UI evidence.")
        let previousKeyWindow = scene.windows.first(where: \.isKeyWindow)
        let window = UIWindow(windowScene: scene)
        window.frame = CGRect(x: 0, y: 0, width: 390, height: 844)
        window.overrideUserInterfaceStyle = style
        let host = UIHostingController(rootView: StorageMeasurementRenderingFixture()
            .environment(\.colorScheme, style == .dark ? .dark : .light)
            .environment(\.dynamicTypeSize, typeSize))
        // Keep a genuinely narrow viewport even when the CI simulator is wider.
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
        host.view.setNeedsLayout()
        host.view.layoutIfNeeded()
        window.layoutIfNeeded()
        attach(view: host.view, name: name + "-top")

        // Accessibility text must remain reachable by scrolling, without shrinking it.
        if typeSize.isAccessibilitySize {
            let scroll = try XCTUnwrap(findScrollView(in: host.view))
            XCTAssertGreaterThan(scroll.contentSize.height, scroll.bounds.height)
            let bottom = max(0, scroll.contentSize.height - scroll.bounds.height + scroll.adjustedContentInset.bottom)
            scroll.setContentOffset(CGPoint(x: 0, y: bottom), animated: false)
            await Task.yield()
            try await Task.sleep(for: .milliseconds(100))
            host.view.layoutIfNeeded()
            attach(view: host.view, name: name + "-bottom")
        }
    }

    private func attach(view: UIView, name: String) {
        XCTAssertEqual(view.bounds.width, 390, accuracy: 1)
        XCTAssertGreaterThan(view.bounds.height, 0)
        let format = UIGraphicsImageRendererFormat()
        format.scale = 2
        let renderer = UIGraphicsImageRenderer(bounds: view.bounds, format: format)
        var rendered = false
        let image = renderer.image { _ in
            rendered = view.drawHierarchy(in: view.bounds, afterScreenUpdates: true)
        }
        XCTAssertTrue(rendered, "The SwiftUI hierarchy must actually render before attaching evidence.")
        guard let png = image.pngData() else {
            XCTFail("Rendered measurement evidence must be exportable as PNG.")
            return
        }
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

private struct StorageMeasurementRenderingFixture: View {
    private let notice = "2026/11/16 08:00:00 NOTICE: Google Drive uses a shared client_id which will stop working in a future release. "
        + String(repeating: "Configure your own Google Drive client_id. ", count: 12)

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                Text("Messhinweise").font(.title2.bold())
                Text("Ausschließlich erfundene Beispieldaten").font(.caption).foregroundStyle(.secondary)
                endpoint(title: "Google-Tresor", path: "demo-tresor:/Fotos", size: PathSize(
                    path: nil, count: nil, bytes: nil,
                    error: notice + "\nERROR : error listing: directory not found",
                    measuredAt: nil, measurementStatus: nil, measurementError: nil))
                Divider()
                endpoint(title: "Google-Archiv", path: "demo-archiv:/Dokumente", size: PathSize(
                    path: nil, count: 18_400, bytes: 32_000_000_000,
                    error: notice + "\nERROR : googleapi: Error 403: userRateLimitExceeded",
                    measuredAt: 1_700_000_000, measurementStatus: "stale", measurementError: nil))
                Divider()
                StorageEndpointMeasurementRow(symbol: "folder.fill", title: "Lokale Quelle", path: "/demo/fotos",
                    size: PathSize(path: nil, count: nil, bytes: nil, error: "No such file or directory",
                        measuredAt: nil, measurementStatus: nil, measurementError: nil),
                    context: .init(isLocal: true, isSource: true))
            }
            .padding(16)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(Color(uiColor: .systemBackground))
    }

    private func endpoint(title: String, path: String, size: PathSize) -> some View {
        StorageEndpointMeasurementRow(symbol: "icloud.fill", title: title, path: path, size: size,
            context: .init(isLocal: false, isSource: false))
    }
}
