import SwiftUI

struct RootTabView: View {
    @EnvironmentObject private var model: AppModel
    @StateObject private var configurationDraft = ConfigurationDraftStore()
    @State private var selectedTab: Int
    @State private var showingSettings = false
    @State private var showingRecoveryCenter = false
    @State private var showingDeviceVault: Bool

    init(initialTab: Int = StorePreviewMode.initialTab) {
        _selectedTab = State(initialValue: initialTab)
        _showingDeviceVault = State(initialValue: StorePreviewMode.opensDeviceVault)
    }

    var body: some View {
        TabView(selection: $selectedTab) {
            NavigationStack { DashboardView(showingSettings: $showingSettings) }
                .tabItem { Label("Übersicht", systemImage: "shield.checkered") }
                .tag(0)

            NavigationStack { ProtectionHomeView() }
                .tabItem { Label("Sichern", systemImage: "square.and.arrow.up") }
                .tag(1)

            NavigationStack { RecoveryCenterView() }
                .tabItem { Label("Wiederherstellen", systemImage: "arrow.counterclockwise") }
                .tag(2)

            AdministrationView(showingSettings: $showingSettings)
                .tabItem { Label("Mehr", systemImage: "ellipsis") }
                .tag(3)
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
        .sheet(isPresented: $showingRecoveryCenter) {
            NavigationStack { RecoveryCenterView() }
        }
        .sheet(isPresented: $showingDeviceVault) {
            NavigationStack { DeviceVaultView() }
        }
        .environmentObject(configurationDraft)
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
        .onReceive(NotificationCenter.default.publisher(for: .pushRecoveryNavigationRequested)) { _ in
            selectedTab = 2
        }
        .onReceive(NotificationCenter.default.publisher(for: .deviceVaultNavigationRequested)) { _ in
            showingDeviceVault = true
        }
        .task { configurationDraft.load(from: model.config) }
        .onChange(of: model.config?.revision) { _, _ in
            configurationDraft.load(from: model.config)
        }
    }

    private func selectRequestedRunIfNeeded() {
        guard model.requestedRunID != nil else { return }
        selectedTab = 3
    }
}

private struct AdministrationView: View {
    @EnvironmentObject private var model: AppModel
    @Binding var showingSettings: Bool
    @State private var route = StorePreviewMode.adminDestination

    var body: some View {
        NavigationStack {
            List {
                Section("Verwalten") {
                    NavigationLink("Datenwege", value: "paths")
                    NavigationLink("Jobs & Zeitpläne", value: "jobs")
                    NavigationLink("Läufe & Protokolle", value: "runs")
                }
                Section("Server") {
                    NavigationLink("System & Wartung", value: "system")
                    Button("Konto & Anmeldung") { showingSettings = true }
                }
            }
            .navigationTitle("Mehr")
            .navigationDestination(for: String.self) { destination($0) }
            .navigationDestination(isPresented: Binding(get: { route != nil }, set: { if !$0 { route = nil } })) {
                destination(route ?? "runs")
            }
            .onAppear { if model.requestedRunID != nil { route = "runs" } }
            .onChange(of: model.requestedRunID) { _, id in if id != nil { route = "runs" } }
        }
    }

    @ViewBuilder private func destination(_ value: String) -> some View {
        switch value {
        case "paths": DataPathsScreen(showingSettings: $showingSettings)
        case "jobs": JobsScreen(showingSettings: $showingSettings)
        case "system": SystemView(showingSettings: $showingSettings)
        default: RunsScreen(showingSettings: $showingSettings)
        }
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
                    if model.isDemoMode {
                        Label("Nur lokale Beispieldaten", systemImage: "checkmark.shield")
                            .foregroundStyle(.green)
                    }
                    if let version = model.overview?.app.version {
                        LabeledContent("Server-Version", value: version)
                    }
                }
                if !model.savedServerProfiles.isEmpty {
                    Section {
                        ForEach(model.savedServerProfiles) { profile in
                            HStack {
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(profile.name)
                                    Text(profile.address).font(.caption).foregroundStyle(.secondary)
                                }
                                Spacer()
                                if profile.address == model.serverAddress {
                                    Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
                                }
                            }
                            .swipeActions {
                                Button("Entfernen", role: .destructive) {
                                    model.forgetServerProfile(profile)
                                }
                            }
                        }
                    } header: {
                        Text("Gespeicherte Server")
                    } footer: {
                        Text("Profile enthalten Adresse und Benutzername, niemals Passwörter. Auf Wunsch liegt die Sitzung geschützt im Geräteschlüsselbund. Entfernen beendet ihre Speicherung. Serverwechsel erfolgt auf der Anmeldeseite.")
                    }
                }
                if !model.isDemoMode {
                    Section("Sicherheit") {
                        NavigationLink { WebAuthnSecurityView() } label: {
                            Label("Passkeys & Sicherheitsschlüssel", systemImage: "person.badge.key")
                        }
                    }
                }
                Section("App") {
                    LabeledContent("App-Version", value: appVersion)
                    LabeledContent("TestFlight-Build", value: appBuild)
                }
                Section {
                    Button(model.isDemoMode ? "Vorschau beenden" : "Abmelden", role: model.isDemoMode ? nil : .destructive) {
                        if model.isDemoMode {
                            Task { await model.logout(); dismiss() }
                        } else {
                            confirmLogout = true
                        }
                    }
                } footer: {
                    Text(model.isDemoMode
                        ? "Die Vorschau enthält keine echten Server- oder Dateidaten."
                        : "Die Abmeldung beendet aus Sicherheitsgründen alle aktiven Administrationssitzungen.")
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
