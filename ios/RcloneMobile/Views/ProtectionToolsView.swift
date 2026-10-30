import SwiftUI
import Charts

struct ProtectionToolsView: View {
    var body: some View {
        List {
            Section("Wiederfinden und erweitern") {
                NavigationLink { VersionFileSearchView() } label: {
                    Label("Datei in Sicherungsständen finden", systemImage: "doc.text.magnifyingglass")
                }
                NavigationLink { CapacityForecastView() } label: {
                    Label("Speicherentwicklung", systemImage: "chart.line.uptrend.xyaxis")
                }
                NavigationLink { TargetMigrationView() } label: {
                    Label("Sicherungsziel wechseln", systemImage: "arrow.triangle.2.circlepath")
                }
            }
            Section("Schutz im Blick behalten") {
                NavigationLink { HeartbeatToolsView() } label: {
                    Label("Unabhängige Überwachung", systemImage: "heart.text.square")
                }
                NavigationLink { WeeklyProtectionReportsView() } label: {
                    Label("Wöchentliche Schutzberichte", systemImage: "doc.richtext")
                }
            }
        }
        .navigationTitle("Schutzwerkzeuge")
        .navigationBarTitleDisplayMode(.inline)
    }
}

private struct ToolFeedback: View {
    @ObservedObject var session: ProtectionToolsSession
    var body: some View {
        if session.isWorking { Section { ProgressView("Wird verarbeitet …") } }
        if let error = session.errorMessage {
            Section { Label(error, systemImage: "exclamationmark.triangle").foregroundStyle(.red).textSelection(.enabled) }
        }
        if let information = session.information {
            Section { Label(information, systemImage: "info.circle").foregroundStyle(.secondary) }
        }
    }
}

private struct ToolWarnings: View {
    let values: [String]
    var body: some View {
        if !values.isEmpty {
            Section("Hinweise") {
                ForEach(Array(values.enumerated()), id: \.offset) { _, value in
                    Label(value, systemImage: "exclamationmark.triangle").foregroundStyle(.orange)
                }
            }
        }
    }
}

private struct ToolDate: View {
    let title: String
    let value: Double?
    var body: some View { LabeledContent(title, value: value.map { AppFormat.date($0) } ?? "–") }
}

private struct ForecastMeasurementScope: View {
    let scope: String
    private var description: String {
        switch scope {
        case "filesystem": "Dateisystem: gemeinsame Belegung des Datenträgers."
        case "remote_account": "Anbieter-Konto: umfasst auch Daten außerhalb dieses Datenwegs."
        case "configured_target": "Zielordnerbudget: gemessener Bestand dieses Zielordners."
        default: "Messbereich noch unbekannt."
        }
    }
    var body: some View { Text(description).font(.caption).foregroundStyle(.secondary) }
}

private struct ProtectionToolPasswordSheet: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss
    let title: String
    let detail: String
    let action: String
    let destructive: Bool
    let binding: ProtectionToolServerBinding
    let request: (String) -> ProtectionToolRequest
    let onSuccess: (ProtectionToolDocument) -> Void
    @State private var password = ""
    @State private var working = false
    @State private var errorMessage: String?

    var body: some View {
        NavigationStack {
            Form {
                Section { Text(detail) }
                Section("Mit Serverpasswort bestätigen") {
                    SecureField("Aktuelles Serverpasswort", text: $password).textContentType(.password)
                    Button(action, role: destructive ? .destructive : nil) { Task { await confirm() } }
                        .disabled(password.isEmpty || working || !binding.matches(model))
                }
                if working { ProgressView("Wird verarbeitet …") }
                if let errorMessage { Text(errorMessage).foregroundStyle(.red) }
            }
            .navigationTitle(title)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Abbrechen") { password = ""; dismiss() }.disabled(working) } }
        }
    }

    private func confirm() async {
        guard binding.matches(model) else { return }
        working = true
        errorMessage = nil
        defer { working = false; password = "" }
        do {
            let response = try await model.withCurrentClient { try await $0.protectionTool(request(password)) }
            guard binding.matches(model) else { return }
            if response.bool("ok") == false {
                throw APIError.server(status: 400, message: response.string("error"))
            }
            onSuccess(response)
            dismiss()
        } catch is CancellationError {
            dismiss()
        } catch { errorMessage = error.localizedDescription }
    }
}

private struct VersionFileSearchView: View {
    @EnvironmentObject private var model: AppModel
    @StateObject private var session = ProtectionToolsSession()
    @State private var query = ""
    @State private var identity = ""
    @State private var results: [ProtectionToolDocument] = []
    @State private var nextCursor: String?
    @State private var didSearch = false
    var body: some View {
        List {
            Section("Sicherungsstände durchsuchen") {
                TextField("Dateiname oder Teil des Pfads", text: $query)
                    .textInputAutocapitalization(.never).autocorrectionDisabled().submitLabel(.search)
                    .onSubmit { Task { await search() } }
                Picker("Datenweg", selection: $identity) {
                    Text("Alle Datenwege").tag("")
                    ForEach(model.config?.backup.pairs ?? []) { Text($0.name).tag($0.id) }
                }
                Button("Datei suchen") { Task { await search() } }
                    .disabled(query.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || session.isWorking)
            }
            ToolFeedback(session: session)
            ToolWarnings(values: session["data"].strings("warnings"))
            if session["data"].bool("truncated") == true {
                Section { Label("Die Suche ist begrenzt. Grenze den Suchbegriff oder Datenweg weiter ein.", systemImage: "line.3.horizontal.decrease.circle") }
            }
            if didSearch {
                Section("\(results.count) Treffer") {
                    ForEach(Array(results.enumerated()), id: \.offset) { _, result in
                        if let binding = session.binding {
                            NavigationLink { VersionFileResultView(result: result, binding: binding) } label: {
                                VStack(alignment: .leading, spacing: 4) {
                                    Text(result.string("path")).font(.headline).lineLimit(3)
                                    Text("\(result.string("pair")) · \(result.string("point_label"))").font(.caption).foregroundStyle(.secondary)
                                    Text(result.string("verification") == "manifest_sha256" ? "SHA-256 im Manifest hinterlegt" : "Nur Dateimetadaten erfasst").font(.caption).foregroundStyle(.secondary)
                                }
                            }
                        }
                    }
                    if results.isEmpty { Text("Keine Datei gefunden.").foregroundStyle(.secondary) }
                    if nextCursor != nil { Button("Weitere Treffer laden") { Task { await search(more: true) } }.disabled(session.isWorking) }
                }
            }
        }
        .navigationTitle("Dateisuche")
        .navigationBarTitleDisplayMode(.inline)
        .task(id: model.serverAddress) { session.reset(for: model); results = []; nextCursor = nil; didSearch = false }
    }

