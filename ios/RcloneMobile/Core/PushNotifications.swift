import UIKit
import UserNotifications

extension Notification.Name {
    static let pushDeviceTokenReady = Notification.Name("pushDeviceTokenReady")
}

@MainActor
final class PushNotificationCoordinator: NSObject, UIApplicationDelegate, UNUserNotificationCenterDelegate {
    private(set) var registration: (token: String, environment: String)?

    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil
    ) -> Bool {
        UNUserNotificationCenter.current().delegate = self
        return true
    }

    func requestAuthorizationAndRegister() async {
        do {
            let granted = try await UNUserNotificationCenter.current().requestAuthorization(
                options: [.alert, .badge, .sound]
            )
            guard granted else { return }
            UIApplication.shared.registerForRemoteNotifications()
        } catch {
            // The app remains fully usable when the user denies notifications
            // or iOS cannot contact APNs yet.
        }
    }

    func unregisterLocally() {
        UIApplication.shared.unregisterForRemoteNotifications()
        registration = nil
    }

    func application(
        _ application: UIApplication,
        didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data
    ) {
        let token = deviceToken.map { String(format: "%02x", $0) }.joined()
#if DEBUG
        let environment = "sandbox"
#else
        let environment = "production"
#endif
        registration = (token, environment)
        NotificationCenter.default.post(
            name: .pushDeviceTokenReady,
            object: nil,
            userInfo: ["token": token, "environment": environment]
        )
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification
    ) async -> UNNotificationPresentationOptions {
        [.banner, .list, .sound]
    }
}
