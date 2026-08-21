import SwiftUI

private enum LoginFieldID: Hashable { case server, username, password }

struct LoginView: View {
    @EnvironmentObject private var model: AppModel
    @State private var server = ""
    @State private var username = "admin"
    @State private var password = ""
    @FocusState private var focusedField: LoginFieldID?

    var body: some View {
        ZStack {
            Color(.systemBackground)
                .ignoresSafeArea()

            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    brand
                        .padding(.bottom, 34)

                    connectionForm

                    if let error = model.errorMessage {
                        ErrorBanner(message: error, dismiss: model.dismissMessages)
                            .padding(.top, 16)
                            .transition(.opacity.combined(with: .move(edge: .top)))
                    }

                    loginButton
                        .padding(.top, 22)

                    Label("Dein Passwort bleibt auf diesem iPhone.", systemImage: "lock.shield")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity, alignment: .center)
                        .padding(.top, 18)
                }
                .frame(maxWidth: 440)
                .padding(.horizontal, 24)
                .padding(.top, 52)
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
    }

    private var brand: some View {
        VStack(alignment: .leading, spacing: 16) {
            ZStack {
                RoundedRectangle(cornerRadius: 19, style: .continuous)
                    .fill(.green.opacity(0.13))
                Image(systemName: "arrow.triangle.2.circlepath.icloud.fill")
                    .font(.system(size: 34, weight: .semibold))
                    .foregroundStyle(.green)
            }
            .frame(width: 68, height: 68)
            .accessibilityHidden(true)

            VStack(alignment: .leading, spacing: 8) {
                Text("Rclone Sync")
                    .font(.largeTitle.weight(.bold))
                Text("Deine Sicherungen. Ruhig im Blick.")
                    .font(.title3)
                    .foregroundStyle(.secondary)
            }
        }
    }

    private var connectionForm: some View {
        VStack(spacing: 0) {
            LoginField(
                title: "Server",
                placeholder: "192.168.1.97 oder backup.example.de",
                symbol: "server.rack",
                text: $server,
                contentType: .URL,
                isSecure: false,
                focus: $focusedField,
                field: .server
            )
            .submitLabel(.next)
            .onSubmit { focusedField = .username }

            Text("Lokale IP-Adressen verwenden automatisch HTTP und Port 8001.")
                .font(.caption)
                .foregroundStyle(.secondary)
                .padding(.horizontal, 16)
                .padding(.bottom, 10)

            Divider().padding(.leading, 48)

            LoginField(
                title: "Benutzer",
                placeholder: "admin",
                symbol: "person",
                text: $username,
                contentType: .username,
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
                symbol: "key",
                text: $password,
                contentType: .password,
                isSecure: true,
                focus: $focusedField,
                field: .password
            )
            .submitLabel(.go)
            .onSubmit(login)
        }
        .background(Color(.secondarySystemBackground), in: RoundedRectangle(cornerRadius: 20, style: .continuous))
        .overlay {
            RoundedRectangle(cornerRadius: 20, style: .continuous)
                .stroke(.primary.opacity(0.07), lineWidth: 1)
        }
    }

    private var loginButton: some View {
        Button(action: login) {
            HStack(spacing: 10) {
                if model.isRefreshing {
                    ProgressView().tint(.white)
                }
                Text(model.isRefreshing ? "Verbindung wird hergestellt …" : "Anmelden")
                    .fontWeight(.semibold)
            }
            .frame(maxWidth: .infinity, minHeight: 52)
        }
        .buttonStyle(.borderedProminent)
        .controlSize(.large)
        .tint(.green)
        .disabled(server.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || username.isEmpty || password.isEmpty || model.isRefreshing)
    }

    private func login() {
        focusedField = nil
        Task { await model.login(server: server, username: username, password: password) }
    }
}

private struct LoginField: View {
    let title: String
    let placeholder: String
    let symbol: String
    @Binding var text: String
    let contentType: UITextContentType?
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
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .keyboardType(.asciiCapable)
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
        .contentShape(Rectangle())
    }
}