    private func search(more: Bool = false) async {
        if let response = await session.call(.search(query: query.trimmingCharacters(in: .whitespacesAndNewlines), identity: identity, cursor: more ? nextCursor : nil), model: model) {
            let candidates = more ? results + response.objects("items") : response.objects("items")
            var seen = Set<String>()
            results = candidates.filter { seen.insert([$0.string("identity"), $0.string("point_id"), $0.string("path")].joined(separator: "\0")).inserted }
            nextCursor = response.string("next_cursor").isEmpty ? nil : response.string("next_cursor")
            didSearch = true
        }
    }
}

private struct VersionFileResultView: View {
    @EnvironmentObject private var model: AppModel
    @StateObject private var session = ProtectionToolsSession()
    let result: ProtectionToolDocument
    let binding: ProtectionToolServerBinding
    @State private var limitMB = 512
    @State private var confirmRestore = false
    var body: some View {
        Form {
            Section("Gefundene Datei") {
                Text(result.string("path")).textSelection(.enabled)
                LabeledContent("Datenweg", value: result.string("pair"))
                LabeledContent("Stand", value: result.string("point_label"))
                if let size = result.number("size_bytes") { LabeledContent("Größe", value: ProtectionToolLabels.bytes(size)) }
                Text(result.string("verification") == "manifest_sha256" ? "Der Stand enthält eine aufgezeichnete SHA-256. Die Wiederherstellung prüft die zurückgeholte Datei erneut." : "Für diesen Treffer sind nur Metadaten vorhanden. Das ist kein aktueller Prüfsummennachweis.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            if result.bool("restorable") == true {
                Section {
                    Stepper("Anfragelimit: \(limitMB) MB", value: $limitMB, in: 512...51_200, step: 512)
                    Button("Ins Staging zurückholen") { confirmRestore = true }
                        .disabled(session.isWorking || model.backupScopeIsRunning || !binding.matches(model))
                } footer: { Text("Die Datei wird getrennt zurückgeholt. Bestehende Quelldateien werden nicht ersetzt.") }
            } else { Section { Text("Dieser Treffer kann über den aktuellen Serververtrag nicht direkt zurückgeholt werden.") } }
            ToolFeedback(session: session)
        }
        .navigationTitle("Datei zurückholen")
        .navigationBarTitleDisplayMode(.inline)
        .task { if binding.matches(model) { session.reset(for: model) } }
        .confirmationDialog("Datei aus diesem Stand zurückholen?", isPresented: $confirmRestore, titleVisibility: .visible) {
            Button("In getrenntes Staging zurückholen") { Task { await restore() } }
            Button("Abbrechen", role: .cancel) {}
        } message: { Text("\(result.string("path")) aus \(result.string("point_label"))") }
    }

    private func restore() async {
        guard binding.matches(model) else { return }
        let request = result.object("restore_request")
        guard let path = request.strings("paths").first else { return }
        if let response = await session.call(.restoreFile(identity: request.string("identity"), pointID: request.string("point_id"), path: path, maxTotalMB: limitMB), model: model) {
            session.information = "Wiederherstellung gestartet. Den Fortschritt findest du unter Läufe."
            await model.refreshCurrentJobs()
            guard binding.matches(model) else { return }
            if let id = response.number("job_id"), let jobID = Int(exactly: id) { model.requestRunNavigation(id: jobID) }
        }
    }
}

private struct CapacityForecastView: View {
    @EnvironmentObject private var model: AppModel
    @StateObject private var session = ProtectionToolsSession()
    var body: some View {
        List {
            Section {
                Text("Die Prognose nutzt Tagesmessungen. Sie zeigt Dateisystem- oder Anbieter-Kontingente; Änderungen am Datenbestand können den Trend verschieben.")
                    .font(.subheadline).foregroundStyle(.secondary)
                Button("Jetzt messen") { Task { await sample() } }.disabled(session.isWorking)
                NavigationLink("Prognose und Zielkapazitäten einstellen") { CapacityForecastSettingsView() }
            }
            ToolFeedback(session: session)
            if session["data"].bool("enabled") == false { Section { Text("Automatische Speichermessung ist deaktiviert.") } }
            Section("Speicherziele") {
                ForEach(Array(session["data"].objects("items").enumerated()), id: \.offset) { _, item in
                    NavigationLink { CapacityHistoryView(item: item, binding: session.binding) } label: {
                        VStack(alignment: .leading, spacing: 5) {
                            Text(item.string("label")).font(.headline)
                            Text(ProtectionToolLabels.state(item.string("state")))
                                .foregroundStyle(["warning", "full", "stale", "error"].contains(item.string("state")) ? Color.orange : Color.secondary)
                            ForecastMeasurementScope(scope: item.string("measurement_scope"))
                            if let free = item.number("free_bytes") { Text("\(ProtectionToolLabels.bytes(free)) frei").font(.caption) }
                            if let days = item.number("days_to_full") { Text("Voraussichtlich noch \(days.formatted(.number.precision(.fractionLength(0...1)))) Tage").font(.caption) }
                        }
                        .accessibilityElement(children: .combine)
                    }
                }
                if session["data"].objects("items").isEmpty && !session.isWorking { Text("Noch keine Speicherziele verfügbar.").foregroundStyle(.secondary) }
            }
        }
        .navigationTitle("Speicherentwicklung")
        .navigationBarTitleDisplayMode(.inline)
        .task(id: model.serverAddress) { session.reset(for: model); await load() }
        .refreshable { await load() }
    }
    private func load() async { _ = await session.call(.forecasts, model: model) }
    private func sample() async {
        if let response = await session.call(.sampleForecasts(identity: nil), model: model) {
            let count = response.integer("sampled") ?? 0
            let pending = response.integer("skipped") ?? 0
            session.information = "\(count) Ziele gemessen.\(pending > 0 ? " \(pending) weitere Ziele folgen in den nächsten Messläufen." : "")"
        }
    }
}

private struct CapacityHistoryView: View {
    @EnvironmentObject private var model: AppModel
    @StateObject private var session = ProtectionToolsSession()
    let item: ProtectionToolDocument
    let binding: ProtectionToolServerBinding?
    @State private var showCapacity = false
    private var currentItem: ProtectionToolDocument { session["target"].objects("items").first ?? item }
    var body: some View {
        List {
            Section {
                Text(currentItem.string("label")).font(.headline)
                Text(ProtectionToolLabels.state(currentItem.string("state")))
                ForecastMeasurementScope(scope: currentItem.string("measurement_scope"))
                Text(currentItem.string("reason")).font(.caption).foregroundStyle(.secondary)
                ToolDate(title: "Letzte Messung", value: currentItem.number("observed_at"))
                ToolDate(title: "Voraussichtlich voll", value: currentItem.number("estimated_full_at"))
                if let growth = currentItem.number("growth_bytes_per_day") { LabeledContent("Wachstum pro Tag", value: ProtectionToolLabels.bytes(growth)) }
                if currentItem.string("kind") == "remote" { Button("Eigene Zielkapazität einstellen") { showCapacity = true }.disabled(session.isWorking || session["target"].string("_revision").isEmpty) }
            }
            if !session["data"].objects("items").isEmpty {
                Section("Gemessene Belegung") {
                    Chart {
                        ForEach(Array(session["data"].objects("items").enumerated()), id: \.offset) { _, row in
                            if let date = row.number("observed_at"), let used = row.number("used_bytes") {
                                LineMark(x: .value("Tag", Date(timeIntervalSince1970: date)), y: .value("Belegt", used / 1_073_741_824))
                            }
                        }
                    }
                    .chartYAxisLabel("GiB")
                    .frame(height: 220)
                    .accessibilityLabel("Gemessene Speicherbelegung in GiB")
                    .accessibilityValue("\(session["data"].objects("items").count) Tagesmessungen")
                }
            }
            ToolFeedback(session: session)
            Section("Tagesmessungen") {
                if session["data"].objects("items").isEmpty && !session.isWorking {
                    Text(currentItem.string("kind") == "remote" ? "Noch keine Tagesmessungen vorhanden. Ein Zielordnerbudget kannst du bereits oben einstellen." : "Noch keine Tagesmessungen für dieses Dateisystem vorhanden.").font(.caption).foregroundStyle(.secondary)
                }
                ForEach(Array(session["data"].objects("items").reversed().enumerated()), id: \.offset) { _, row in
                    VStack(alignment: .leading, spacing: 4) {
                        Text(row.number("observed_at").map { AppFormat.date($0) } ?? "–")
                        if let used = row.number("used_bytes") { Text("\(ProtectionToolLabels.bytes(used)) belegt").font(.caption) }
                        else { Text(row.string("reason")).font(.caption).foregroundStyle(.secondary) }
                    }
                }
            }
        }
        .navigationTitle("Messverlauf")
        .navigationBarTitleDisplayMode(.inline)
        .task {
            guard binding?.matches(model) == true else { return }
            session.reset(for: model); await load()
        }
        .sheet(isPresented: $showCapacity) {
            if let binding = session.binding {
                CapacityOverrideSheet(item: currentItem, revision: session["target"].string("_revision"), binding: binding) { Task { await load() } }
            }
        }
    }
    private func load() async {
        guard binding?.matches(model) == true else { return }
        _ = await session.call(.forecastTarget(identity: item.string("identity")), key: "target", model: model)
        _ = await session.call(.forecastHistory(identity: item.string("identity")), model: model)
    }
}

private struct CapacityOverrideSheet: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss
    let item: ProtectionToolDocument
    let revision: String
    let binding: ProtectionToolServerBinding
    let onSaved: () -> Void
    @State private var useProvider = true
    @State private var value = ""
    @State private var working = false
    @State private var errorMessage: String?
    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Text(item.string("label")).font(.headline)
                    Toggle("Anbieter-Kapazität verwenden", isOn: $useProvider)
                    if !useProvider { TextField("Eigene Kapazität in GiB", text: $value).keyboardType(.decimalPad) }
                } footer: { Text("Eine eigene Kapazität gilt als Budget dieses Zielordners; sein Bestand wird gemessen. Ohne eigene Kapazität verwendet die Prognose das Kontingent des Anbieter-Kontos.") }
                Section { Button("Kapazität speichern") { Task { await save() } }.disabled(working || !binding.matches(model)) }
                if working { ProgressView("Wird gespeichert …") }
                if let errorMessage { Text(errorMessage).foregroundStyle(.red) }
            }
            .navigationTitle("Zielkapazität")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Abbrechen") { dismiss() }.disabled(working) } }
            .task {
                useProvider = item.number("capacity_bytes") == nil
                value = item.number("capacity_bytes").map { String($0 / 1_073_741_824) } ?? ""
            }
        }
    }
    private func save() async {
        guard binding.matches(model) else { return }
        let capacity: Int64?
        if useProvider { capacity = nil }
        else {
            guard let gib = Double(value.replacingOccurrences(of: ",", with: ".")), gib.isFinite, gib > 0, gib <= 1_073_741_824 else { errorMessage = "Gib eine positive Kapazität in GiB ein."; return }
            capacity = Int64(exactly: (gib * 1_073_741_824).rounded())
        }
        working = true
        defer { working = false }
        do {
            _ = try await model.withCurrentClient { try await $0.protectionTool(.setCapacity(identity: item.string("identity"), capacityBytes: capacity, revision: revision)) }
            onSaved(); dismiss()
        } catch is CancellationError { dismiss() }
        catch { errorMessage = error.localizedDescription }
    }
}

