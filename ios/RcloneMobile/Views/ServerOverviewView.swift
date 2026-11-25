import SwiftUI

struct ServerOverviewView: View {
    @EnvironmentObject private var app: AppModel

    var body: some View { ServerOverviewContent(store: app.serverOverview) }
}

private struct ServerOverviewContent: View {
    @EnvironmentObject private var app: AppModel
    @Environment(\.scenePhase) private var scenePhase
    @ObservedObject var store: ServerOverviewModel
    @State private var showingConnection = false
    @State private var connectionProfile: SavedServerProfile?

    private var profiles: [SavedServerProfile] {
        var result = app.savedServerProfiles
        if !app.isDemoMode, let url = try? APIClient.normalizedServerURL(app.serverAddress),
           !result.contains(where: { $0.address == url.absoluteString }) {
            result.insert(SavedServerProfile(name: url.host ?? url.absoluteString,
                address: url.absoluteString, username: app.savedUsername, lastUsedAt: 0), at: 0)
        }
        return result
    }

    private var refreshKey: String {
        profiles.map { $0.address + "|" + $0.username + "|\($0.lastUsedAt)" }.joined(separator: "\n")
            + "|\(app.serverAddress)|\(app.savedUsername)|\(app.phase)|\(app.isDemoMode)|\(scenePhase)"
    }

    var body: some View {
        TimelineView(.periodic(from: .now, by: 15)) { context in
            List {
                Section {
                    LabeledContent("Server", value: "\(store.entries.count)")
                    LabeledContent("Aktueller Stand", value: "\(store.entries.filter { $0.isCurrent(at: context.date) }.count) von \(store.entries.count)")
                    LabeledContent("Erfasste aktive Läufe", value: runningSummary(at: context.date))
                } footer: {
                    Text("Aktualisiert alle 30 Sekunden, solange diese Ansicht im Vordergrund ist. Jede Karte gehört zum angegebenen Server und Konto.")
                }

                Section("Server im Blick") {
                    if store.entries.isEmpty {
                        ContentUnavailableView("Noch keine Server", systemImage: "server.rack",
                            description: Text("Verbinde einen Server, um seinen Schutzstatus hier zu sehen."))
                    }
                    ForEach(store.entries) { entry in
                        NavigationLink {
                            ServerOverviewDetailView(store: store, serverID: entry.id, refresh: refresh) { profile in
                                connectionProfile = profile
                                showingConnection = true
                            }
                        } label: {
                            ServerOverviewRow(entry: entry, now: context.date,
                                isActive: app.phase == .signedIn && entry.profile.address == app.serverAddress)
                        }
                    }
                }

                if !app.isDemoMode {
                    Section {
                        Button {
                            connectionProfile = nil
                            showingConnection = true
                        } label: {
                            Label("Server verbinden", systemImage: "plus.circle")
                        }
                    } footer: {
                        Text("Die aktive Arbeitsverbindung bleibt beim Hinzufügen erhalten. Bis zu acht Serverprofile werden gespeichert.")
                    }
                }
            }
        }
        .sicherpfadGlassScreen()
        .navigationTitle("Alle Server")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                if store.isRefreshing {
                    ProgressView().accessibilityLabel("Server werden aktualisiert")
                } else {
                    Button { Task { await refresh() } } label: { Image(systemName: "arrow.clockwise") }
                        .accessibilityLabel("Alle Server aktualisieren")
                }
            }
        }
        .refreshable { await refresh() }
        .task(id: refreshKey) {
            guard scenePhase == .active else { return }
            repeat {
                await refresh()
                do { try await Task.sleep(for: .seconds(30)) } catch { return }
            } while !Task.isCancelled
        }
        .sheet(isPresented: $showingConnection) {
            ServerOverviewLoginView(store: store, profile: connectionProfile)
        }
    }

    private func refresh() async {
        if app.isDemoMode {
            store.showDemo(overview: app.overview, storage: app.storage, progress: app.progress, pbs: app.pbs)
        } else {
            await store.refresh(profiles: profiles, activeAddress: app.serverAddress,
                activeUsername: app.savedUsername, activeClient: app.phase == .signedIn ? app.client : nil)
        }
    }

    private func runningSummary(at now: Date) -> String {
        let snapshots = store.entries.filter { $0.isCurrent(at: now) }.compactMap(\.snapshot)
        let counts = snapshots.compactMap(\.runningCount)
        let count = counts.reduce(0, +)
        return counts.count == store.entries.count && !store.entries.isEmpty
            ? "\(count)" : "\(count) · nicht vollständig erfasst"
    }
}

