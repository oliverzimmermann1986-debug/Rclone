import SwiftUI

@main
struct RcloneMobileApp: App {
    @StateObject private var model = AppModel()

    var body: some Scene {
        WindowGroup {
            AppRootView()
                .environmentObject(model)
                .tint(.green)
                .task { await model.restoreSession() }
        }
    }
}
private struct AppRootView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        Group {
            switch model.phase {
            case .checking:
                LaunchStatusView()
            case .signedOut:
                LoginView()
            case .signedIn:
                RootTabView()
            }
        }
        .animation(.smooth(duration: 0.32), value: model.phase)
    }
}

private struct LaunchStatusView: View {
    var body: some View {
        ZStack {
            Color(.systemGroupedBackground).ignoresSafeArea()
            VStack(spacing: 18) {
                Image(systemName: "arrow.triangle.2.circlepath.icloud")
                    .font(.system(size: 42, weight: .semibold))
                    .foregroundStyle(.green)
                    .accessibilityHidden(true)
                ProgressView("Verbindung wird geprüft …")
            }
        }
    }
}
