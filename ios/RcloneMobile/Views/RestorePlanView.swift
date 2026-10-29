import SwiftUI

struct RestorePlanView: View {
    @EnvironmentObject private var model: AppModel
    @State private var draft = RestorePlanDraft()
    @State private var preview: RestorePlanResponse?
    @State private var rhythm: RestorePlanRhythm = .custom
    @State private var time = Date()
    @State private var weekday = 0
    @State private var isLoading = false
    @State private var isPreviewing = false
    @State private var isSaving = false
    @State private var error: String?
    @State private var confirmSave = false
    @State private var confirmReload = false
    @State private var confirmDiscard = false
    @State private var confirmationContext: String?
    @State private var loadID = UUID()
    @State private var previewReceivedAt = ProcessInfo.processInfo.systemUptime
    @State private var planReceivedAt = ProcessInfo.processInfo.systemUptime

    private var plan: RestorePlanResponse? { draft.plan }
    private var settings: RestorePlanSettings { draft.settings }
    private var previewSettings: RestorePlanSettings? { draft.previewSettings }

    private var currentContext: String {
        "\(model.phase)|\(model.serverAddress)|\(model.savedUsername)|\(model.client.map { String(describing: ObjectIdentifier($0)) } ?? "demo")"
    }

    var body: some View {
        Form {
            if model.isDemoMode {
                Section {
                    Text("Verbinde dich mit deinem Server, um den Prüfplan zu sehen und zu ändern.")
                }
            } else if let plan {
                editor(plan).disabled(isSaving || isLoading)
                conflictSection
                previewSection
                Section {
                    Button(isSaving ? "Speichert …" : "Prüfplan übernehmen") {
                        confirmationContext = currentContext
                        confirmSave = true
                    }
                        .disabled(isSaving || isLoading || isPreviewing || draft.context != currentContext || !draft.canSave)
                        .accessibilityIdentifier("saveRestorePlan")
                } footer: {
                    Text("Die Übernahme ändert nur den automatischen Restore-Prüfplan für alle aktiven Datenwege. Sicherungsjobs und vorhandene Nachweise bleiben erhalten. Termine bestätigen keinen erfolgreichen Restore.")
                }
            } else if isLoading {
                LoadingSection(label: "Prüfplan wird geladen …")
            }
            if let error {
                Section {
                    Label(error, systemImage: "exclamationmark.triangle")
                        .foregroundStyle(.orange)
                    Button("Plan neu laden") { requestReload() }.disabled(isSaving || isLoading)
                }
            }
        }
        .navigationTitle("Restore-Prüfplan")
        .navigationBarTitleDisplayMode(.inline)
        .environment(\.timeZone, TimeZone(identifier: plan?.timezone ?? "Europe/Berlin") ?? .current)
        .task(id: currentContext) {
            if draft.enterContext(currentContext) {
                preview = nil; error = nil
                confirmSave = false; confirmReload = false; confirmDiscard = false; confirmationContext = nil
                loadID = UUID(); isSaving = false; isPreviewing = false; isLoading = false
                await load()
            } else if plan == nil, !isLoading, !isSaving, !isPreviewing {
                await load()
            }
        }
        .task(id: settings) { await updatePreview() }
        .confirmationDialog("Prüfplan übernehmen?", isPresented: $confirmSave, titleVisibility: .visible) {
            Button("Für alle aktiven Datenwege übernehmen") {
                let context = confirmationContext
                Task { await save(confirmedContext: context) }
            }
            Button("Abbrechen", role: .cancel) {}
        } message: {
            Text("Zeitplan: \(settings.schedule) (\(plan?.timezone ?? ""))\n\(settings.sampleFiles) Dateien, höchstens \(settings.maxTotalMB) MiB je Datenweg.\nBetroffen: \(preview?.dataPaths.map(\.name).joined(separator: ", ") ?? "")")
        }
        .confirmationDialog("Aktuellen Serverstand laden?", isPresented: $confirmReload, titleVisibility: .visible) {
            Button("Serverstand laden, Entwurf behalten") {
                let context = confirmationContext
                Task { if context == currentContext { await load() } }
            }
            Button("Abbrechen", role: .cancel) {}
        } message: {
            Text("Deine Änderungen bleiben im Formular. Anschließend kannst du sie ausdrücklich auf den aktuellen Serverstand übertragen oder verwerfen.")
        }
        .confirmationDialog("Eigene Änderungen verwerfen?", isPresented: $confirmDiscard, titleVisibility: .visible) {
            Button("Serverwerte verwenden", role: .destructive) {
                guard confirmationContext == currentContext, draft.discardChanges(context: currentContext) else { return }
                preview = plan; error = nil
                previewReceivedAt = planReceivedAt
                syncScheduleControls()
            }
            Button("Abbrechen", role: .cancel) {}
        } message: {
            Text("Der geladene Serverstand ersetzt deine ungesicherten Änderungen. Es wird nichts auf dem Server gespeichert.")
        }
    }