struct ServerOverviewRow: View {
    let entry: ServerOverviewEntry
    let now: Date
    var isActive = false

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            Text(entry.profile.name).font(.headline)
            Text(entry.profile.address).font(.caption).foregroundStyle(.secondary)
                .lineLimit(2)
            Text(entry.profile.username + (isActive ? " · Aktive Verbindung" : ""))
                .font(.caption).foregroundStyle(.secondary)
            Label(entry.status(at: now), systemImage: symbol)
                .font(.subheadline.weight(.medium)).foregroundStyle(color)
            if let snapshot = entry.snapshot {
                if entry.isCurrent(at: now), let count = snapshot.runningCount, count > 0 {
                    Label("\(count) aktive Läufe", systemImage: "arrow.triangle.2.circlepath")
                        .font(.caption).foregroundStyle(.blue)
                }
                Text("Stand: \(AppFormat.date(snapshot.receivedAt.timeIntervalSince1970))")
                    .font(.caption2).foregroundStyle(.secondary)
                Text("Datenbereich: \(AppFormat.bytes(snapshot.overview.system.dataDisk.freeBytes)) frei")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 5)
        .accessibilityElement(children: .combine)
    }

    private var color: Color {
        guard entry.isCurrent(at: now), let snapshot = entry.snapshot else { return .secondary }
        if snapshot.hasError { return .red }
        return snapshot.needsAttention || !snapshot.missing.isEmpty ? .orange : .green
    }

    private var symbol: String {
        guard entry.isCurrent(at: now), let snapshot = entry.snapshot else { return "questionmark.circle" }
        return snapshot.hasError || snapshot.needsAttention || !snapshot.missing.isEmpty
            ? "exclamationmark.triangle" : "checkmark.shield"
    }
}

private struct ServerOverviewDetailView: View {
    @EnvironmentObject private var app: AppModel
    @Environment(\.scenePhase) private var scenePhase
    @ObservedObject var store: ServerOverviewModel
    let serverID: String
    let refresh: () async -> Void
    let reconnect: (SavedServerProfile) -> Void

    var body: some View {
        TimelineView(.periodic(from: .now, by: 15)) { context in
            List {
                if let entry = store.entries.first(where: { $0.id == serverID }) {
                    Section("Verbindung") {
                        ServerOverviewRow(entry: entry, now: context.date,
                            isActive: app.phase == .signedIn && entry.profile.address == app.serverAddress)
                        if case let .unavailable(message) = entry.state { Text(message).foregroundStyle(.secondary) }
                        if entry.state == .needsLogin,
                           app.phase != .signedIn || entry.profile.address != app.serverAddress,
                           !app.isDemoMode {
                            Button("Erneut anmelden") { reconnect(entry.profile) }
                        }
                    }
                    if let snapshot = entry.snapshot {
                        if !entry.isCurrent(at: context.date) {
                            Section {
                                Label("Letzter Stand · nicht aktuell", systemImage: "clock.badge.exclamationmark")
                                    .foregroundStyle(.orange)
                            }
                        }
                        if !snapshot.missing.isEmpty {
                            Section("Nicht verfügbar") {
                                ForEach(snapshot.missing, id: \.self) { Text($0) }
                            }
                        }
                        Section("Schutz & Betrieb") {
                            if snapshot.lockdown?.active == true {
                                Label("Notfall-Lockdown aktiv", systemImage: "hand.raised.fill").foregroundStyle(.red)
                            }
                            LabeledContent("Datenwege", value: "\(snapshot.overview.pairs.enabled) aktiv / \(snapshot.overview.pairs.total) gesamt")
                            LabeledContent("Letzte erfolgreiche Sicherung", value: AppFormat.date(snapshot.overview.jobs.lastSuccess?.startedAt))
                            LabeledContent("Automatik", value: !snapshot.overview.services.scheduler.configuredEnabled ? "Deaktiviert"
                                : snapshot.overview.services.scheduler.control?.paused == true ? "Pausiert" : "Aktiv")
                            LabeledContent("Sicherungslauf", value: snapshot.progress.map { $0.running ? "Läuft" : "Kein aktiver Lauf" } ?? "Unbekannt")
                            LabeledContent("PBS-Lauf", value: snapshot.pbs.map { $0.running ? "Läuft" : "Kein aktiver Lauf" } ?? "Unbekannt")
                        }
                        Section("Speicher & System") {
                            LabeledContent("Hostname", value: snapshot.overview.system.hostname)
                            LabeledContent("Datenbereich frei", value: AppFormat.bytes(snapshot.overview.system.dataDisk.freeBytes))
                            if let percent = snapshot.overview.system.dataDisk.percentUsed {
                                LabeledContent("Datenbereich belegt", value: "\(Int(percent.rounded())) %")
                            }
                            LabeledContent("Arbeitsspeicher verfügbar", value: AppFormat.bytes(snapshot.overview.system.memory.availableBytes))
                            ForEach(snapshot.storage?.pairs ?? []) { pair in
                                if let disk = pair.localDisk {
                                    LabeledContent("\(pair.name) · lokal frei", value: AppFormat.bytes(disk.freeBytes))
                                }
                            }
                        }
                        Section("Warnungen & Befunde") {
                            if snapshot.incidents.isEmpty {
                                Text(snapshot.missing.isEmpty ? "Keine aktuellen Befunde gemeldet." : "Befunde sind noch nicht vollständig verfügbar.")
                                    .foregroundStyle(.secondary)
                            }
                            ForEach(snapshot.incidents) { incident in
                                Label(incident.message, systemImage: incident.symbol)
                                    .foregroundStyle(incident.color)
                            }
                        }
                        Section("Datenwege") {
                            ForEach(snapshot.overview.pairs.health) { pair in
                                VStack(alignment: .leading, spacing: 4) {
                                    Text(pair.name).font(.headline)
                                    Text(pair.overdue == true ? "Überfällig" : pair.lastStatus.map { StatusStyle.label(for: $0) } ?? "Noch kein Ergebnis")
                                        .font(.subheadline).foregroundStyle(.secondary)
                                    Text("Letzter Lauf: \(AppFormat.date(pair.lastRun))").font(.caption).foregroundStyle(.secondary)
                                }
                            }
                        }
                    }
                } else {
                    ContentUnavailableView("Profil entfernt", systemImage: "server.rack")
                }
            }
        }
        .sicherpfadGlassScreen()
        .navigationTitle("Serverdetails")
        .navigationBarTitleDisplayMode(.inline)
        .refreshable { await refresh() }
        .task(id: scenePhase) {
            guard scenePhase == .active else { return }
            repeat {
                await refresh()
                do { try await Task.sleep(for: .seconds(30)) } catch { return }
            } while !Task.isCancelled
        }
    }
}

