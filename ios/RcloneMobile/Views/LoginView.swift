import SwiftUI

private enum LoginFieldID: Hashable { case server, username, password }

struct LoginView: View {
    @EnvironmentObject private var model: AppModel
    @State private var server = ""
    @State private var username = "admin"
    @State private var password = ""
    @State private var loginTask: Task<Void, Never>?
    @State private var showHTTPWarning = false
    @FocusState private var focusedField: LoginFieldID?

    var body: some View {
        ZStack {
            Color(.systemGroupedBackground)
                .ignoresSafeArea()

            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    brand
                        .padding(.bottom, 30)

                    Text("Mit Server verbinden")
                        .font(.title2.weight(.bold))

                    Text("Adresse und Zugangsdaten deiner Rclone-Installation.")
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                        .padding(.top, 6)
                        .padding(.bottom, 16)

                    if !model.savedServerProfiles.isEmpty {
                        savedServers
                            .padding(.bottom, 16)
                    }

                    connectionForm

                    if let error = model.errorMessage {
                        ErrorBanner(message: error, dismiss: model.dismissMessages)
                            .padding(.top, 16)
                            .transition(.opacity.combined(with: .move(edge: .top)))
                    }

                    loginButton
                        .padding(.top, 22)

                    secureLoginButtons
                        .padding(.top, 14)

                    Button {
                        focusedField = nil
                        model.enterDemoMode()
                    } label: {
                        Label("App mit Beispieldaten ansehen", systemImage: "sparkles.rectangle.stack")
                            .fontWeight(.semibold)
                            .frame(maxWidth: .infinity, minHeight: 48)
                    }
                    .buttonStyle(.bordered)
                    .controlSize(.large)
                    .padding(.top, 10)
                    .accessibilityHint("Öffnet eine lokale, unveränderliche Vorschau ohne Server und ohne echte Daten.")

                    Text("Die Vorschau läuft vollständig auf diesem iPhone und verbindet sich mit keinem Server.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                        .frame(maxWidth: .infinity)
                        .padding(.top, 8)

                    Label("Die App speichert dein Passwort nicht. Es wird zur Anmeldung an den angegebenen Server gesendet.", systemImage: "lock.shield")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                        .frame(maxWidth: .infinity, alignment: .center)
                        .padding(.top, 18)
                }
                .frame(maxWidth: 440)
                .padding(.horizontal, 24)
                .padding(.top, 34)
                .padding(.bottom, 32)
                .frame(maxWidth: .infinity)
            }
            .scrollDismissesKeyboard(.interactively)
        }
        .toolbar {
            ToolbarItemGroup(placement: .keyboard) {
                Spacer()
                Button("Fertig") { focusedField = nil }
            }
        }
        .onAppear {
            server = model.serverAddress
            username = model.savedUsername
        }
        .alert("Passwort unverschlüsselt senden?", isPresented: $showHTTPWarning) {
            Button("Abbrechen", role: .cancel) {}
            Button("Über HTTP anmelden", role: .destructive, action: performLogin)
        } message: {
            Text("Diese Serveradresse verwendet kein HTTPS. Benutzername und Passwort können im Netzwerk mitgelesen werden. Bestätige dies für diesen Anmeldeversuch ausdrücklich.")
        }
    }

    private var brand: some View {
        HStack(spacing: 12) {
            Image(systemName: "arrow.triangle.2.circlepath.icloud.fill")
                .font(.system(size: 22, weight: .semibold))
                .foregroundStyle(.green)
                .frame(width: 44, height: 44)
                .background(.green.opacity(0.12), in: RoundedRectangle(cornerRadius: 13, style: .continuous))
                .accessibilityHidden(true)

            VStack(alignment: .leading, spacing: 2) {
                Text("Rclone Sync")
                    .font(.headline)
                Text("Sicherungen auf deinem Server")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }

    private var connectionForm: some View {
        VStack(spacing: 0) {
            LoginField(
                title: "Server",
                placeholder: "192.168.1.67 oder backup.example.de",
                accessibilityHint: "Gib die vollständige Adresse deiner Rclone-Sync-Installation ein. Einen abweichenden Port direkt anhängen.",
                symbol: "server.rack",
                text: $server,
                contentType: .URL,
                keyboardType: .URL,
                capitalization: .never,
                isSecure: false,
                focus: $focusedField,
                field: .server
            )
            .submitLabel(.next)
            .onSubmit { focusedField = .username }

            Text("Lokale IP-Adressen verwenden automatisch HTTP. Einen abweichenden Port kannst du direkt anhängen.")
                .font(.caption)
                .foregroundStyle(.secondary)
                .padding(.horizontal, 16)
                .padding(.bottom, 10)

            Divider().padding(.leading, 48)

            LoginField(
                title: "Benutzer",
                placeholder: "admin",
                accessibilityHint: "Gib den Benutzernamen für die Serveranmeldung ein.",
                symbol: "person",
                text: $username,
                contentType: .username,
                keyboardType: .default,
                capitalization: .never,
                isSecure: false,
                focus: $focusedField,
                field: .username
            )
            .submitLabel(.next)
            .onSubmit { focusedField = .password }

            Divider().padding(.leading, 48)

            LoginField(
                title: "Passwort",
                placeholder: "Passwort",
                accessibilityHint: "Gib das Passwort für die Serveranmeldung ein.",
                symbol: "key",
                text: $password,
                contentType: .password,
                keyboardType: .default,
                capitalization: .never,
                isSecure: true,
                focus: $focusedField,
                field: .password
            )
            .submitLabel(.go)
            .onSubmit(login)
        }
        .background(Color(.secondarySystemGroupedBackground), in: RoundedRectangle(cornerRadius: 18, style: .continuous))
        .overlay {
            RoundedRectangle(cornerRadius: 18, style: .continuous)
                .stroke(.primary.opacity(0.07), lineWidth: 1)
        }
    }

    private var loginButton: some View {
        Button(action: model.isRefreshing ? cancelLogin : login) {
            HStack(spacing: 10) {
                if model.isRefreshing {
                    ProgressView().tint(.white)
                }
                Text(model.isRefreshing ? "Abbrechen" : "Verbinden")
                    .fontWeight(.semibold)
            }
            .frame(maxWidth: .infinity, minHeight: 52)
        }
        .buttonStyle(.borderedProminent)
        .controlSize(.large)
        .tint(.green)
        .disabled(!model.isRefreshing && (server.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || username.isEmpty || password.isEmpty))
        .accessibilityLabel(model.isRefreshing ? "Anmeldung abbrechen" : "Mit Server verbinden")
        .accessibilityHint(model.isRefreshing
            ? "Bricht den laufenden Verbindungsversuch ab."
            : "Meldet dich mit der eingegebenen Serveradresse und den Zugangsdaten an.")
    }

    private var savedServers: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 9) {
                ForEach(model.savedServerProfiles) { profile in
                    Button {
                        server = profile.address
                        username = profile.username
                        password = ""
                        focusedField = .password
                    } label: {
                        Label(profile.name, systemImage: "server.rack")
                            .font(.subheadline.weight(.medium))
                            .padding(.horizontal, 12)
                            .frame(minHeight: 40)
                    }
                    .buttonStyle(.bordered)
                    .accessibilityHint("Übernimmt Serveradresse und Benutzername. Das Passwort wird nicht gespeichert.")
                }
            }
        }
    }

    private var secureLoginButtons: some View {
        VStack(spacing: 10) {
            HStack(spacing: 10) {
                Rectangle().fill(.secondary.opacity(0.25)).frame(height: 1)
                Text("ODER SICHER")
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(.secondary)
                Rectangle().fill(.secondary.opacity(0.25)).frame(height: 1)
            }

            Button { performWebAuthn(method: "passkey") } label: {
                Label("Mit Passkey anmelden", systemImage: "person.badge.key.fill")
                    .fontWeight(.semibold)
                    .frame(maxWidth: .infinity, minHeight: 48)
            }
            .buttonStyle(.bordered)
            .controlSize(.large)

            Button { performWebAuthn(method: "security_key") } label: {
                Label("Mit Sicherheitsschlüssel", systemImage: "key.horizontal.fill")
                    .fontWeight(.semibold)
                    .frame(maxWidth: .infinity, minHeight: 48)
            }
            .buttonStyle(.bordered)
            .controlSize(.large)
        }
        .disabled(model.isRefreshing || server.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
        .accessibilityHint("Öffnet die sichere Anmeldeseite deiner Rclone-Installation.")
    }

    private func login() {
        focusedField = nil
        guard let url = try? APIClient.normalizedServerURL(server) else {
            performLogin()
            return
        }
        if APIClient.requiresExplicitInsecureTransportConfirmation(url) {
            showHTTPWarning = true
            return
        }
        performLogin()
    }

    private func performLogin() {
        loginTask?.cancel()
        loginTask = Task {
            await model.login(server: server, username: username, password: password)
            loginTask = nil
        }
    }

    private func performWebAuthn(method: String) {
        focusedField = nil
        loginTask?.cancel()
        loginTask = Task {
            await model.loginWithWebAuthn(server: server, method: method)
            loginTask = nil
        }
    }

    private func cancelLogin() {
        loginTask?.cancel()
        loginTask = nil
    }
}

private struct LoginField: View {
    let title: String
    let placeholder: String
    let accessibilityHint: String
    let symbol: String
    @Binding var text: String
    let contentType: UITextContentType?
    let keyboardType: UIKeyboardType
    let capitalization: TextInputAutocapitalization
    let isSecure: Bool
    let focus: FocusState<LoginFieldID?>.Binding
    let field: LoginFieldID

    var body: some View {
        HStack(spacing: 13) {
            Image(systemName: symbol)
                .font(.body.weight(.medium))
                .foregroundStyle(.secondary)
                .frame(width: 24)

            VStack(alignment: .leading, spacing: 3) {
                Text(title)
                    .font(.caption)
                    .foregroundStyle(.secondary)

                Group {
                    if isSecure {
                        SecureField(placeholder, text: $text)
                    } else {
                        TextField(placeholder, text: $text)
                    }
                }
                .textContentType(contentType)
                .focused(focus, equals: field)
                .textInputAutocapitalization(capitalization)
                .autocorrectionDisabled()
                .keyboardType(keyboardType)
                .accessibilityLabel(title)
                .accessibilityHint(accessibilityHint)
                .accessibilityIdentifier("login\(title)Field")
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
        .contentShape(Rectangle())
    }
}
