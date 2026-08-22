import UIKit
import UserNotifications

extension Notification.Name {
    static let pushDeviceTokenReady = Notification.Name("pushDeviceTokenReady")
    static let pushNavigationRequested = Notification.Name("pushNavigationRequested")
}

@MainActor
final class PushNotificationCoordinator: NSObject, UIApplicationDelegate, UNUserNotificationCenterDelegate {
    private(set) var registration: (token: String, environment: String)?
    private var pendingNavigationJobID: Int?

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

    func consumePendingNavigationJobID() -> Int? {
        defer { pendingNavigationJobID = nil }
        return pendingNavigationJobID
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

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse
    ) async {
        let userInfo = response.notification.request.content.userInfo
        guard let jobID = Self.jobID(from: userInfo) else { return }
        pendingNavigationJobID = jobID
        NotificationCenter.default.post(
            name: .pushNavigationRequested,
            object: nil,
            userInfo: ["job_id": jobID]
        )
    }

    private static func jobID(from userInfo: [AnyHashable: Any]) -> Int? {
        if let value = userInfo["job_id"] as? Int, value > 0 { return value }
        if let value = userInfo["job_id"] as? NSNumber, value.intValue > 0 {
            return value.intValue
        }
        if let value = userInfo["job_id"] as? String,
           let parsed = Int(value), parsed > 0 {
            return parsed
        }
        return nil
    }
}
