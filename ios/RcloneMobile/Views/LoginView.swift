import SwiftUI

private enum LoginFieldID: Hashable { case server, username, password }

struct LoginView: View {
    @EnvironmentObject private var model: AppModel
    @State private var server = ""
    @State private var username = "admin"
    @State private var password = ""
    @State private var rememberSession = true
    @State private var selectedTarget: ServerLoginTarget?
    @State private var canonicalSavedTarget: ServerLoginTarget?
    @State private var inputError: String?
    @State private var didLoadInitialValues = false
    @State private var loginTask: Task<Void, Never>?
    @State private var showHTTPWarning = false
    @State private var showingOfflineCard = false
    @State private var showingServerOverview = false
    @State private var showingLegalInformation = false
    @FocusState private var focusedField: LoginFieldID?

    var body: some View {
        ZStack {
            SicherpfadGlassBackdrop()

            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    brand
                        .padding(.bottom, 30)

                    Text(selectedTarget == nil ? "Deinen Server verbinden" : "Bei Sicherpfad anmelden")
                        .font(.title2.weight(.bold))

                    Text(selectedTarget == nil
                        ? "Füge die Adresse deines Sicherpfad-Servers ein. Du kannst sie aus dem Browser kopieren."
                        : "Verwende die Zugangsdaten dieses Sicherpfad-Servers.")
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                        .padding(.top, 6)
                        .padding(.bottom, 16)

                    if let target = selectedTarget {
                        ServerLoginTargetCard(target: target)
                        Button("Server ändern", action: changeServer)
                            .frame(minHeight: 44)
                            .disabled(isSigningIn)
                            .padding(.bottom, 8)
                        credentialForm
                        Toggle("Angemeldet bleiben", isOn: $rememberSession)
                            .frame(minHeight: 44)
                            .padding(.top, 16)
                            .disabled(isSigningIn)
                        Text("Nur HTTPS-Sitzungen werden geschützt auf diesem Gerät gespeichert. Bei HTTP ist nach einem App-Neustart eine neue Anmeldung nötig. Dein Passwort wird nicht gespeichert.")
                            .font(.caption).foregroundStyle(.secondary)
                    } else {
                        if !model.savedServerProfiles.isEmpty {
                            savedServers.padding(.bottom, 16)
                        }
                        serverAddressForm
                    }

                    if let inputError {
                        ErrorBanner(message: inputError) { self.inputError = nil }
                            .padding(.top, 16)
                    }
                    if let error = model.errorMessage {
                        ErrorBanner(message: error, dismiss: model.dismissMessages)
                            .padding(.top, 16)
                            .transition(.opacity.combined(with: .move(edge: .top)))
                    }

                    if selectedTarget == nil {
                        Button(action: chooseServer) {
                            Text("Weiter zur Anmeldung")
                                .fontWeight(.semibold)
                                .frame(maxWidth: .infinity, minHeight: 52)
                        }
                        .buttonStyle(SicherpfadGlassProminentButtonStyle())
                        .disabled(isSigningIn || server.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                        .padding(.top, 22)
                        .accessibilityHint("Prüft nur das Adressformat. Eine Verbindung wird erst bei der Anmeldung aufgebaut.")
                    } else {
                        loginButton.padding(.top, 22)
                        secureLoginButtons.padding(.top, 14)
                    }
                    if !model.savedServerProfiles.isEmpty {
                        Button("Alle Server ansehen") {
                            focusedField = nil
                            showingServerOverview = true
                        }
                        .frame(maxWidth: .infinity, minHeight: 44)
                        .disabled(isSigningIn)
                    }
                    Button("Offline-Notfallkarte") { showingOfflineCard = true }
                        .font(.footnote).frame(maxWidth: .infinity, minHeight: 44).padding(.top, 14)
                        .disabled(isSigningIn)

                    Button {
                        focusedField = nil
                        model.enterDemoMode()
                    } label: {
                        Label("App mit Beispieldaten ansehen", systemImage: "sparkles.rectangle.stack")
                            .fontWeight(.semibold)
                            .frame(maxWidth: .infinity, minHeight: 48)
                    }
                    .buttonStyle(SicherpfadGlassSecondaryButtonStyle())
                    .disabled(isSigningIn)
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
                    Button("Projektinformationen") {
                        focusedField = nil
                        showingLegalInformation = true
                    }
                    .frame(maxWidth: .infinity, minHeight: 44)
                    .disabled(isSigningIn)
                    .padding(.top, 8)
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
            guard !didLoadInitialValues else { return }
            didLoadInitialValues = true
            server = model.serverAddress
            username = model.savedUsername
            if let url = URL(string: model.serverAddress) {
                canonicalSavedTarget = try? ServerLoginTarget(serverURL: url)
            }
        }
        .sheet(isPresented: $showingLegalInformation) { LegalInformationSheet() }
        .sheet(isPresented: $showingOfflineCard) { OfflineRecoveryCardView() }
        .sheet(isPresented: $showingServerOverview) {
            NavigationStack {
                ServerOverviewView()
                    .toolbar {
                        ToolbarItem(placement: .confirmationAction) {
                            Button("Fertig") { showingServerOverview = false }
                        }
                    }
            }
        }
        .alert("Passwort unverschlüsselt senden?", isPresented: $showHTTPWarning) {
            Button("Abbrechen", role: .cancel) {}
            Button("Über HTTP anmelden", role: .destructive, action: performLogin)
        } message: {
            Text("HTTP ist unverschlüsselt. Zugangsdaten und Sitzungen können im Netzwerk mitgelesen werden. Diese Sitzung wird auch mit Angemeldet bleiben nicht gespeichert. Nach einem App-Neustart musst du dich erneut anmelden.")
        }
    }

    private var brand: some View {
        HStack(spacing: 14) {
            Image("SicherpfadMark")
                .resizable().scaledToFit()
                .frame(width: 48, height: 48)
                .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
                .overlay {
                    RoundedRectangle(cornerRadius: 12, style: .continuous)
                        .strokeBorder(Color.white.opacity(0.50), lineWidth: 1)
                }
                .shadow(color: SicherpfadGlassPalette.emerald.opacity(0.42), radius: 14, y: 5)
                .accessibilityHidden(true)

            VStack(alignment: .leading, spacing: 2) {
                Text("Sicherpfad")
                    .font(.headline)
                Text("Sicherungen auf deinem Server")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }

    private var isSigningIn: Bool { loginTask != nil || model.isRefreshing }

    private var serverAddressForm: some View {
        VStack(spacing: 0) {
            LoginField(
                title: "Server",
                placeholder: "192.168.50.10 oder backup.example.de",
                accessibilityHint: "Gib die vollständige Adresse deines Sicherpfad-Servers ein. Einen abweichenden Port direkt anhängen.",
                symbol: "server.rack",
                text: $server,
                contentType: .URL,
                keyboardType: .URL,
                capitalization: .never,
                isSecure: false,
                focus: $focusedField,
                field: .server
            )
            .submitLabel(.continue)
            .onSubmit(chooseServer)
            .disabled(isSigningIn)

            Text("HTTPS wird empfohlen. Lokale Adressen ohne http:// oder https:// verwenden HTTP. Port und Basispfad kannst du mit angeben; ein abschließendes /login wird aus der Browseradresse entfernt.")
                .font(.caption)
                .foregroundStyle(.secondary)
                .padding(.horizontal, 16)
                .padding(.bottom, 10)

        }
        .sicherpfadGlassCard(cornerRadius: 22)
    }

    private var credentialForm: some View {
        VStack(spacing: 0) {
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
        .disabled(isSigningIn)
        .sicherpfadGlassCard(cornerRadius: 22)
    }

    private var loginButton: some View {
        Button(action: isSigningIn ? cancelLogin : login) {
            HStack(spacing: 10) {
                if isSigningIn {
                    ProgressView().tint(.white)
                }
                Text(isSigningIn ? "Abbrechen" : "Anmelden")
                    .fontWeight(.semibold)
            }
            .frame(maxWidth: .infinity, minHeight: 52)
        }
        .buttonStyle(SicherpfadGlassProminentButtonStyle())
        .disabled(!isSigningIn && (selectedTarget == nil || username.isEmpty || password.isEmpty))
        .accessibilityLabel(isSigningIn ? "Anmeldung abbrechen" : "Bei Sicherpfad anmelden")
        .accessibilityHint(isSigningIn
            ? "Bricht den laufenden Verbindungsversuch ab."
            : "Meldet dich mit der eingegebenen Serveradresse und den Zugangsdaten an.")
    }

    private var savedServers: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 9) {
                ForEach(model.savedServerProfiles) { profile in
                    Button {
                        selectSavedProfile(profile)
                    } label: {
                        Label(profile.name, systemImage: "server.rack")
                            .font(.subheadline.weight(.medium))
                            .padding(.horizontal, 12)
                            .frame(minHeight: 44)
                    }
                    .buttonStyle(.bordered)
                    .disabled(isSigningIn)
                    .accessibilityHint("Wählt diese Adresse und öffnet die Anmeldung. Das Passwort wird nicht gespeichert.")
                }
            }
        }

    }

    private var secureLoginButtons: some View {
        VStack(spacing: 10) {
            HStack(spacing: 10) {
                Rectangle().fill(.secondary.opacity(0.25)).frame(height: 1)
                Text("ODER")
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(.secondary)
                Rectangle().fill(.secondary.opacity(0.25)).frame(height: 1)
            }

            Button { performWebAuthn(method: "passkey") } label: {
                Label("Mit Passkey anmelden", systemImage: "person.badge.key.fill")
                    .fontWeight(.semibold)
                    .frame(maxWidth: .infinity, minHeight: 48)
            }
            .buttonStyle(SicherpfadGlassSecondaryButtonStyle())
            .disabled(isSigningIn || selectedTarget?.supportsPasskeys != true)

            Button { performWebAuthn(method: "security_key") } label: {
                Label("Mit Sicherheitsschlüssel", systemImage: "key.horizontal.fill")
                    .fontWeight(.semibold)
                    .frame(maxWidth: .infinity, minHeight: 48)
            }
            .buttonStyle(SicherpfadGlassSecondaryButtonStyle())
            .disabled(isSigningIn || selectedTarget?.supportsPasskeys != true)

            if selectedTarget?.supportsPasskeys != true {
                Text("Passkeys und Sicherheitsschlüssel benötigen einen Domainnamen mit HTTPS. Verwende dafür die passende HTTPS-Adresse deines Servers.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .accessibilityHint("Öffnet die sichere Anmeldeseite deiner Rclone-Installation.")
    }

    private func login() {
        guard !isSigningIn, !username.isEmpty, !password.isEmpty, let target = selectedTarget else { return }
        focusedField = nil
        inputError = nil
        if APIClient.requiresExplicitInsecureTransportConfirmation(target.url) {
            showHTTPWarning = true
            return
        }
        performLogin()
    }

    private func performLogin() {
        guard !isSigningIn, !username.isEmpty, !password.isEmpty, let target = selectedTarget else { return }
        let submittedUsername = username
        let submittedPassword = password
        let submittedRemember = rememberSession
        loginTask = Task {
            await model.login(server: target.address, username: submittedUsername, password: submittedPassword, rememberSession: submittedRemember)
            loginTask = nil
        }
    }

    private func performWebAuthn(method: String) {
        guard !isSigningIn, let target = selectedTarget, target.supportsPasskeys else { return }
        focusedField = nil
        inputError = nil
        let submittedRemember = rememberSession
        loginTask = Task {
            await model.loginWithWebAuthn(server: target.address, method: method, rememberSession: submittedRemember)
            loginTask = nil
        }
    }

    private func cancelLogin() {
        loginTask?.cancel()
        loginTask = nil
    }

    private func chooseServer() {
        guard !isSigningIn else { return }
        do {
            let target: ServerLoginTarget
            if let known = canonicalSavedTarget, server.trimmingCharacters(in: .whitespacesAndNewlines) == known.address {
                target = known
            } else {
                target = try ServerLoginTarget(address: server)
            }
            selectedTarget = target
            canonicalSavedTarget = target
            server = target.address
            password = ""
            inputError = nil
            focusedField = .username
        } catch {
            inputError = error.localizedDescription
            focusedField = .server
        }
    }

    private func changeServer() {
        guard !isSigningIn else { return }
        if let selectedTarget {
            canonicalSavedTarget = selectedTarget
            server = selectedTarget.address
        }
        password = ""
        inputError = nil
        showHTTPWarning = false
        selectedTarget = nil
        model.dismissMessages()
        focusedField = .server
    }

    private func selectSavedProfile(_ profile: SavedServerProfile) {
        guard !isSigningIn else { return }
        password = ""
        username = profile.username
        server = profile.address
        model.dismissMessages()
        do {
            guard let url = URL(string: profile.address) else { throw APIError.invalidServer }
            let target = try ServerLoginTarget(serverURL: url)
            canonicalSavedTarget = target
            selectedTarget = target
            server = target.address
            inputError = nil
            focusedField = .password
        } catch {
            inputError = error.localizedDescription
            canonicalSavedTarget = nil
            focusedField = .server
        }
    }
}

struct ServerLoginTargetCard: View {
    let target: ServerLoginTarget

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Gewählter Server")
                .font(.caption.weight(.medium))
                .foregroundStyle(.secondary)
            Text(target.address)
                .font(.subheadline.monospaced())
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityLabel("Gewählter Server: \(target.address)")
            Label(target.usesHTTPS ? "HTTPS-Adresse" : "HTTP · unverschlüsselt", systemImage: target.usesHTTPS ? "link" : "exclamationmark.triangle")
                .font(.footnote)
                .foregroundStyle(target.usesHTTPS ? Color.secondary : Color.orange)
            if target.isTailscale {
                Text("Tailscale muss auf diesem iPhone aktiv sein, damit der Server erreichbar ist.")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(16)
        .sicherpfadGlassCard(cornerRadius: 20, accent: target.usesHTTPS ? SicherpfadGlassPalette.emerald : .orange)
    }
}

private struct LoginField: View {
    @ScaledMetric(relativeTo: .body) private var symbolWidth: CGFloat = 28
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
        HStack(alignment: .top, spacing: 13) {
            Image(systemName: symbol)
                .font(.body.weight(.medium))
                .foregroundStyle(.secondary)
                .fixedSize()
                .frame(width: symbolWidth, alignment: .leading)

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
        .frame(minHeight: 44)
        .contentShape(Rectangle())
    }
}