    @ViewBuilder private var conflictSection: some View {
        if draft.needsResolution {
            Section("Entwurf abgleichen") {
                Text("Deine Änderungen sind erhalten. Speichern bleibt gesperrt, bis du den aktuellen Serverstand abgeglichen und eine neue Vorschau geprüft hast.")
                if let latest = draft.latestServerPlan {
                    LabeledContent("Server: Automatisch prüfen", value: latest.settings.enabled ? "Ein" : "Aus")
                    LabeledContent("Server: Zeitplan", value: "\(latest.settings.schedule) (\(latest.timezone))")
                    LabeledContent("Server: Dateien je Datenweg", value: "\(latest.settings.sampleFiles)")
                    LabeledContent("Server: Datenlimit", value: "\(latest.settings.maxTotalMB) MiB")
                    LabeledContent("Server: Suchlimit", value: "\(latest.settings.maxScanFiles) Dateien")
                    Button("Meine Änderungen auf diesen Stand übertragen") {
                        guard !isLoading, !isSaving, draft.rebaseChanges(context: currentContext) else { return }
                        preview = nil; error = nil
                        syncScheduleControls()
                        Task { await updatePreview() }
                    }
                    Button("Eigene Änderungen verwerfen", role: .destructive) {
                        confirmationContext = currentContext
                        confirmDiscard = true
                    }
                } else {
                    Button("Aktuellen Serverstand laden") { requestReload() }
                }
            }
            .disabled(isLoading || isSaving)
        }
    }

    private func requestReload() {
        guard !isSaving, !isLoading else { return }
        if draft.isDirty {
            confirmationContext = currentContext
            confirmReload = true
        } else {
            Task { await load() }
        }
    }

    @ViewBuilder private func editor(_ plan: RestorePlanResponse) -> some View {
        Section {
            Toggle("Automatisch prüfen", isOn: $draft.settings.enabled)
            Picker("Rhythmus", selection: $rhythm) {
                ForEach(RestorePlanRhythm.allCases) { Text($0.title).tag($0) }
            }
            .onChange(of: rhythm) { _, _ in updateExpression() }
            if rhythm != .custom {
                DatePicker("Uhrzeit", selection: $time, displayedComponents: .hourAndMinute)
                    .onChange(of: time) { _, _ in updateExpression() }
                if rhythm == .weekly {
                    Picker("Wochentag", selection: $weekday) {
                        ForEach(0..<7) { index in
                            Text(["Sonntag", "Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag"][index]).tag(index)
                        }
                    }.onChange(of: weekday) { _, _ in updateExpression() }
                }
            } else {
                TextField("Zeitplan (Cron)", text: $draft.settings.schedule)
                    .textInputAutocapitalization(.never).autocorrectionDisabled()
            }
            LabeledContent("Zeitzone", value: plan.timezone)
            Stepper("\(settings.sampleFiles) Dateien je Datenweg", value: $draft.settings.sampleFiles, in: 1...500)
            LabeledContent("Datenlimit je Datenweg") {
                TextField("MiB", value: $draft.settings.maxTotalMB, format: .number)
                    .keyboardType(.numberPad).multilineTextAlignment(.trailing)
                    .accessibilityLabel("Datenlimit in MiB je Datenweg")
            }
        } header: {
            Text("Automatische Stichproben")
        } footer: {
            Text("Ein vollständiger erfolgreicher Nachweis gilt sieben Tage. Sonntag und Mittwoch lassen Reserve für Laufzeit und Zeitumstellung. Das Datenlimit gilt in MiB; mehr Daten können Cloud-Kosten verursachen.")
        }
    }

    @ViewBuilder private var previewSection: some View {
        Section("Vorschau") {
            if isPreviewing { ProgressView("Prüft Termine und Nachweisablauf …") }
            if let preview, previewSettings == settings {
                if preview.dueNow { Label("Automatische Prüfung ist bereits fällig", systemImage: "clock.badge.exclamationmark") }
                ForEach(Array(preview.nextRuns.enumerated()), id: \.offset) { index, stamp in
                    LabeledContent("Termin \(index + 1)", value: planDate(stamp))
                }
                if preview.nextRuns.isEmpty { Text("Keine automatischen Prüftermine") }
                ForEach(preview.warnings, id: \.self) { warning in
                    Label(warning, systemImage: "exclamationmark.triangle").foregroundStyle(.orange)
                }
                ForEach(preview.dataPaths) { path in
                    VStack(alignment: .leading, spacing: 4) {
                        Text(path.name).font(.headline)
                        Text(path.evidenceState == "passed" && (path.validUntil ?? 0) > preview.generatedAt + max(0, ProcessInfo.processInfo.systemUptime - previewReceivedAt)
                             ? "Nachweis gültig bis \(planDate(path.validUntil ?? 0))"
                             : "Kein aktuell gültiger Restore-Nachweis")
                            .font(.subheadline).foregroundStyle(.secondary)
                        if path.coverageGap {
                            Label("Nachweis fehlt oder Prüflücke", systemImage: "exclamationmark.triangle")
                                .font(.subheadline).foregroundStyle(.orange)
                        }
                    }
                }
            }
        }
    }

