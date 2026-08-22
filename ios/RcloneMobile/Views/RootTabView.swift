import SwiftUI

struct RootTabView: View {
    @EnvironmentObject private var model: AppModel
    @State private var selectedTab = 0
    @State private var showingSettings = false

    var body: some View {
        TabView(selection: $selectedTab) {
            NavigationStack { DashboardView(showingSettings: $showingSettings) }
                .tabItem { Label("Lage", systemImage: "shield.checkered") }
                .tag(0)

            NavigationStack { DataPathsScreen(showingSettings: $showingSettings) }
                .tabItem { Label("Datenwege", systemImage: "arrow.left.arrow.right") }
                .tag(1)

            NavigationStack { JobsScreen(showingSettings: $showingSettings) }
                .tabItem { Label("Jobs", systemImage: "calendar") }
                .tag(2)

            NavigationStack { RunsScreen(showingSettings: $showingSettings) }
                .tabItem { Label("Läufe", systemImage: "clock.arrow.circlepath") }
                .tag(3)

            NavigationStack { SystemView(showingSettings: $showingSettings) }
                .tabItem { Label("System", systemImage: "server.rack") }
                .tag(4)
        }
        .tint(.green)
        .safeAreaInset(edge: .top, spacing: 0) {
            if let error = model.errorMessage {
                ErrorBanner(message: error, dismiss: model.dismissMessages)
                    .padding(.horizontal, 12)
                    .padding(.vertical, 8)
                    .background(.bar)
                    .accessibilityIdentifier("globalErrorBanner")
            }
        }
        .sheet(isPresented: $showingSettings) { SettingsView() }
        .alert("Hinweis", isPresented: Binding(
            get: { model.actionMessage != nil },
            set: { if !$0 { model.dismissMessages() } }
        )) {
            Button("OK") { model.dismissMessages() }
        } message: {
            Text(model.actionMessage ?? "")
        }
        .onAppear { selectRequestedRunIfNeeded() }
        .onChange(of: model.requestedRunID) { _, _ in selectRequestedRunIfNeeded() }
    }

    private func selectRequestedRunIfNeeded() {
        guard model.requestedRunID != nil else { return }
        selectedTab = 3
    }
}
struct SettingsButton: View {
    @Binding var showingSettings: Bool

    var body: some View {
        Button { showingSettings = true } label: { Image(systemName: "person.crop.circle") }
            .accessibilityLabel("Konto und Einstellungen")
    }
}

private struct SettingsView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var confirmLogout = false

    var body: some View {
        NavigationStack {
            Form {
                Section("Verbindung") {
                    LabeledContent("Server", value: model.serverAddress)
                    LabeledContent("Benutzer", value: model.savedUsername)
                    if let version = model.overview?.app.version {
                        LabeledContent("Server-Version", value: version)
                    }
                }
                Section("App") {
                    LabeledContent("App-Version", value: appVersion)
                    LabeledContent("TestFlight-Build", value: appBuild)
                }
                Section {
                    Button("Abmelden", role: .destructive) { confirmLogout = true }
                } footer: {
                    Text("Die Abmeldung beendet aus Sicherheitsgründen alle aktiven Administrationssitzungen.")
                }
            }
            .navigationTitle("Konto")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Fertig") { dismiss() } } }
            .confirmationDialog("Alle Sitzungen abmelden?", isPresented: $confirmLogout, titleVisibility: .visible) {
                Button("Alle Sitzungen abmelden", role: .destructive) {
                    Task { await model.logout(); dismiss() }
                }
                Button("Abbrechen", role: .cancel) {}
            } message: {
                Text("Du musst dich anschließend auf allen Geräten neu anmelden.")
            }
        }
    }

    private var appVersion: String {
        Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "–"
    }

    private var appBuild: String {
        Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "–"
    }
}
