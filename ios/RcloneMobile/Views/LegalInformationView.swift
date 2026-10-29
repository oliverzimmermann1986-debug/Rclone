import SwiftUI

/// Project information remains readable without a server or network.
struct LegalInformationView: View {
    var body: some View {
        List {
            Section("Deine Installation") {
                Text("Wende dich bei Fragen zu deinem Server an dessen Betreiber. Diese Quellcodevorlage enthält keine persönlichen Anbieterangaben.")
                    .textSelection(.enabled)
            }
            Section {
                if let privacy = URL(string: "https://oliverzimmermann1986-debug.github.io/Rclone/datenschutz.html") {
                    Link("Datenschutzerklärung öffnen", destination: privacy)
                }
                if let support = URL(string: "https://github.com/oliverzimmermann1986-debug/Rclone/issues") {
                    Link("Projekt-Feedback öffnen", destination: support)
                }
            } header: {
                Text("Weitere Informationen")
            } footer: {
                Text("Die Projektinformationen sind auch offline lesbar. Die verlinkten Webseiten benötigen eine Internetverbindung. Für den Betrieb deines selbst gehosteten Servers ist dessen Betreiber verantwortlich.")
            }
        }
        .navigationTitle("Projektinformationen")
        .navigationBarTitleDisplayMode(.inline)
    }
}

struct LegalInformationSheet: View {
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            LegalInformationView()
                .toolbar {
                    ToolbarItem(placement: .confirmationAction) {
                        Button("Fertig") { dismiss() }
                    }
                }
        }
    }
}
