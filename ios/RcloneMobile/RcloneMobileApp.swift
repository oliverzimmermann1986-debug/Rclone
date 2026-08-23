import SwiftUI

@main
struct RcloneMobileApp: App {
    @UIApplicationDelegateAdaptor(PushNotificationCoordinator.self) private var pushDelegate
    @StateObject private var model = AppModel()

    var body: some Scene {
        WindowGroup {
            AppRootView(pushCoordinator: pushDelegate)
                .environmentObject(model)
                .tint(.green)
                .task { await model.restoreSession() }
        }
    }
}
private struct AppRootView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @AppStorage("pushPrimerDecision") private var pushPrimerDecision = "notAsked"
    @State private var showPushPrimer = false
    let pushCoordinator: PushNotificationCoordinator

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
        .animation(reduceMotion ? nil : .smooth(duration: 0.32), value: model.phase)
        .task(id: model.phase) {
            if let jobID = pushCoordinator.consumePendingNavigationJobID() {
                model.requestRunNavigation(id: jobID)
            }
            guard model.phase == .signedIn else {
                if model.phase == .signedOut {
                    pushCoordinator.unregisterLocally()
                }
                return
            }
            if await pushCoordinator.registerIfAlreadyAuthorized() {
                await registerCurrentPushToken()
            } else if pushPrimerDecision == "notAsked" {
                showPushPrimer = true
            }
        }
        .onReceive(NotificationCenter.default.publisher(for: .pushDeviceTokenReady)) { notification in
            guard let token = notification.userInfo?["token"] as? String,
                  let environment = notification.userInfo?["environment"] as? String else { return }
            Task { await model.registerPushDevice(token: token, environment: environment) }
        }
        .onReceive(NotificationCenter.default.publisher(for: .pushNavigationRequested)) { notification in
            guard let jobID = notification.userInfo?["job_id"] as? Int else { return }
            model.requestRunNavigation(id: jobID)
            _ = pushCoordinator.consumePendingNavigationJobID()
        }
        .onReceive(NotificationCenter.default.publisher(for: .pushAuthorizationRequested)) { _ in
            guard model.phase == .signedIn else { return }
            showPushPrimer = true
        }
        .alert("Bei Sicherungsfehlern informieren?", isPresented: $showPushPrimer) {
            Button("Später", role: .cancel) {
                pushPrimerDecision = "later"
            }
            Button("Mitteilungen erlauben") {
                pushPrimerDecision = "accepted"
                Task {
                    let granted = await pushCoordinator.requestAuthorizationAndRegister()
                    if granted {
                        await registerCurrentPushToken()
                    } else {
                        model.actionMessage = "Mitteilungen sind nicht erlaubt. Du kannst sie später in iOS unter Einstellungen → Rclone Sync → Mitteilungen aktivieren."
                    }
                }
            }
        } message: {
            Text("Rclone Sync meldet nur Sicherungs- und Prüfprobleme. Erfolgreiche Läufe erzeugen keine Mitteilung. Die App funktioniert auch ohne Push vollständig.")
        }
    }

    private func registerCurrentPushToken() async {
        guard let registration = pushCoordinator.registration else { return }
        await model.registerPushDevice(
            token: registration.token,
            environment: registration.environment
        )
    }
}

private struct LaunchStatusView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var showRecoveryActions = false

    var body: some View {
        ZStack {
            Color(.systemGroupedBackground).ignoresSafeArea()
            VStack(spacing: 16) {
                Image(systemName: "arrow.triangle.2.circlepath.icloud")
                    .font(.system(size: 42, weight: .semibold))
                    .foregroundStyle(.green)
                    .accessibilityHidden(true)
                ProgressView()
                Text("Verbindung wird geprüft …")
                    .font(.headline)
                if !model.serverAddress.isEmpty {
                    Text(model.serverAddress)
                        .font(.subheadline.monospaced())
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                        .lineLimit(2)
                        .accessibilityLabel("Gespeicherter Server: \(model.serverAddress)")
                }
                if showRecoveryActions {
                    Text("Du kannst weiter warten oder die Verbindungseinstellungen prüfen.")
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                    HStack {
                        Button("Abbrechen", role: .cancel) {
                            model.cancelSessionRestore()
                        }
                        .buttonStyle(.bordered)
                        .accessibilityIdentifier("cancelSessionRestoreButton")
                        Button("Server ändern") {
                            model.changeServerDuringRestore()
                        }
                        .buttonStyle(.borderedProminent)
                        .accessibilityIdentifier("changeServerButton")
                    }
                }
            }
            .frame(maxWidth: 420)
            .padding(24)
        }
        .task {
            if reduceMotion {
                showRecoveryActions = true
                return
            }
            try? await Task.sleep(for: .seconds(2))
            guard !Task.isCancelled else { return }
            withAnimation { showRecoveryActions = true }
        }
    }
}
