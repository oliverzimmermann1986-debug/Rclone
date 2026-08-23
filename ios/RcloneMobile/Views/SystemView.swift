import SwiftUI

struct SystemView: View {
    @EnvironmentObject private var model: AppModel
    @Binding var showingSettings: Bool
    @State private var showPauseOptions = false
    @State private var confirmResume = false

    var body: some View {
        List {
            if let system = model.overview?.system {
                Section("Server") {
                    LabeledContent("Hostname", value: system.hostname)
                    LabeledContent("Umgebung", value: system.virtualization)
                    LabeledContent("Adresse", value: system.addresses.first ?? "–")
                    LabeledContent("Laufzeit", value: AppFormat.elapsed(system.uptimeSeconds))
                }
                Section("Auslastung") {
                    UsageRow(title: "CPU", value: system.cpu.loadPercent, detail: "\(system.cpu.capacity.formatted()) Kerne", symbol: "cpu")
                    UsageRow(title: "Arbeitsspeicher", value: system.memory.percentUsed, detail: "\(AppFormat.bytes(system.memory.usedBytes)) von \(AppFormat.bytes(system.memory.totalBytes))", symbol: "memorychip")
                    UsageRow(title: "Datenträger", value: system.dataDisk.percentUsed, detail: "\(AppFormat.bytes(system.dataDisk.freeBytes)) frei", symbol: "internaldrive")
                }
                schedulerSection
                Section("Spezialwerkzeuge") {
                    NavigationLink { PBSToolsView() } label: {
                        HStack {
                            Label("Proxmox Backup Server", systemImage: "shippingbox.and.arrow.backward")
                            Spacer()
                            StatusBadge(status: model.pbs?.running == true ? "running" : model.pbs?.enabled == true ? "ok" : nil)
                        }
                    }
                    NavigationLink { PushStatusView() } label: {
                        Label("Push-Mitteilungen", systemImage: "bell.badge")
                    }
                }
                diagnosticsSection
                Section("Software") {
                    LabeledContent("Rclone Sync", value: model.overview?.app.version ?? "–")
                    LabeledContent("Python", value: system.python)
                    LabeledContent("Kernel", value: system.kernel)
                }
            } else {
                switch model.overviewState {
                case let .failed(message):
                    LoadFailureView(title: "Systemdaten nicht geladen", message: message) {
                        Task { await model.refresh() }
                    }
                default:
                    LoadingSection(label: "Systemdaten werden geladen …")
                }
            }
        }
        .navigationTitle("System")
        .toolbar {
            ToolbarItem(placement: .topBarLeading) { SettingsButton(showingSettings: $showingSettings) }
            ToolbarItem(placement: .topBarTrailing) {
                Button { Task { await model.refresh() } } label: { Image(systemName: "arrow.clockwise") }
                    .accessibilityLabel("Aktualisieren")
            }
        }
        .refreshable { await model.refresh() }
        .confirmationDialog("Zeitpläne pausieren", isPresented: $showPauseOptions, titleVisibility: .visible) {
            Button("1 Stunde") { Task { await model.pauseScheduler(minutes: 60) } }
            Button("4 Stunden") { Task { await model.pauseScheduler(minutes: 240) } }
            Button("24 Stunden") { Task { await model.pauseScheduler(minutes: 1_440) } }
            Button("Abbrechen", role: .cancel) {}
        } message: {
            Text("Manuelle Sicherungen bleiben weiterhin möglich.")
        }
        .confirmationDialog("Zeitpläne fortsetzen?", isPresented: $confirmResume, titleVisibility: .visible) {
            Button("Fortsetzen") { Task { await model.resumeScheduler() } }
            Button("Abbrechen", role: .cancel) {}
        }
    }

    private var schedulerSection: some View {
        Section("Zeitpläne") {
            let paused = model.overview?.services.scheduler.control?.paused == true
            HStack {
                Label(paused ? "Pausiert" : "Aktiv", systemImage: paused ? "pause.circle.fill" : "calendar.badge.clock")
                Spacer()
                StatusBadge(status: paused ? "warning" : model.overview?.services.scheduler.active)
            }
            if paused {
                Button("Zeitpläne fortsetzen") { confirmResume = true }
            } else {
                Button("Wartungsfenster starten") { showPauseOptions = true }
            }
        }
    }