private struct CapacityForecastSettingsView: View {
    @EnvironmentObject private var model: AppModel
    @StateObject private var session = ProtectionToolsSession()
    @State private var enabled = true
    @State private var interval = 24
    @State private var warning = 30
    @State private var retention = 180
    @State private var capacities: [String: String] = [:]
    @State private var pendingConfig: ProtectionToolDocument?
    @State private var showConfirmation = false
    var body: some View {
        Form {
            Section("Prognose") {
                Toggle("Automatische Tagesmessungen", isOn: $enabled)
                Stepper("Messintervall: \(interval) Stunden", value: $interval, in: 1...168)
                Stepper("Warnung ab weniger als \(warning) Tagen", value: $warning, in: 1...365)
                Stepper("Messverlauf: \(retention) Tage", value: $retention, in: 7...365)
            }
            Section {
                ForEach(Array(session["targets"].objects("items").filter { $0.string("kind") == "remote" }.enumerated()), id: \.offset) { _, target in
                    VStack(alignment: .leading, spacing: 4) {
                        Text(target.string("label"))
                        TextField("Eigene Kapazität in GiB (optional)", text: Binding(get: { capacities[target.string("identity")] ?? "" }, set: { capacities[target.string("identity")] = $0 }))
                            .keyboardType(.decimalPad)
                            .accessibilityLabel("Kapazität für \(target.string("label")) in GiB")
                    }
                }
            } header: { Text("Anbieter ohne Kontingentangabe") } footer: {
                Text("Leere Felder verwenden das Kontingent des Anbieter-Kontos. Eine eigene Kapazität gilt als Budget dieses Zielordners; dessen Bestand wird weiterhin gemessen.")
            }
            ToolFeedback(session: session)
            Section { Button("Einstellungen speichern") { prepare() }.disabled(session.isWorking || session["configuration"].values.isEmpty) }
        }
        .navigationTitle("Speicherprognose")
        .navigationBarTitleDisplayMode(.inline)
        .task(id: model.serverAddress) { session.reset(for: model); await load() }
        .sheet(isPresented: $showConfirmation) {
            if let config = pendingConfig, let binding = session.binding {
                ProtectionToolPasswordSheet(title: "Speicherprognose speichern", detail: "Messintervall, Aufbewahrung und ausdrücklich gesetzte Kapazitäten werden revisionsgebunden gespeichert.", action: "Einstellungen speichern", destructive: false, binding: binding, request: { .saveConfiguration(config, password: $0) }) { _ in
                    Task { await load() }
                }
            }
        }
    }
    private func load() async {
        guard let config = await session.call(.configuration, key: "configuration", model: model) else { return }
        _ = await session.call(.forecasts, key: "targets", model: model)
        let options = config.object("backup").object("storage_forecast")
        enabled = options.bool("enabled") ?? true
        interval = options.integer("sample_interval_hours") ?? 24
        warning = options.integer("warning_days") ?? 30
        retention = options.integer("history_days") ?? 180
        capacities = options.object("capacities").values.reduce(into: [:]) { values, row in
            if case let .number(bytes) = row.value { values[row.key] = String(bytes / 1_073_741_824) }
        }
    }
    private func prepare() {
        var config = session["configuration"]
        var backup = config.object("backup")
        var options = backup.object("storage_forecast")
        var limits = options.object("capacities").values
        for (key, value) in capacities {
            if value.trimmingCharacters(in: .whitespaces).isEmpty { limits.removeValue(forKey: key); continue }
            guard let gib = Double(value.replacingOccurrences(of: ",", with: ".")), gib.isFinite, gib > 0, gib < 8_000_000 else { session.errorMessage = "Gib eine positive Kapazität in GiB ein."; return }
            limits[key] = .number((gib * 1_073_741_824).rounded())
        }
        options.values.merge(["enabled": .bool(enabled), "sample_interval_hours": .number(Double(interval)), "warning_days": .number(Double(warning)), "history_days": .number(Double(retention)), "capacities": .object(limits)]) { _, new in new }
        backup.values["storage_forecast"] = .object(options.values)
        config.values["backup"] = .object(backup.values)
        pendingConfig = config
        showConfirmation = true
    }
}

