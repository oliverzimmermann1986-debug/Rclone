import SwiftUI

/// Available before login; never asks the network or treats this as a live score.
struct OfflineRecoveryCardView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss
    var body: some View {
        NavigationStack {
            List {
                Section {
                    Label("Gespeicherte Notfallkarten – nicht live", systemImage: "wifi.slash")
                        .foregroundStyle(.orange)
                    Text("Sicherungen nicht überschreiben. Auf einem Ersatzserver den eigenen Cloud-Zugang einrichten, danach unter Wiederherstellen die verschlüsselte Notfallakte öffnen und eine Datei getrennt zurückholen.")
                }
                ForEach(model.savedServerProfiles) { profile in
                    if let pass = RecoveryOfflineStore().load(server: profile.address, username: profile.username) {
                        Section(profile.name) {
                            LabeledContent("Konto", value: profile.username)
                            LabeledContent("Server", value: profile.address)
                            LabeledContent("Gespeicherter Stand", value: AppFormat.date(pass.generatedAt))
                            ForEach(pass.dataPaths) { path in
                                VStack(alignment: .leading, spacing: 4) {
                                    Text(path.name).font(.headline)
                                    Text("Letzte gespeicherte Prüfung: \(AppFormat.date(path.restore.lastAttemptAt))")
                                        .font(.caption).foregroundStyle(.secondary)
                                }
                            }
                        }
                    }
                }
                Section {
                    Text("Ohne zuvor geladenen Recovery-Pass liegt keine Karte vor. Die Notfallakte enthält ein Vault-Inventar, keine Dateiinhalte oder Zugangsdaten. Passphrase getrennt aufbewahren.")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            .navigationTitle("Offline-Notfallkarte")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Fertig") { dismiss() } } }
        }
    }
}