    private var diagnosticsSection: some View {
        Section("Diagnose") {
            if let doctor = model.doctor {
                HStack {
                    Label(doctor.ok ? "Alle Prüfungen bestanden" : "Hinweise gefunden", systemImage: doctor.ok ? "checkmark.shield.fill" : "stethoscope")
                    Spacer()
                    StatusBadge(status: doctor.level)
                }
                ForEach(doctor.checks.prefix(6)) { check in
                    VStack(alignment: .leading, spacing: 3) {
                        Text(check.name ?? check.title ?? "Prüfung").font(.subheadline.weight(.semibold))
                        if let message = check.message ?? check.detail { Text(message).font(.caption).foregroundStyle(.secondary) }
                    }
                }
            }
            if let checkedAt = model.doctorLastCheckedAt {
                LabeledContent(
                    "Zuletzt geprüft",
                    value: AppFormat.date(checkedAt.timeIntervalSince1970)
                )
                .font(.caption)
                .foregroundStyle(.secondary)
            }
            Button { Task { await model.refreshDoctor() } } label: {
                HStack(spacing: 10) {
                    if model.doctorIsRefreshing { ProgressView() }
                    Label(
                        model.doctor == nil ? "Systemdiagnose ausführen" : "Erneut prüfen",
                        systemImage: "stethoscope"
                    )
                }
            }
            .disabled(model.doctorIsRefreshing)
            .accessibilityIdentifier("refreshDoctorButton")
            NavigationLink { OperationsHubView() } label: {
                Label("Betrieb & Wartung", systemImage: "wrench.and.screwdriver")
            }
        }
    }
}

private struct PushStatusView: View {
    @EnvironmentObject private var model: AppModel
    @State private var status: PushStatus?
    @State private var isLoading = false
    @State private var isTesting = false
    @State private var errorMessage: String?

    var body: some View {
        List {
            Section("Dieses iPhone") {
                Button {
                    NotificationCenter.default.post(name: .pushAuthorizationRequested, object: nil)
                } label: {
                    Label("Mitteilungen aktivieren oder prüfen", systemImage: "bell.badge")
                }
                .accessibilityHint("Zeigt zuerst, welche Fehler gemeldet werden, und fragt danach nach der iOS-Berechtigung.")
            } footer: {
                Text("Rclone Sync informiert nur über Sicherungs- und Prüfprobleme, nicht über erfolgreiche Läufe.")
            }
            if let status {
                Section("Bereitschaft") {
                    LabeledContent("APNs-Konfiguration") {
                        StatusBadge(status: status.configured ? "ok" : "warning")
                    }
                    LabeledContent("Registrierte Geräte", value: "\(status.registeredDevices)")
                    LabeledContent("Gerätebindung", value: "\(status.deviceLeaseDays) Tage")
                    if !status.configured {
                        Label("APNs ist auf dem Server nicht vollständig eingerichtet.", systemImage: "exclamationmark.triangle")
                            .font(.caption)
                            .foregroundStyle(.orange)
                    } else if status.registeredDevices == 0 {
                        Label("Kein aktives iPhone ist registriert.", systemImage: "iphone")
                            .font(.caption)
                            .foregroundStyle(.orange)
                    }
                }
                Section("Zustellung") {
                    LabeledContent("Ausstehend", value: "\(status.outbox.pending)")
                    LabeledContent("Zugestellt", value: "\(status.outbox.sent)")
                    LabeledContent("Endgültig fehlgeschlagen", value: "\(status.outbox.failed)")
                    if let lastError = status.outbox.lastError, !lastError.isEmpty {
                        VStack(alignment: .leading, spacing: 4) {
                            Label("Letzter Zustellfehler", systemImage: "exclamationmark.triangle")
                                .font(.subheadline.weight(.semibold))
                            Text(lastError).font(.caption).textSelection(.enabled)
                            if let timestamp = status.outbox.lastErrorAt {
                                Text(AppFormat.date(timestamp)).font(.caption2).foregroundStyle(.secondary)
                            }
                        }
                        .foregroundStyle(.orange)
                    }
                }
                Section("Fehlerereignisse") {
                    if status.events.isEmpty {
                        Text("Keine Ereignisse aktiviert").foregroundStyle(.secondary)
                    } else {
                        ForEach(status.events, id: \.self) { event in
                            Label(eventLabel(event), systemImage: "exclamationmark.bubble")
                        }
                    }
                }
                Section {
                    Button { Task { await sendTest() } } label: {
                        HStack(spacing: 10) {
                            if isTesting { ProgressView() }
                            Label("Testmitteilung senden", systemImage: "paperplane")
                        }
                    }
                    .disabled(isTesting || !status.configured || status.registeredDevices == 0)
                } footer: {
                    Text("Der Test prüft die echte Zustellung über APNs bis zu einem registrierten Gerät.")
                }
            } else if isLoading {
                LoadingSection(label: "Push-Status wird geladen …")
            } else if let errorMessage {
                LoadFailureView(title: "Push-Status nicht geladen", message: errorMessage) {
                    Task { await load() }
                }
            }
        }
        .navigationTitle("Push-Mitteilungen")
        .navigationBarTitleDisplayMode(.inline)
        .refreshable { await load() }
        .task { await load() }
    }