private struct HeartbeatToolsView: View {
    @EnvironmentObject private var model: AppModel
    @StateObject private var session = ProtectionToolsSession()
    @State private var test = false
    var body: some View {
        Form {
            Section {
                Text("Ein unabhängiger Serverdienst sendet regelmäßig ein Lebenszeichen an deinen externen Monitor. Dort muss der Alarm für ausbleibende Lebenszeichen eingerichtet sein.")
                LabeledContent("Zustand", value: ProtectionToolLabels.state(session["data"].string("state")))
                ToolDate(title: "Letzter Versuch", value: session["data"].number("last_attempt_at"))
                ToolDate(title: "Letzter Erfolg", value: session["data"].number("last_success_at"))
                ToolDate(title: "Nächster Versuch", value: session["data"].number("next_attempt_at"))
                if !session["data"].string("last_error").isEmpty { Text(session["data"].string("last_error")).foregroundStyle(.orange) }
            } header: { Text("Externer Monitor") } footer: { Text("Ein erfolgreicher Heartbeat belegt Erreichbarkeit des Dienstes. Er bestätigt keine erfolgreiche Sicherung.") }
            Section {
                NavigationLink("Überwachung einrichten") { ProtectionOperationsSettingsView(mode: .heartbeat) }
                Button("Test-Lebenszeichen senden") { test = true }.disabled(session.isWorking || session["data"].bool("configured") != true)
                Button("Status aktualisieren") { Task { await load() } }.disabled(session.isWorking)
            }
            ToolFeedback(session: session)
        }
        .navigationTitle("Unabhängige Überwachung")
        .navigationBarTitleDisplayMode(.inline)
        .task(id: model.serverAddress) { session.reset(for: model); await load() }
        .sheet(isPresented: $test) {
            if let binding = session.binding {
                ProtectionToolPasswordSheet(title: "Lebenszeichen testen", detail: "Der Server kontaktiert den eingerichteten externen Monitor mit der gespeicherten Überwachungs-URL.", action: "Test-Lebenszeichen senden", destructive: false, binding: binding, request: { .testHeartbeat(password: $0) }) { _ in Task { await load() } }
            }
        }
    }
    private func load() async { _ = await session.call(.heartbeat, model: model) }
}

