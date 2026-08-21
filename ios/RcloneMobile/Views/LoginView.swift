import SwiftUI

struct LoginView: View {
    @EnvironmentObject private var model: AppModel
    @State private var server = ""
    @State private var username = "admin"
    @State private var password = ""
    @FocusState private var focusedField: Field?

    private enum Field { case server, username, password }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 26) {
                    brand
                    VStack(spacing: 14) {
                        TextField("https://backup.example.de", text: $server)
                            .textContentType(.URL)
                            .keyboardType(.URL)
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                            .focused($focusedField, equals: .server)
                            .submitLabel(.next)
                            .onSubmit { focusedField = .username }
                        TextField("Benutzername", text: $username)
                            .textContentType(.username)
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                            .focused($focusedField, equals: .username)
                            .submitLabel(.next)
                            .onSubmit { focusedField = .password }
                        SecureField("Passwort", text: $password)
                            .textContentType(.password)
                            .focused($focusedField, equals: .password)
                            .submitLabel(.go)
                            .onSubmit(login)
                    }
                    .textFieldStyle(.roundedBorder)

                    if let error = model.errorMessage {
                        ErrorBanner(message: error, dismiss: model.dismissMessages)
                            .transition(.opacity.combined(with: .move(edge: .top)))
                    }

                    Button(action: login) {
                        HStack {
                            if model.isRefreshing { ProgressView().tint(.white) }
                            Text(model.isRefreshing ? "Verbindung wird hergestellt …" : "Sicher anmelden")
                                .frame(maxWidth: .infinity)
                        }
                    }
                    .buttonStyle(.borderedProminent)
                    .controlSize(.large)
                    .tint(.teal)
                    .disabled(server.isEmpty || username.isEmpty || password.isEmpty || model.isRefreshing)

                    Label("Das Passwort wird nicht auf diesem iPhone gespeichert.", systemImage: "lock.shield")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
                .padding(.horizontal, 24)
                .padding(.vertical, 38)
                .frame(maxWidth: 520)
                .frame(maxWidth: .infinity)
            }
            .background(Color(.systemGroupedBackground))
            .onAppear {
                server = model.serverAddress
                username = model.savedUsername
                focusedField = server.isEmpty ? .server : .password
            }
        }
    }

    private var brand: some View {
        VStack(alignment: .leading, spacing: 18) {
            Image(systemName: "arrow.triangle.2.circlepath.icloud.fill")
                .font(.system(size: 44, weight: .semibold))
                .foregroundStyle(.teal)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 7) {
                Text("Rclone Sync")
                    .font(.largeTitle.bold())
                Text("Sicherungen ruhig und klar im Blick.")
                    .font(.title3)
                    .foregroundStyle(.secondary)
            }
        }
    }

    private func login() {
        focusedField = nil
        Task { await model.login(server: server, username: username, password: password) }
    }
}
