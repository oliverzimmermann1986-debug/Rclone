import UIKit
import UserNotifications

extension Notification.Name {
    static let pushDeviceTokenReady = Notification.Name("pushDeviceTokenReady")
    static let pushNavigationRequested = Notification.Name("pushNavigationRequested")
    static let pushRecoveryNavigationRequested = Notification.Name("pushRecoveryNavigationRequested")
    static let deviceVaultNavigationRequested = Notification.Name("deviceVaultNavigationRequested")
    static let pushPauseRequested = Notification.Name("pushPauseRequested")
    static let pushAuthorizationRequested = Notification.Name("pushAuthorizationRequested")
}

@MainActor
final class PushNotificationCoordinator: NSObject, UIApplicationDelegate, UNUserNotificationCenterDelegate {
    private(set) var registration: (token: String, environment: String)?
    private var pendingNavigationJobID: Int?
    private var pendingRecoveryNavigation = false

    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil
    ) -> Bool {
        UNUserNotificationCenter.current().delegate = self
        let openIncident = UNNotificationAction(
            identifier: "OPEN_INCIDENT",
            title: "Vorfall prüfen",
            options: [.foreground]
        )
        let pauseSchedules = UNNotificationAction(
            identifier: "PAUSE_SCHEDULES",
            title: "Zeitpläne 1 Stunde pausieren",
            options: [.foreground, .authenticationRequired]
        )
        let incidentCategory = UNNotificationCategory(
            identifier: "RCLONE_INCIDENT",
            actions: [openIncident, pauseSchedules],
            intentIdentifiers: [],
            options: []
        )
        UNUserNotificationCenter.current().setNotificationCategories([incidentCategory])
        return true
    }

    func requestAuthorizationAndRegister() async -> Bool {
        do {
            let granted = try await UNUserNotificationCenter.current().requestAuthorization(
                options: [.alert, .badge, .sound]
            )
            guard granted else { return false }
            UIApplication.shared.registerForRemoteNotifications()
            return true
        } catch {
            // The app remains fully usable when the user denies notifications
            // or iOS cannot contact APNs yet.
            return false
        }
    }

    func registerIfAlreadyAuthorized() async -> Bool {
        let settings = await UNUserNotificationCenter.current().notificationSettings()
        switch settings.authorizationStatus {
        case .authorized, .provisional, .ephemeral:
            UIApplication.shared.registerForRemoteNotifications()
            return true
        case .notDetermined, .denied:
            return false
        @unknown default:
            return false
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

    func consumePendingRecoveryNavigation() -> Bool {
        defer { pendingRecoveryNavigation = false }
        return pendingRecoveryNavigation
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
        if response.actionIdentifier == "PAUSE_SCHEDULES" {
            NotificationCenter.default.post(name: .pushPauseRequested, object: nil)
            return
        }
        let event = userInfo["event"] as? String ?? ""
        if ["anomaly_blocked", "recovery_error", "restore_test_error"].contains(event) {
            pendingRecoveryNavigation = true
            NotificationCenter.default.post(name: .pushRecoveryNavigationRequested, object: nil)
        }
        if let jobID = Self.jobID(from: userInfo) {
            pendingNavigationJobID = jobID
            NotificationCenter.default.post(
                name: .pushNavigationRequested,
                object: nil,
                userInfo: ["job_id": jobID]
            )
        }
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