private enum OperationsSettingsMode { case heartbeat, reports }

private struct ProtectionOperationsSettingsView: View {
    @EnvironmentObject private var model: AppModel
    @StateObject private var session = ProtectionToolsSession()
    let mode: OperationsSettingsMode
    @State private var enabled = false
    @State private var period = 60
    @State private var timeout = 5
    @State private var secretURL = ""
    @State private var clearURL = false
    @State private var allowPrivate = false
    @State private var schedule = "0 8 * * 1"
    @State private var timezone = "Europe/Berlin"
    @State private var retention = 52
    @State private var emailEnabled = false
    @State private var host = ""
    @State private var port = "587"
    @State private var encryption = "starttls"
    @State private var username = ""
    @State private var emailPassword = ""
    @State private var clearEmailPassword = false
    @State private var sender = ""
    @State private var recipients = ""
    @State private var showConfirmation = false

    var body: some View {
        Form {
            if mode == .heartbeat { heartbeatFields } else { reportFields }
            ToolFeedback(session: session)
            Section { Button("Einstellungen speichern") { showConfirmation = true }.disabled(session.isWorking || session["data"].string("_revision").isEmpty) }
        }
        .navigationTitle(mode == .heartbeat ? "Monitor einrichten" : "Berichte einrichten")
        .navigationBarTitleDisplayMode(.inline)
        .task(id: model.serverAddress) { session.reset(for: model); secretURL = ""; emailPassword = ""; await load() }
        .sheet(isPresented: $showConfirmation) {
            if let binding = session.binding {
                ProtectionToolPasswordSheet(title: "Einstellungen bestätigen", detail: mode == .heartbeat ? "Der Server verwendet die angegebene Monitor-URL für regelmäßige Lebenszeichen." : "Zeitplan, Aufbewahrung und E-Mail-Ziele werden gespeichert. Der automatische Versand verwendet ausschließlich diese Empfänger.", action: "Einstellungen speichern", destructive: false, binding: binding, request: makeRequest) { _ in
                    secretURL = ""; emailPassword = ""
                    Task { await load() }
                }
            }
        }
    }

    private var heartbeatFields: some View {
        Group {
            Section("Lebenszeichen") {
                Toggle("Überwachung aktivieren", isOn: $enabled)
                SecureField("Neue HTTPS-Überwachungs-URL", text: $secretURL).textInputAutocapitalization(.never).autocorrectionDisabled()
                if session["data"].object("heartbeat").bool("url_configured") == true {
                    Text("Eine URL ist gespeichert. Ein leeres Feld erhält sie.").font(.caption).foregroundStyle(.secondary)
                    Toggle("Gespeicherte URL entfernen", isOn: $clearURL)
                }
                Stepper("Intervall: \(period) Sekunden", value: $period, in: 60...3600, step: 60)
                Stepper("Zeitlimit: \(timeout) Sekunden", value: $timeout, in: 1...10)
                Toggle("Private Monitor-Adressen erlauben", isOn: $allowPrivate)
            }
        }
    }

