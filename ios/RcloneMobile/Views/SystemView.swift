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
                    UsageRow(title: "Arbeitsspeicher", value: system.memory.percentUsed ?? 0, detail: "\(AppFormat.bytes(system.memory.usedBytes)) von \(AppFormat.bytes(system.memory.totalBytes))", symbol: "memorychip")
                    UsageRow(title: "Datenträger", value: system.dataDisk.percentUsed ?? 0, detail: "\(AppFormat.bytes(system.dataDisk.freeBytes)) frei", symbol: "internaldrive")
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
                }
                diagnosticsSection
                Section("Software") {
                    LabeledContent("Rclone Sync", value: model.overview?.app.version ?? "–")
                    LabeledContent("Python", value: system.python)
                    LabeledContent("Kernel", value: system.kernel)
                }
            } else {
                LoadingSection(label: "Systemdaten werden geladen …")
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
            } else {
                Button { Task { await model.refreshDoctor() } } label: { Label("Systemdiagnose ausführen", systemImage: "stethoscope") }
            }
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
                Section("Verbindung") {
                    LabeledContent("Status") { StatusBadge(status: pbs.enabled && pbs.clientAvailable ? "ok" : "warning") }
                    LabeledContent("Repository", value: pbs.repository.isEmpty ? "Nicht eingerichtet" : pbs.repository)
                    if !pbs.namespace.isEmpty { LabeledContent("Namespace", value: pbs.namespace) }
                }
                Section("Targets") {
                    if pbs.targets.isEmpty {
                        ContentUnavailableView("Keine PBS-Targets", systemImage: "shippingbox", description: Text("Targets werden in den erweiterten Einstellungen des Web-Frontends eingerichtet."))
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
                LoadingSection(label: "PBS-Status wird geladen …")
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
    let value: Double
    let detail: String
    let symbol: String

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Label(title, systemImage: symbol)
                Spacer()
                Text("\(value.formatted(.number.precision(.fractionLength(0)))) %")
            }
            ProgressView(value: min(max(value, 0), 100), total: 100)
                .tint(value >= 90 ? .red : value >= 75 ? .orange : .teal)
            Text(detail).font(.caption).foregroundStyle(.secondary)
        }
        .padding(.vertical, 4)
        .accessibilityElement(children: .combine)
    }
}
