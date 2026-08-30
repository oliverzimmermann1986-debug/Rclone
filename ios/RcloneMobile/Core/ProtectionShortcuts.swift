import AppIntents

struct OpenRecoveryCenterIntent: AppIntent {
    static var title: LocalizedStringResource = "Recovery Center öffnen"
    static var description = IntentDescription("Öffnet Nachweise, Sicherheitsstopps und Wiederherstellungswerkzeuge.")
    static var openAppWhenRun = true

    @MainActor
    func perform() async throws -> some IntentResult & ProvidesDialog {
        NotificationCenter.default.post(name: .pushRecoveryNavigationRequested, object: nil)
        return .result(dialog: "Recovery Center wird geöffnet.")
    }
}

struct ProtectionStatusIntent: AppIntent {
    static var title: LocalizedStringResource = "Schutzstatus anzeigen"
    static var description = IntentDescription("Liest den zuletzt auf diesem Gerät gespeicherten Schutzstatus vor.")

    func perform() async throws -> some IntentResult & ProvidesDialog {
        guard let snapshot = ProtectionWidgetSnapshot.load() else {
            return .result(dialog: "Noch kein Schutzstatus gespeichert. Öffne zuerst das Recovery Center.")
        }
        return .result(
            dialog: "\(snapshot.hostname): Schutzscore \(snapshot.score) von 100, \(snapshot.activePaths) von \(snapshot.totalPaths) Datenwegen aktiv."
        )
    }
}

struct OpenDeviceVaultIntent: AppIntent {
    static var title: LocalizedStringResource = "Geräte-Vault öffnen"
    static var description = IntentDescription("Öffnet den verifizierten Import für Fotos und Dateien.")
    static var openAppWhenRun = true

    @MainActor
    func perform() async throws -> some IntentResult & ProvidesDialog {
        NotificationCenter.default.post(name: .deviceVaultNavigationRequested, object: nil)
        return .result(dialog: "Geräte-Vault wird geöffnet.")
    }
}

struct RcloneProtectionShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(
            intent: OpenRecoveryCenterIntent(),
            phrases: ["Öffne das Recovery Center in \(.applicationName)", "Prüfe meine Sicherung mit \(.applicationName)"],
            shortTitle: "Recovery Center",
            systemImageName: "lifepreserver"
        )
        AppShortcut(
            intent: ProtectionStatusIntent(),
            phrases: ["Wie ist mein Schutzstatus in \(.applicationName)"],
            shortTitle: "Schutzstatus",
            systemImageName: "checkmark.shield"
        )
        AppShortcut(
            intent: OpenDeviceVaultIntent(),
            phrases: ["Öffne den Geräte-Vault in \(.applicationName)", "Sichere eine Datei mit \(.applicationName)"],
            shortTitle: "Geräte-Vault",
            systemImageName: "iphone.and.arrow.forward"
        )
    }
}