    private var reportFields: some View {
        Group {
            Section("Wöchentliche Berichte") {
                Toggle("Automatische Berichte", isOn: $enabled)
                TextField("Zeitplan (Cron)", text: $schedule).textInputAutocapitalization(.never).autocorrectionDisabled()
                TextField("Zeitzone", text: $timezone).textInputAutocapitalization(.never).autocorrectionDisabled()
                Stepper("Aufbewahrung: \(retention) Wochen", value: $retention, in: 4...104)
            }
            Section("E-Mail-Versand") {
                Toggle("Berichte automatisch versenden", isOn: $emailEnabled)
                TextField("SMTP-Server", text: $host).textInputAutocapitalization(.never).autocorrectionDisabled()
                TextField("Port", text: $port).keyboardType(.numberPad)
                Picker("Verschlüsselung", selection: $encryption) { Text("STARTTLS").tag("starttls"); Text("TLS").tag("tls") }
                TextField("SMTP-Benutzer", text: $username).textInputAutocapitalization(.never).autocorrectionDisabled()
                SecureField("Neues SMTP-Passwort", text: $emailPassword)
                if session["data"].object("weekly_report").object("email").bool("password_set") == true {
                    Text("Ein Passwort ist gespeichert. Ein leeres Feld erhält es.").font(.caption).foregroundStyle(.secondary)
                    Toggle("Gespeichertes Passwort entfernen", isOn: $clearEmailPassword)
                }
                TextField("Absenderadresse", text: $sender).keyboardType(.emailAddress).textInputAutocapitalization(.never).autocorrectionDisabled()
                TextField("Empfänger, mit Komma getrennt", text: $recipients, axis: .vertical).keyboardType(.emailAddress).textInputAutocapitalization(.never).autocorrectionDisabled()
                Stepper("SMTP-Zeitlimit: \(timeout) Sekunden", value: $timeout, in: 1...30)
            }
        }
    }

    private func load() async {
        guard let document = await session.call(.operationsSettings, model: model) else { return }
        let heartbeat = document.object("heartbeat")
        let report = document.object("weekly_report")
        let email = report.object("email")
        enabled = (mode == .heartbeat ? heartbeat : report).bool("enabled") ?? false
        period = heartbeat.integer("period_seconds") ?? 60
        timeout = (mode == .heartbeat ? heartbeat : email).integer("timeout_seconds") ?? 5
        allowPrivate = heartbeat.bool("allow_private_hosts") ?? false
        schedule = report.string("schedule"); timezone = report.string("timezone")
        retention = report.integer("retention_weeks") ?? 52
        emailEnabled = email.bool("enabled") ?? false
        host = email.string("host"); port = String(email.integer("port") ?? 587)
        encryption = email.string("encryption"); username = email.string("username")
        sender = email.string("from_address"); recipients = email.strings("to_addresses").joined(separator: ", ")
        clearURL = false; clearEmailPassword = false
    }

    private func makeRequest(_ password: String) -> ProtectionToolRequest {
        var heartbeat: [String: JSONValue] = ["enabled": .bool(enabled), "period_seconds": .number(Double(period)), "timeout_seconds": .number(Double(timeout)), "allow_private_hosts": .bool(allowPrivate)]
        if clearURL || !secretURL.isEmpty { heartbeat["secret_url"] = .string(clearURL ? "" : secretURL) }
        var email: [String: JSONValue] = ["enabled": .bool(emailEnabled), "host": .string(host), "port": .number(Double(port) ?? 0), "encryption": .string(encryption), "username": .string(username), "from_address": .string(sender), "to_addresses": .array(recipients.split(whereSeparator: { $0 == "," || $0 == ";" }).map { .string(String($0).trimmingCharacters(in: .whitespacesAndNewlines)) }), "timeout_seconds": .number(Double(timeout))]
        if clearEmailPassword || !emailPassword.isEmpty { email["password"] = .string(clearEmailPassword ? "" : emailPassword) }
        let report: [String: JSONValue] = ["enabled": .bool(enabled), "schedule": .string(schedule), "timezone": .string(timezone), "retention_weeks": .number(Double(retention)), "email": .object(email)]
        return .saveOperations(revision: session["data"].string("_revision"), heartbeat: mode == .heartbeat ? heartbeat : nil, report: mode == .reports ? report : nil, password: password)
    }
}

private struct TargetMigrationView: View {
    @EnvironmentObject private var model: AppModel
    @StateObject private var session = ProtectionToolsSession()
    @State private var identity = ""
    @State private var target = ""
    @State private var limitMB = 5120
    @State private var showPreview = false
    var body: some View {
        List {
            Section {
                Text("Das neue Ziel muss leer sein. Der Server kopiert den bisherigen Zielbestand und prüft ihn per SHA-256. Erst danach kannst du den Datenweg bewusst umstellen; das bisherige Ziel bleibt erhalten.")
                    .font(.subheadline).foregroundStyle(.secondary)
                Picker("Datenweg (Push)", selection: $identity) {
                    Text("Datenweg wählen").tag("")
                    ForEach((model.config?.backup.pairs ?? []).filter { $0.direction == "push" }) { Text($0.name).tag($0.id) }
                }
                TextField("Neues Ziel, z. B. remote:ordner", text: $target).textInputAutocapitalization(.never).autocorrectionDisabled()
                Stepper("Kopierlimit: \(limitMB) MB", value: $limitMB, in: 512...51_200, step: 512)
                Button("Zielwechsel prüfen") { Task { await preview() } }.disabled(identity.isEmpty || target.isEmpty || session.isWorking)
            } header: { Text("Neuen Zielwechsel vorbereiten") }
            ToolFeedback(session: session)
            Section("Vorbereitete Zielwechsel") {
                ForEach(Array(session["data"].objects("items").enumerated()), id: \.offset) { _, plan in
                    if let binding = session.binding {
                        NavigationLink { MigrationPlanView(initialPlan: plan, binding: binding) } label: {
                            VStack(alignment: .leading, spacing: 4) {
                                Text(plan.string("pair")).font(.headline)
                                Text(ProtectionToolLabels.state(plan.string("status"))).font(.caption).foregroundStyle(.secondary)
                            }
                        }
                    }
                }
            }
        }
        .navigationTitle("Sicherungsziel wechseln")
        .navigationBarTitleDisplayMode(.inline)
        .task(id: model.serverAddress) { session.reset(for: model); identity = ""; target = ""; await load() }
        .refreshable { await load() }
        .navigationDestination(isPresented: $showPreview) {
            if let binding = session.binding { MigrationPlanView(initialPlan: session["preview"], binding: binding) }
        }
    }
    private func load() async {
        _ = await session.call(.configuration, key: "configuration", model: model)
        _ = await session.call(.migrations, model: model)
    }
    private func preview() async {
        guard let _ = await session.call(.previewMigration(identity: identity, target: target.trimmingCharacters(in: .whitespacesAndNewlines), revision: session["configuration"].string("_revision"), maxTotalMB: limitMB), key: "preview", model: model) else { return }
        showPreview = true
    }
}

