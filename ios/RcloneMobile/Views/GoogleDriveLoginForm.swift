import SwiftUI

/// UI only: draft bindings, no API client, tokens, network calls or Google password field.
struct GoogleDriveLoginForm: View {
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    let driveRemotes: [ExistingGoogleDriveRemote]
    let driveReconnectSupported: Bool
    @Binding var name: String
    @Binding var reconnect: Bool
    @Binding var clientId: String
    @Binding var clientSecret: String
    @Binding var useStoredClient: Bool

    init(driveRemotes: [ExistingGoogleDriveRemote], driveReconnectSupported: Bool,
         name: Binding<String>, reconnect: Binding<Bool>, clientId: Binding<String>,
         clientSecret: Binding<String>, useStoredClient: Binding<Bool>) {
        self.driveRemotes = driveRemotes
        self.driveReconnectSupported = driveReconnectSupported
        _name = name
        _reconnect = reconnect
        _clientId = clientId
        _clientSecret = clientSecret
        _useStoredClient = useStoredClient
    }

    private var existing: ExistingGoogleDriveRemote? {
        reconnect ? driveRemotes.first(where: { $0.name == name }) : nil
    }

    private var connectionChoice: Binding<String> {
        Binding(get: { reconnect ? name : "" }, set: { choice in
            reconnect = !choice.isEmpty
            name = choice.isEmpty ? "gdrive-eigen" : choice
            clientId = ""
            clientSecret = ""
            useStoredClient = driveRemotes.first(where: { $0.name == choice })?.hasOwnClient == true
        })
    }

    var body: some View {
        Section("Google Drive auswählen") {
            if driveReconnectSupported, !driveRemotes.isEmpty {
                if dynamicTypeSize.isAccessibilitySize {
                    connectionOption("Neue Verbindung", value: "")
                    ForEach(driveRemotes) { remote in
                        connectionOption("Erneuern: \(remote.name)", value: remote.name)
                    }
                } else {
                    Picker("Verbindung", selection: connectionChoice) {
                        Text("Neue Verbindung").tag("")
                        ForEach(driveRemotes) { remote in
                            Text("Erneuern: \(remote.name)").tag(remote.name)
                        }
                    }
                    .pickerStyle(.menu)
                    .frame(minHeight: 44)
                }
            }
            if reconnect {
                Text("Verwende dasselbe Google-Konto. Datenwege, Tresore und vorhandene Ordnerzuordnungen bleiben erhalten.")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            } else {
                TextField("Name der neuen Verbindung", text: $name)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .frame(minHeight: 44)
                    .accessibilityIdentifier("googleDriveRemoteName")
                Text("2–64 Buchstaben, Ziffern, Bindestriche oder Unterstriche. Verwende einen noch freien Namen.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            if !driveReconnectSupported {
                Text("Dieser Server unterstützt hier nur neue Google-Verbindungen. Zum Erneuern einer vorhandenen Verbindung ist ein Backend-Update nötig.")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }
        }

        Section("Google-App-Anbindung") {
            if existing?.hasOwnClient == true {
                Toggle("Gespeicherte API-Anbindung verwenden", isOn: $useStoredClient)
                    .frame(minHeight: 44)
                if useStoredClient {
                    Text("Die eigene Google-App-Anbindung auf dem Server bleibt erhalten. Du meldest nur das Google-Konto neu an.")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
            }
            if existing?.hasOwnClient != true || !useStoredClient {
                Text("Einmalig in Google Cloud ein Projekt mit aktivierter Drive-API und einem OAuth-Client vom Typ „Desktop-App“ vorbereiten. Eine normale Google-Anmeldung erstellt diese App-Anbindung nicht.")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                Text("Bei externen OAuth-Apps im Status „Testing“ läuft die Freigabe nach sieben Tagen ab. Für dauerhafte Sicherungen den Veröffentlichungsstatus in Google Cloud prüfen.")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                TextField("Google Client-ID", text: $clientId, axis: .vertical)
                    .keyboardType(.asciiCapable)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .frame(minHeight: 44)
                    .accessibilityIdentifier("googleDriveClientId")
                SecureField("Google Client-Secret", text: $clientSecret)
                    .textContentType(nil)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .frame(minHeight: 44)
                    .accessibilityIdentifier("googleDriveClientSecret")
                Text("Beide Angaben gehören zur Google-App. Dein Google-Passwort gibst du ausschließlich im offiziellen Google-Fenster ein.")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }
            Link(destination: URL(string: "https://console.cloud.google.com/apis/credentials")!) {
                Label("Google Cloud öffnen", systemImage: "arrow.up.right.square")
                    .frame(minHeight: 44, alignment: .leading)
            }
            Link(destination: URL(string: "https://rclone.org/drive/#making-your-own-client-id")!) {
                Label("Anleitung für die eigene Google-App", systemImage: "book")
                    .frame(minHeight: 44, alignment: .leading)
            }
            Link(destination: URL(string: "https://developers.google.com/identity/protocols/oauth2#expiration")!) {
                Label("Google-Freigabestatus verstehen", systemImage: "info.circle")
                    .frame(minHeight: 44, alignment: .leading)
            }
            .accessibilityIdentifier("googleDriveOAuthExpirationLink")
        }
    }

    private func connectionOption(_ title: String, value: String) -> some View {
        let selected = connectionChoice.wrappedValue == value
        return Button { connectionChoice.wrappedValue = value } label: {
            HStack(alignment: .top, spacing: 12) {
                Image(systemName: selected ? "checkmark.circle.fill" : "circle")
                    .foregroundStyle(selected ? Color.accentColor : Color.secondary)
                    .fixedSize()
                    .accessibilityHidden(true)
                Text(title)
                    .multilineTextAlignment(.leading)
                    .lineLimit(nil)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .font(.body)
            .frame(minHeight: 44, alignment: .leading)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityValue(selected ? "Ausgewählt" : "Nicht ausgewählt")
    }
}