    private func load() async {
        isLoading = true
        defer { isLoading = false }
        do {
            status = try await model.withCurrentClient { try await $0.getPushStatus() }
            errorMessage = nil
        } catch is CancellationError {
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func sendTest() async {
        isTesting = true
        defer { isTesting = false }
        do {
            let response = try await model.withCurrentClient { try await $0.testPushNotification() }
            guard response.ok else {
                errorMessage = response.error ?? "Die Testmitteilung konnte nicht gesendet werden."
                return
            }
            model.actionMessage = "Testmitteilung wurde über APNs zugestellt."
            await load()
        } catch is CancellationError {
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func eventLabel(_ event: String) -> String {
        switch event {
        case "sync_error": "Sicherungsfehler"
        case "check_error": "Prüffehler"
        case "restore_test_error": "Restore-Test fehlgeschlagen"
        case "pbs_error": "PBS-Fehler"
        default: event
        }
    }
}

private struct PBSToolsView: View {
    @EnvironmentObject private var model: AppModel
    @State private var selectedTarget: PBSTarget?
    @State private var confirmAll = false
    @State private var confirmCancel = false

    var body: some View {
        List {
            if let pbs = model.pbs {
                if case let .failed(message) = model.pbsState {
                    Section {
                        LoadFailureView(title: "PBS-Status nicht aktualisiert", message: message) {
                            Task { await model.refresh() }
                        }
                    }
                }
                Section("Verbindung") {
                    LabeledContent("Status") { StatusBadge(status: pbs.enabled && pbs.clientAvailable ? "ok" : "warning") }
                    LabeledContent("Repository", value: pbs.repository.isEmpty ? "Nicht eingerichtet" : pbs.repository)
                    if !pbs.namespace.isEmpty { LabeledContent("Namespace", value: pbs.namespace) }
                }
                Section("Targets") {
                    if pbs.targets.isEmpty {
                        ContentUnavailableView("Keine PBS-Targets", systemImage: "shippingbox", description: Text("Eingerichtete Ziele erscheinen hier."))
                    } else {
                        ForEach(pbs.targets) { target in
                            Button { selectedTarget = target } label: {
                                VStack(alignment: .leading, spacing: 5) {
                                    Text(target.name).font(.headline).foregroundStyle(.primary)
                                    Text(target.paths.joined(separator: " · ")).font(.caption).foregroundStyle(.secondary).lineLimit(2)
                                    Text("Letzter Erfolg: \(AppFormat.relative(target.lastSuccess))").font(.caption).foregroundStyle(.secondary)
                                }
                            }
                            .disabled(!pbs.enabled || !pbs.clientAvailable || pbs.running)
                        }
                    }
                }
                Section {
                    if pbs.running {
                        Button("PBS-Lauf abbrechen", role: .destructive) { confirmCancel = true }
                    } else {
                        Button("Alle Targets sichern") { confirmAll = true }
                            .disabled(!pbs.enabled || !pbs.clientAvailable || pbs.targets.isEmpty)
                    }
                }
            } else {
                switch model.pbsState {
                case let .failed(message):
                    Section {
                        LoadFailureView(title: "PBS-Status nicht geladen", message: message) {
                            Task { await model.refresh() }
                        }
                    }
                default:
                    LoadingSection(label: "PBS-Status wird geladen …")
                }
            }
            Section("Konfiguration") {
                NavigationLink { PBSConfigurationView() } label: {
                    Label("PBS konfigurieren", systemImage: "slider.horizontal.3")
                }
            }
        }
        .navigationTitle("PBS")
        .navigationBarTitleDisplayMode(.inline)
        .refreshable { await model.refresh() }
        .confirmationDialog("PBS-Target sichern?", isPresented: Binding(
            get: { selectedTarget != nil },
            set: { if !$0 { selectedTarget = nil } }
        ), titleVisibility: .visible) {
            Button("Sicherung starten") {
                let name = selectedTarget?.name
                selectedTarget = nil
                Task { await model.runPBS(target: name) }
            }
            Button("Abbrechen", role: .cancel) { selectedTarget = nil }
        }
        .confirmationDialog("Alle PBS-Targets sichern?", isPresented: $confirmAll, titleVisibility: .visible) {
            Button("Sicherung starten") { Task { await model.runPBS(target: nil) } }
            Button("Abbrechen", role: .cancel) {}
        }
        .confirmationDialog("PBS-Lauf abbrechen?", isPresented: $confirmCancel, titleVisibility: .visible) {
            Button("Abbruch anfordern", role: .destructive) { Task { await model.cancelPBS() } }
            Button("Weiterlaufen lassen", role: .cancel) {}
        }
    }
}

private struct UsageRow: View {
    let title: String
    let value: Double?
    let detail: String
    let symbol: String

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Label(title, systemImage: symbol)
                Spacer()
                Text(value.map { "\($0.formatted(.number.precision(.fractionLength(0)))) %" } ?? "Nicht verfügbar")
            }
            if let value {
                ProgressView(value: min(max(value, 0), 100), total: 100)
                    .tint(value >= 90 ? .red : value >= 75 ? .orange : .green)
                Text(detail).font(.caption).foregroundStyle(.secondary)
            } else {
                Text("Messwert nicht vom Server geliefert")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 4)
        .accessibilityElement(children: .combine)
    }
}