private struct MigrationPlanView: View {
    @EnvironmentObject private var model: AppModel
    @StateObject private var session = ProtectionToolsSession()
    let initialPlan: ProtectionToolDocument
    let binding: ProtectionToolServerBinding
    @State private var plan = ProtectionToolDocument()
    @State private var confirmation: MigrationConfirmation?
    private enum MigrationConfirmation: String, Identifiable { case copy, cutover; var id: String { rawValue } }
    var body: some View {
        Form {
            Section("Zielwechsel") {
                LabeledContent("Datenweg", value: plan.string("pair"))
                LabeledContent("Zustand", value: ProtectionToolLabels.state(plan.string("status")))
                LabeledContent("Bisheriges Ziel", value: plan.string("source")).textSelection(.enabled)
                LabeledContent("Neues Ziel", value: plan.string("target")).textSelection(.enabled)
                if let bytes = plan.number("total_bytes") { LabeledContent("Umfang", value: ProtectionToolLabels.bytes(bytes)) }
                if let files = plan.integer("files") { LabeledContent("Dateien", value: files.formatted()) }
                ToolDate(title: "Geprüft", value: plan.number("verified_at"))
                ToolDate(title: "Plan gültig bis", value: plan.number("expires_at"))
                if !plan.string("error").isEmpty { Text(plan.string("error")).foregroundStyle(.red) }
            }
            ToolWarnings(values: plan.strings("warnings"))
            Section {
                if plan.string("status") == "preview" { Button("Kopieren und prüfen …") { confirmation = .copy }.disabled(model.backupScopeIsRunning) }
                if plan.string("status") == "verified" { Button("Geprüftes Ziel übernehmen …", role: .destructive) { confirmation = .cutover }.disabled(model.backupScopeIsRunning) }
                Button("Status aktualisieren") { Task { await refresh() } }
                if let job = plan.number("job_id"), let id = Int(exactly: job) { Button("Lauf und Abbruch öffnen") { model.requestRunNavigation(id: id) } }
            }
            .disabled(session.isWorking || !binding.matches(model))
            ToolFeedback(session: session)
        }
        .navigationTitle("Zielwechsel prüfen")
        .navigationBarTitleDisplayMode(.inline)
        .task {
            plan = initialPlan
            if binding.matches(model) { session.reset(for: model); await refresh() }
        }
        .task(id: plan.string("status")) {
            guard ["running", "switching"].contains(plan.string("status")) else { return }
            while !Task.isCancelled {
                do { try await Task.sleep(for: .seconds(2)) } catch { return }
                guard binding.matches(model), ["running", "switching"].contains(plan.string("status")) else { return }
                await refresh()
            }
        }
        .sheet(item: $confirmation) { stage in
            ProtectionToolPasswordSheet(title: stage == .copy ? "Ziel kopieren" : "Ziel übernehmen", detail: stage == .copy ? "\(plan.string("source")) wird nach \(plan.string("target")) kopiert und vollständig per SHA-256 geprüft." : "Der Datenweg \(plan.string("pair")) wird auf \(plan.string("target")) umgestellt. Das bisherige Ziel bleibt erhalten.", action: stage == .copy ? "Kopieren und prüfen" : "Geprüftes Ziel übernehmen", destructive: stage == .cutover, binding: binding, request: { password in
                stage == .copy ? .runMigration(id: plan.string("id"), password: password) : .switchMigration(id: plan.string("id"), revision: plan.string("config_revision"), password: password)
            }) { response in
                plan = response
                Task { await model.refreshCurrentJobs(); await refresh() }
            }
        }
    }
    private func refresh() async {
        guard binding.matches(model) else { return }
        let wasRunning = ["running", "switching"].contains(plan.string("status"))
        if let response = await session.call(.migration(id: initialPlan.string("id")), model: model) {
            plan = response
            if wasRunning && !["running", "switching"].contains(plan.string("status")) { await model.refreshCurrentJobs() }
        }
    }
}

