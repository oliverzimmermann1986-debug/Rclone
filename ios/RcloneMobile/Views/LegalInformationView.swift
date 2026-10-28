import SwiftUI

/// Provider details are bundled so they remain readable without a server or network.
struct LegalInformationView: View {
    var body: some View {
        List {
            Section("Anbieter von Sicherpfad") {
                Text("Sicherpfad Contributors\n[Anbieterangaben entfernt]\n[Anschrift entfernt]\n[Anschrift entfernt]\nDeutschland")
                    .textSelection(.enabled)
                if let email = URL(string: "mailto:contact@example.invalid") {
                    Link("contact@example.invalid", destination: email)
                }
            }
            Section {
                if let privacy = URL(string: "https://oliverzimmermann1986-debug.github.io/Rclone/datenschutz.html") {
                    Link("Datenschutzerklärung öffnen", destination: privacy)
                }
                if let support = URL(string: "https://github.com/oliverzimmermann1986-debug/Rclone/issues") {
                    Link("Supportformular öffnen", destination: support)
                }
            } header: {
                Text("Weitere Informationen")
            } footer: {
                Text("Die Anbieterangaben sind auch offline lesbar. Die verlinkten Webseiten benötigen eine Internetverbindung. Für den Betrieb deines selbst gehosteten Servers ist dessen Betreiber verantwortlich.")
            }
        }
        .navigationTitle("Impressum")
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
