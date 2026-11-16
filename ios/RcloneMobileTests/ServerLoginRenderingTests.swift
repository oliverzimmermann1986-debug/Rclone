import SwiftUI
import UIKit
import XCTest
@testable import RcloneMobile

@MainActor
final class ServerLoginRenderingTests: XCTestCase {
    func testAddressStepLight() async throws {
        try await captureAddress(style: .light, typeSize: .large, name: "login-address-light")
    }

    func testAddressStepDark() async throws {
        try await captureAddress(style: .dark, typeSize: .large, name: "login-address-dark")
    }

    func testAddressStepAccessibility() async throws {
        try await captureAddress(style: .light, typeSize: .accessibility3, name: "login-address-accessibility")
    }

    func testTargetCardsDark() async throws {
        try await capture(ServerLoginCardFixture(), style: .dark, typeSize: .large, name: "login-target-dark")
    }

    func testTargetCardsAccessibility() async throws {
        try await capture(ServerLoginCardFixture(), style: .light, typeSize: .accessibility3, name: "login-target-accessibility")
    }

    private func captureAddress(style: UIUserInterfaceStyle, typeSize: DynamicTypeSize, name: String) async throws {
        let suite = "ServerLoginRenderingTests-\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        let address = "https://demo.tail0000.ts.net/proxy/login"
        defaults.set(address, forKey: "serverAddress")
        defaults.set("admin", forKey: "username")
        defaults.set(try JSONEncoder().encode([
            SavedServerProfile(name: "Beispielserver", address: address, username: "admin", lastUsedAt: 1_700_000_000)
        ]), forKey: "savedServerProfiles")
        let model = AppModel(defaults: defaults) { _ in
            preconditionFailure("Login rendering must never create an API client")
        }
        model.errorMessage = "Beispielhinweis: Die vorherige Sitzung konnte nicht wiederhergestellt werden. Bitte erneut anmelden."
        // This is the actual first step, including onAppear; no restoreSession task is invoked.
        try await capture(LoginView().environmentObject(model), style: style, typeSize: typeSize, name: name)
    }

    private func capture<Content: View>(_ content: Content, style: UIUserInterfaceStyle, typeSize: DynamicTypeSize, name: String) async throws {
        let scene = try XCTUnwrap(UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first)
        let previousKeyWindow = scene.windows.first(where: \.isKeyWindow)
        let window = UIWindow(windowScene: scene)
        window.frame = CGRect(x: 0, y: 0, width: 390, height: 844)
        window.overrideUserInterfaceStyle = style
        let host = UIHostingController(rootView: content
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
        if let scroll = findScrollView(in: host.view), scroll.contentSize.height > scroll.bounds.height {
            let bottom = scroll.contentSize.height - scroll.bounds.height + scroll.adjustedContentInset.bottom
            scroll.setContentOffset(CGPoint(x: 0, y: bottom), animated: false)
            await Task.yield()
            try await Task.sleep(for: .milliseconds(100))
            host.view.layoutIfNeeded()
            attach(view: host.view, name: name + "-bottom")
        }
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
        guard let png = image.pngData() else { XCTFail("Login evidence must be PNG"); return }
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

private struct ServerLoginCardFixture: View {
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                Text("Bei Sicherpfad anmelden").font(.title2.bold())
                Text("Erfundene Serveradressen · keine Verbindung wird aufgebaut")
                    .font(.caption).foregroundStyle(.secondary)
                if let target = try? ServerLoginTarget(address: "https://demo.tail0000.ts.net:8443/sicherungen/proxy/privater-bereich/login") {
                    ServerLoginTargetCard(target: target)
                }
                if let target = try? ServerLoginTarget(address: "http://192.0.2.10:8001/demo/login") {
                    ServerLoginTargetCard(target: target)
                }
            }
            .padding(24)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(Color(uiColor: .systemGroupedBackground))
    }
}