    private func updateExpression() {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: plan?.timezone ?? "Europe/Berlin") ?? .current
        let parts = calendar.dateComponents([.hour, .minute], from: time)
        if let expression = RestorePlanRhythm.expression(rhythm, hour: parts.hour ?? 5, minute: parts.minute ?? 0, weekday: weekday) {
            draft.settings.schedule = expression
        }
    }

    private func planDate(_ stamp: Double) -> String {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "de_DE")
        formatter.timeZone = TimeZone(identifier: plan?.timezone ?? "Europe/Berlin")
        formatter.dateStyle = .medium; formatter.timeStyle = .short
        return formatter.string(from: Date(timeIntervalSince1970: stamp))
    }

    private func load() async {
        guard !model.isDemoMode else { return }
        let context = currentContext
        let owner = UUID()
        loadID = owner
        isLoading = true
        draft.invalidatePreview()
        preview = nil
        isPreviewing = false
        defer { if loadID == owner, currentContext == context { isLoading = false } }
        do {
            let result = try await model.withCurrentClient { try await $0.getRestorePlan() }
            guard !Task.isCancelled, loadID == owner, currentContext == context else { return }
            guard draft.receive(result, context: context) else { return }
            preview = draft.needsResolution ? nil : result
            error = nil
            isPreviewing = false; previewReceivedAt = ProcessInfo.processInfo.systemUptime
            planReceivedAt = previewReceivedAt
            if !draft.needsResolution { syncScheduleControls() }
        } catch is CancellationError {} catch {
            if loadID == owner, currentContext == context { self.error = error.localizedDescription }
        }
    }

    private func syncScheduleControls() {
        let parts = settings.schedule.split(separator: " ").map(String.init)
        if parts.count == 5, let minute = Int(parts[0]), let hour = Int(parts[1]), parts[2] == "*", parts[3] == "*" {
            var calendar = Calendar(identifier: .gregorian)
            calendar.timeZone = TimeZone(identifier: plan?.timezone ?? "Europe/Berlin") ?? .current
            time = calendar.date(bySettingHour: hour, minute: minute, second: 0, of: Date()) ?? Date()
            if parts[4] == "*" { rhythm = .daily }
            else if parts[4] == "0,3" { rhythm = .twiceWeekly }
            else if let day = Int(parts[4]), (0...6).contains(day) { weekday = day; rhythm = .weekly }
            else { rhythm = .custom }
        } else { rhythm = .custom }
    }

    private func updatePreview() async {
        guard let plan, !isSaving, !isLoading, !draft.needsResolution, draft.context == currentContext else { return }
        let context = currentContext
        let owner = loadID
        let previewDraft = settings
        if previewDraft == plan.settings {
            _ = draft.acceptPreview(plan, settings: previewDraft, revision: plan.revision, context: context)
            preview = plan; isPreviewing = false; error = nil
            previewReceivedAt = planReceivedAt
            return
        }
        isPreviewing = true
        draft.invalidatePreview()
        do {
            try await Task.sleep(for: .milliseconds(350))
            let result = try await model.withCurrentClient {
                try await $0.previewRestorePlan(RestorePlanUpdate(revision: plan.revision, settings: previewDraft))
            }
            guard !Task.isCancelled, settings == previewDraft, loadID == owner,
                  currentContext == context, self.plan?.revision == plan.revision,
                  draft.acceptPreview(result, settings: previewDraft, revision: plan.revision, context: context) else { return }
            preview = result; error = nil
            previewReceivedAt = ProcessInfo.processInfo.systemUptime
        } catch is CancellationError {} catch {
            if !Task.isCancelled, settings == previewDraft, loadID == owner, currentContext == context {
                self.error = error.localizedDescription
                if case APIError.server(status: 409, message: _) = error {
                    self.draft.markConflict(context: context)
                    preview = nil
                }
            }
        }
        if !Task.isCancelled, settings == previewDraft, loadID == owner, currentContext == context { isPreviewing = false }
    }

    private func save(confirmedContext: String?) async {
        guard confirmedContext == currentContext, !isSaving, !isLoading, !isPreviewing,
              let request = draft.saveRequest(context: currentContext) else { return }
        let context = currentContext
        let owner = loadID
        isSaving = true
        defer { if loadID == owner, currentContext == context { isSaving = false } }
        do {
            let result = try await model.withCurrentClient {
                try await $0.saveRestorePlan(request)
            }
            guard loadID == owner, currentContext == context else { return }
            guard draft.acceptSaved(result, context: context) else { return }
            preview = result; error = nil
            previewReceivedAt = ProcessInfo.processInfo.systemUptime
            planReceivedAt = previewReceivedAt
            await model.refresh(reloadConfig: true)
        } catch is CancellationError {} catch {
            if loadID == owner, currentContext == context {
                self.error = error.localizedDescription
                if case APIError.server(status: 409, message: _) = error {
                    draft.markConflict(context: context)
                    preview = nil
                }
            }
        }
    }
}