private struct ServerOverviewLoginView: View {
    @EnvironmentObject private var app: AppModel
    @Environment(\.dismiss) private var dismiss
    @ObservedObject var store: ServerOverviewModel
    private let savedAddress: String?
    @State private var address: String
    @State private var username: String
    @State private var password = ""
    @State private var rememberSession = true
    @State private var allowHTTP = false
    @State private var task: Task<Void, Never>?
    @State private var validationError: String?

    init(store: ServerOverviewModel, profile: SavedServerProfile?) {
        self.store = store
        self.savedAddress = profile?.address
        _address = State(initialValue: profile?.address ?? "")
        _username = State(initialValue: profile?.username ?? "admin")
    }

    private var target: ServerLoginTarget? {
        try? ServerLoginTarget(address: address, persistedAddress: savedAddress)
    }

    var body: some View {
        NavigationStack {
            Form {
                Section("Server") {
                    TextField("https://server.example", text: $address)
                        .textContentType(.URL).keyboardType(.URL).textInputAutocapitalization(.never)
                        .autocorrectionDisabled().accessibilityLabel("Serveradresse")
                    TextField("Benutzername", text: $username).textContentType(.username)
                        .textInputAutocapitalization(.never).autocorrectionDisabled()
                }
                .disabled(store.isConnecting)
                Section("Anmeldung") {
                    SecureField("Passwort", text: $password).textContentType(.password)
                    Button("Mit Passwort verbinden") { connect(method: nil) }
                        .disabled(store.isConnecting || target == nil || username.isEmpty || password.isEmpty)
                    if target?.supportsPasskeys == true {
                        Button("Mit Passkey verbinden") { connect(method: "passkey") }
                        Button("Mit Sicherheitsschlüssel verbinden") { connect(method: "security_key") }
                    }
                }
                .disabled(store.isConnecting)
                Section {
                    Toggle("Angemeldet bleiben", isOn: $rememberSession)
                    if target?.usesHTTPS == false {
                        Toggle("Unverschlüsselte HTTP-Verbindung erlauben", isOn: $allowHTTP)
                    }
                } footer: {
                    Text(target?.usesHTTPS == false
                        ? "HTTP überträgt Zugangsdaten unverschlüsselt. Diese Sitzung bleibt nur bis zum Beenden oder Abmelden in der App verfügbar."
                        : "Die Sitzung kann geschützt auf diesem Gerät gespeichert werden. Das Passwort wird nicht gespeichert. Deine aktive Arbeitsverbindung bleibt erhalten.")
                }
                .disabled(store.isConnecting)
                if store.isConnecting { ProgressView("Server wird verbunden …") }
                if let error = validationError ?? store.connectionError {
                    Section { Text(error).foregroundStyle(.red).accessibilityIdentifier("serverOverviewConnectionError") }
                }
            }
            .navigationTitle("Server verbinden")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Abbrechen") {
                        task?.cancel()
                        store.cancelConnection()
                        password = ""
                        dismiss()
                    }
                }
            }
            .interactiveDismissDisabled(store.isConnecting)
            .onChange(of: address) { _, _ in allowHTTP = false }
            .onDisappear { task?.cancel(); store.cancelConnection(); password = "" }
        }
    }

    private func connect(method: String?) {
        validationError = nil
        guard let target else { validationError = "Gib eine gültige Serveradresse ein."; return }
        task = Task {
            let connected = await store.connect(target: target, username: username, password: method == nil ? password : nil,
                passkeyMethod: method, rememberSession: rememberSession, insecureTransportConfirmed: allowHTTP,
                activeAddress: app.phase == .signedIn ? app.serverAddress : "",
                prepareConnection: { await app.prepareOverviewConnection(url: target.url) }) { url, username, remember in
                    app.rememberOverviewServer(url: url, username: username, rememberSession: remember)
                }
            password = ""
            if connected { dismiss() }
        }
    }
}