private struct WeeklyProtectionReportsView: View {
    @EnvironmentObject private var model: AppModel
    @StateObject private var session = ProtectionToolsSession()
    var body: some View {
        List {
            Section {
                Text("Berichte fassen Läufe, Restore-Nachweise und Speichertrends zusammen. Das Erstellen eines Berichts versendet keine E-Mail.")
                    .font(.subheadline).foregroundStyle(.secondary)
                Button("Aktuellen Wochenbericht erstellen") { Task { await generate() } }.disabled(session.isWorking)
                NavigationLink("Zeitplan und E-Mail einrichten") { ProtectionOperationsSettingsView(mode: .reports) }
            }
            ToolFeedback(session: session)
            Section("Schutzberichte") {
                ForEach(Array(session["data"].objects("items").enumerated()), id: \.offset) { _, report in
                    if let binding = session.binding {
                        NavigationLink { WeeklyProtectionReportView(report: report, binding: binding) } label: {
                            VStack(alignment: .leading, spacing: 4) {
                                Text(report.string("period_label").isEmpty ? "\(report.string("week_start")) bis \(report.string("week_end"))" : report.string("period_label")).font(.headline)
                                Text(report.number("generated_at").map { AppFormat.date($0) } ?? "–").font(.caption).foregroundStyle(.secondary)
                            }
                        }
                    }
                }
                if session["data"].objects("items").isEmpty && !session.isWorking { Text("Noch kein Wochenbericht vorhanden.").foregroundStyle(.secondary) }
            }
        }
        .navigationTitle("Wöchentliche Schutzberichte")
        .navigationBarTitleDisplayMode(.inline)
        .task(id: model.serverAddress) { session.reset(for: model); await load() }
        .refreshable { await load() }
    }
    private func load() async { _ = await session.call(.weeklyHistory, model: model) }
    private func generate() async {
        guard await session.call(.generateReport, key: "generated", model: model) != nil else { return }
        await load()
    }
}

private struct WeeklyProtectionReportView: View {
    @EnvironmentObject private var model: AppModel
    @StateObject private var session = ProtectionToolsSession()
    let report: ProtectionToolDocument
    let binding: ProtectionToolServerBinding
    @State private var file: URL?
    @State private var exporting = false
    @State private var send = false
    var body: some View {
        List {
            reportSummary
            ToolWarnings(values: report.object("summary").strings("notes"))
            Section {
                Button("PDF zum Teilen vorbereiten") { Task { await export() } }.disabled(exporting || !binding.matches(model))
                if exporting { ProgressView("PDF wird geladen …") }
                if let file { ShareLink(item: file) { Label("PDF teilen oder in Dateien sichern", systemImage: "square.and.arrow.up") } }
            }
            Section {
                Text(session["settings"].object("weekly_report").object("email").strings("to_addresses").joined(separator: ", "))
                    .font(.caption).textSelection(.enabled)
                Button("Bericht per E-Mail senden …") { send = true }
                    .disabled(session.isWorking || !binding.matches(model) || session["settings"].object("weekly_report").object("email").bool("enabled") != true)
                if !session["delivery"].values.isEmpty { Text(ProtectionToolLabels.state(session["delivery"].object("delivery").string("state"))) }
            } header: { Text("Eingerichtete Empfänger") } footer: { Text("Der Versand überträgt diesen Schutzbericht an die oben angegebenen E-Mail-Adressen.") }
            ToolFeedback(session: session)
        }
        .navigationTitle("Schutzbericht")
        .navigationBarTitleDisplayMode(.inline)
        .task {
            guard binding.matches(model) else { return }
            session.reset(for: model)
            _ = await session.call(.operationsSettings, key: "settings", model: model)
        }
        .sheet(isPresented: $send) {
            if let id = report.number("id"), let reportID = Int(exactly: id) {
                ProtectionToolPasswordSheet(title: "Schutzbericht versenden", detail: "Empfänger: \(session["settings"].object("weekly_report").object("email").strings("to_addresses").joined(separator: ", ")). Der Bericht enthält Betriebszustände und Nachweise dieses Servers.", action: "An diese Empfänger senden", destructive: false, binding: binding, request: { .sendReport(id: reportID, password: $0) }) { response in
                    session.information = ProtectionToolLabels.state(response.object("delivery").string("state"))
                }
            }
        }
    }
    private var reportSummary: some View {
        Group {
            Section("Berichtszeitraum") {
                Text(report.string("period_label").isEmpty ? "\(report.string("week_start")) bis \(report.string("week_end"))" : report.string("period_label"))
                ToolDate(title: "Erstellt", value: report.number("generated_at"))
                LabeledContent("Zeitzone", value: report.string("timezone"))
            }
            Section("Läufe") {
                reportMetric("Insgesamt", key: "total")
                reportMetric("Erfolgreich", key: "successful")
                reportMetric("Fehlgeschlagen", key: "failed")
                reportMetric("Mit Hinweisen", key: "warnings")
                reportMetric("Abgebrochen", key: "cancelled")
                reportMetric("Probeläufe", key: "dry_runs")
            }
            Section("Restore-Nachweise") {
                LabeledContent("Datenwege", value: (report.object("summary").object("protection").integer("configured_paths") ?? 0).formatted())
                LabeledContent("Restore-Prüfungen", value: (report.object("summary").object("protection").integer("restore_tests") ?? 0).formatted())
                LabeledContent("Verifizierte Prüfungen", value: (report.object("summary").object("protection").integer("verified_restore_tests") ?? 0).formatted())
            }
        }
    }
    private func reportMetric(_ title: String, key: String) -> some View {
        LabeledContent(title, value: (report.object("summary").object("jobs").integer(key) ?? 0).formatted())
    }
    private func export() async {
        guard binding.matches(model), let value = report.number("id"), let id = Int(exactly: value) else { return }
        exporting = true
        defer { exporting = false }
        do {
            let downloaded = try await model.withCurrentClient { try await $0.downloadProtectionReport(id: id) }
            guard binding.matches(model) else { return }
            file = downloaded
        }
        catch is CancellationError {} catch { session.errorMessage = error.localizedDescription }
    }
}
