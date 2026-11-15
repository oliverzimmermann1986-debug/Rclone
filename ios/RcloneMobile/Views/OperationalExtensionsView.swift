import SwiftUI
import UIKit

// MARK: - 1. Notfall-Lockdown (Panik-Knopf)

struct LockdownControlView: View {
    @EnvironmentObject var model: AppModel
    @State private var status: LockdownStatusResponse = LockdownStatusResponse(active: false)
    @State private var password = ""
    @State private var reason = "Manueller Notfall-Lockdown (iOS)"
    @State private var working = false
    @State private var message: String?
    @State private var errorMessage: String?
    @State private var manualRemountSteps: [String] = []

    var body: some View {
        List {
            statusSection
            credentialsSection
            actionSection
            if !manualRemountSteps.isEmpty {
                remountHintSection
            }
        }
        .navigationTitle("Notfall-Lockdown")
        .task { await loadStatus() }
        .refreshable { await loadStatus() }
    }

    private var statusSection: some View {
        Section("Zustand") {
            HStack {
                Image(systemName: status.active ? "exclamationmark.octagon.fill" : "checkmark.shield.fill")
                    .foregroundStyle(status.active ? .red : .green)
                VStack(alignment: .leading, spacing: 4) {
                    Text(status.active ? "LOCKDOWN AKTIV" : "Normalbetrieb")
                        .font(.headline)
                    if let reasonText = status.reason, !reasonText.isEmpty {
                        Text(reasonText)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            }
            Text("Der Notfall-Lockdown bricht laufende Sync-Jobs sofort ab, sperrt geplante Läufe, trennt konfigurierte Backup-Mounts und widerruft Web-Sitzungen.")
                .font(.footnote)
                .foregroundStyle(.secondary)
        }
    }

    private var credentialsSection: some View {
        Section("Autorisierung") {
            SecureField("Aktuelles Admin-Passwort", text: $password)
                .textContentType(.password)
            if !status.active {
                TextField("Grund für Lockdown", text: $reason)
            }
        }
    }

    private var actionSection: some View {
        Section {
            if status.active {
                Button {
                    Task { await releaseLockdown() }
                } label: {
                    Label(working ? "Wird freigegeben…" : "Lockdown aufheben", systemImage: "lock.open.fill")
                }
                .disabled(working || password.isEmpty)
            } else {
                Button(role: .destructive) {
                    Task { await triggerLockdown() }
                } label: {
                    Label(working ? "Lockdown wird ausgelöst…" : "Notfall-Lockdown auslösen", systemImage: "hand.raised.fill")
                }
                .disabled(working || password.isEmpty)
            }

            if let message {
                Text(message)
                    .font(.footnote)
                    .foregroundStyle(.green)
            }
            if let errorMessage {
                Text(errorMessage)
                    .font(.footnote)
                    .foregroundStyle(.red)
            }
        }
    }

    private var remountHintSection: some View {
        Section("Manuelle Schritte nach Freigabe") {
            ForEach(manualRemountSteps, id: \.self) { step in
                Text(step)
                    .font(.caption.monospaced())
            }
        }
    }

    private func loadStatus() async {
        guard let client = model.client else { return }
        do {
            status = try await client.getLockdownStatus()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func triggerLockdown() async {
        guard let client = model.client else { errorMessage = APIError.unauthenticated.localizedDescription; return }
        working = true
        errorMessage = nil
        message = nil
        defer { working = false }
        do {
            _ = try await client.activateLockdown(password: password, reason: reason)
            password = ""
            message = "Lockdown aktiviert. Laufende Jobs wurden gestoppt und Zeitpläne gesperrt."
            await loadStatus()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func releaseLockdown() async {
        guard let client = model.client else { errorMessage = APIError.unauthenticated.localizedDescription; return }
        working = true
        errorMessage = nil
        message = nil
        defer { working = false }
        do {
            let result = try await client.releaseLockdown(password: password)
            password = ""
            manualRemountSteps = result.manualSteps
            message = "Lockdown wurde aufgehoben."
            await loadStatus()
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}

// MARK: - 2. Schutzsiegel, Ransomware-Canary, Bitrot-Scrub & Boot-Drill

struct ObjectLockAndScrubView: View {
    @EnvironmentObject var model: AppModel
    @State private var objectLock: ObjectLockReportResponse?
    @State private var scrubStatus: BitrotScrubStatusResponse?
    @State private var bootDrill: BootDrillStatusResponse?
    @State private var ransomware: RansomwareStatusResponse?
    @State private var working = false
    @State private var feedback: String?
    @State private var errorMessage: String?
    @State private var canaryReauthPair: String?
    @State private var canaryPassword = ""

    var body: some View {
        List {
            ransomwareSection
            objectLockSection
            bitrotSection
            bootDrillSection
            if let feedback {
                Section {
                    Text(feedback).font(.footnote).foregroundStyle(.green)
                }
            }
            if let errorMessage {
                Section {
                    Text(errorMessage).font(.footnote).foregroundStyle(.red)
                }
            }
        }
        .navigationTitle("Schutzsiegel & Prüfungen")
        .task { await reloadAll(refresh: false) }
        .refreshable { await reloadAll(refresh: true) }
        .alert(
            "Canary neu initialisieren",
            isPresented: Binding(
                get: { canaryReauthPair != nil },
                set: { if !$0 { canaryReauthPair = nil; canaryPassword = "" } }
            )
        ) {
            SecureField("Aktuelles Serverpasswort", text: $canaryPassword)
            Button("Abbrechen", role: .cancel) {
                canaryReauthPair = nil
                canaryPassword = ""
            }
            Button("Überschreiben", role: .destructive) {
                let targetPair = canaryReauthPair
                let pwd = canaryPassword
                canaryReauthPair = nil
                canaryPassword = ""
                if let targetPair {
                    Task { await initCanary(for: targetPair, currentPassword: pwd) }
                }
            }
            .disabled(canaryPassword.isEmpty)
        } message: {
            Text("Um eine bestehende Canary-Wächterdatei zu erneuern, bitte das aktuelle Serverpasswort bestätigen.")
        }
    }

    private var ransomwareSection: some View {
        Section("Ransomware-Früherkennung & Canary") {
            if let ransomware {
                ForEach(ransomware.pairs) { pair in
                    CanaryPairRow(
                        pair: pair,
                        onInit: {
                            if pair.canary.configured {
                                canaryPassword = ""
                                canaryReauthPair = pair.name
                            } else {
                                Task { await initCanary(for: pair.name, currentPassword: nil) }
                            }
                        }
                    )
                }
            } else {
                Text("Wird geladen…").foregroundStyle(.secondary)
            }
        }
    }

    private var objectLockSection: some View {
        Section("Unveränderbar-Siegel (S3 Object Lock)") {
            if let objectLock {
                let lockedCount = objectLock.buckets.filter { $0.level == "locked" }.count
                HStack {
                    Image(systemName: lockedCount == objectLock.buckets.count && !objectLock.buckets.isEmpty ? "lock.shield.fill" : "shield.lefthalf.filled")
                        .foregroundStyle(lockedCount == objectLock.buckets.count && !objectLock.buckets.isEmpty ? .green : .orange)
                    Text("\(lockedCount)/\(objectLock.buckets.count) Buckets mit Object Lock")
                }
                ForEach(objectLock.buckets) { item in
                    VStack(alignment: .leading, spacing: 2) {
                        HStack {
                            Text("\(item.remote):\(item.bucket)").font(.subheadline.weight(.medium))
                            Spacer()
                            Text(item.label)
                                .font(.caption)
                                .foregroundStyle(item.level == "locked" ? .green : .secondary)
                        }
                        Text(item.hint).font(.caption).foregroundStyle(.secondary)
                    }
                }
            } else {
                Text("Kein S3-Object-Lock-Bericht geladen.").foregroundStyle(.secondary)
            }
        }
    }

    private var bitrotSection: some View {
        Section("Cloud-Bitrot-Tiefenprüfung (rclone check)") {
            if let scrubStatus {
                HStack {
                    Text("Nächster Scrub")
                    Spacer()
                    Text(scrubStatus.due ? "Fällig" : "Aktuell")
                        .foregroundStyle(scrubStatus.due ? .orange : .secondary)
                }
                if let last = scrubStatus.lastJob {
                    Text("Letzter Lauf: \(last.status)")
                        .font(.caption)
                        .foregroundStyle(last.status == "success" ? Color.secondary : Color.red)
                }
            }
            Button {
                Task { await triggerScrub() }
            } label: {
                Label("Bitrot-Scrub jetzt starten", systemImage: "checkmark.seal")
            }
            .disabled(working)
        }
    }

    private var bootDrillSection: some View {
        Section("Proxmox-Boot-Prüfung (QEMU Headless Drill)") {
            if let bootDrill {
                HStack {
                    Text("Status")
                    Spacer()
                    Text(bootDrill.running ? "Läuft…" : (bootDrill.due ? "Prüfung fällig" : "Bereit"))
                        .foregroundStyle(bootDrill.due ? .orange : .secondary)
                }
                if let last = bootDrill.last {
                    Text("Letzter Test: \(last.status)")
                        .font(.caption)
                        .foregroundStyle(last.status == "success" ? Color.secondary : Color.red)
                }
            }
            Button {
                Task { await triggerBootDrill() }
            } label: {
                Label("Boot-Drill starten", systemImage: "memorychip")
            }
            .disabled(working)
        }
    }

    private func reloadAll(refresh: Bool) async {
        guard let client = model.client else { return }
        async let lockTask = try? client.getObjectLockReport(refresh: refresh)
        async let scrubTask = try? client.getBitrotScrubStatus()
        async let drillTask = try? client.getBootDrillStatus()
        async let rwTask = try? client.getRansomwareStatus()
        objectLock = await lockTask
        scrubStatus = await scrubTask
        bootDrill = await drillTask
        ransomware = await rwTask
    }

    private func initCanary(for pairName: String, currentPassword: String?) async {
        guard let client = model.client else { errorMessage = APIError.unauthenticated.localizedDescription; return }
        working = true
        feedback = nil
        errorMessage = nil
        defer { working = false }
        do {
            let res = try await client.initPairCanary(
                pair: pairName,
                filename: nil,
                currentPassword: currentPassword
            )
            feedback = "Canary für \(res.pair) initialisiert (\(res.canaryFile))."
            ransomware = try? await client.getRansomwareStatus()
        } catch APIError.configReauthenticationRequired {
            canaryPassword = ""
            canaryReauthPair = pairName
        } catch APIError.reauthenticationRequired {
            canaryPassword = ""
            canaryReauthPair = pairName
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func triggerScrub() async {
        guard let client = model.client else { errorMessage = APIError.unauthenticated.localizedDescription; return }
        working = true
        feedback = nil
        errorMessage = nil
        defer { working = false }
        do {
            _ = try await client.startBitrotScrub(pairs: nil)
            feedback = "Bitrot-Scrub wurde in die Warteschlange gestellt."
            scrubStatus = try? await client.getBitrotScrubStatus()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func triggerBootDrill() async {
        guard let client = model.client else { errorMessage = APIError.unauthenticated.localizedDescription; return }
        working = true
        feedback = nil
        errorMessage = nil
        defer { working = false }
        do {
            _ = try await client.startBootDrill()
            feedback = "QEMU-Boot-Drill wurde gestartet."
            bootDrill = try? await client.getBootDrillStatus()
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}

private struct CanaryPairRow: View {
    let pair: RansomwarePairStatus
    let onInit: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(pair.name).font(.subheadline.weight(.medium))
                Spacer()
                if pair.canary.configured {
                    HStack(spacing: 8) {
                        Text(pair.canary.ok ? "Canary OK" : "Canary Alarm!")
                            .font(.caption.weight(.semibold))
                            .foregroundStyle(pair.canary.ok ? .green : .red)
                        Button("Neu anlegen", action: onInit)
                            .font(.caption2)
                            .buttonStyle(.bordered)
                    }
                } else {
                    Button("Canary anlegen", action: onInit)
                        .font(.caption)
                }
            }
            if let err = pair.canary.error, !err.isEmpty {
                Text(err).font(.caption2).foregroundStyle(.red)
            }
        }
    }
}

// MARK: - 3. Cloud-Speicher, Wachstumsprognose & Egress-Wächter

struct CloudQuotaAndBudgetView: View {
    @EnvironmentObject var model: AppModel
    @State private var report: CloudQuotaResponse?
    @State private var loading = false
    @State private var errorMessage: String?

    var body: some View {
        List {
            budgetSection
            remotesSection
            if let errorMessage {
                Section {
                    Text(errorMessage).font(.footnote).foregroundStyle(.red)
                }
            }
        }
        .navigationTitle("Cloud-Kontingent & Egress")
        .task { await loadQuota(refresh: false) }
        .refreshable { await loadQuota(refresh: true) }
    }

    private var budgetSection: some View {
        Section("Monatliches Transfer- & Egress-Budget") {
            if let budget = report?.budget {
                HStack {
                    Text("Egress (Download / Prüfungen)")
                    Spacer()
                    Text(ByteCountFormatter.string(fromByteCount: budget.egressBytes, countStyle: .file))
                        .foregroundStyle(budget.egressExceeded ? .red : (budget.egressWarning ? .orange : .primary))
                }
                HStack {
                    Text("Upload (Sync / Backup)")
                    Spacer()
                    Text(ByteCountFormatter.string(fromByteCount: budget.uploadBytes, countStyle: .file))
                        .foregroundStyle(budget.uploadExceeded ? .red : (budget.uploadWarning ? .orange : .primary))
                }
                if budget.throttleOptionalActive {
                    Label("Optionale Egress-Jobs (Bitrot-Scrub) sind wegen Budgetüberschreitung gedrosselt.", systemImage: "exclamationmark.triangle.fill")
                        .font(.caption)
                        .foregroundStyle(.orange)
                }
            } else {
                Text(loading ? "Wird berechnet…" : "Keine Budgetdaten verfügbar.")
                    .foregroundStyle(.secondary)
            }
        }
    }

    private var remotesSection: some View {
        Section("Cloud-Speicher & Füllstandsprognose (rclone about)") {
            if let remotes = report?.remotes, !remotes.isEmpty {
                ForEach(remotes) { remote in
                    RemoteQuotaRow(remote: remote)
                }
            } else {
                Text(loading ? "Frage Remotes ab…" : "Keine Remote-Kontingente gemeldet.")
                    .foregroundStyle(.secondary)
            }
        }
    }

    private func loadQuota(refresh: Bool) async {
        guard let client = model.client else { return }
        loading = true
        errorMessage = nil
        defer { loading = false }
        do {
            report = try await client.getCloudQuota(refresh: refresh)
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}

private struct RemoteQuotaRow: View {
    let remote: CloudRemoteQuotaItem

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(remote.remote).font(.headline)
                Spacer()
                if let pct = remote.percentUsed {
                    Text(String(format: "%.1f %% belegt", pct))
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(pct >= 90 ? .red : (pct >= 75 ? .orange : .secondary))
                }
            }
            if let used = remote.usedBytes, let free = remote.freeBytes {
                Text("Belegt: \(ByteCountFormatter.string(fromByteCount: used, countStyle: .file)) • Frei: \(ByteCountFormatter.string(fromByteCount: free, countStyle: .file))")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            if let days = remote.daysUntilFull {
                Text(String(format: "Prognose: noch ca. %.0f Tage bis voll", days))
                    .font(.caption2)
                    .foregroundStyle(days < 30 ? .orange : .secondary)
            }
            if let err = remote.error, !err.isEmpty {
                Text(err).font(.caption2).foregroundStyle(.red)
            }
        }
    }
}

// MARK: - 4. Bisync-Konfliktauflösung

struct ConflictResolutionView: View {
    @EnvironmentObject var model: AppModel
    let pairName: String
    @State private var response: PairConflictsResponse?
    @State private var password = ""
    @State private var working = false
    @State private var feedback: String?
    @State private var errorMessage: String?

    var body: some View {
        List {
            Section("Passwort für Überschreib-Aktionen") {
                SecureField("Aktuelles Passwort (für Seite 1/2 behalten)", text: $password)
                    .textContentType(.password)
            }
            Section("Erkannte Bisync-Konflikte (\(response?.groups.count ?? 0))") {
                if let conflicts = response?.groups, !conflicts.isEmpty {
                    ForEach(conflicts) { group in
                        ConflictGroupRow(group: group, working: working) { choice in
                            Task { await resolve(group: group, choice: choice) }
                        }
                    }
                } else {
                    Text("Keine offenen Bisync-Konfliktdateien für \(pairName).")
                        .foregroundStyle(.secondary)
                }
            }
            if let feedback {
                Section { Text(feedback).font(.footnote).foregroundStyle(.green) }
            }
            if let errorMessage {
                Section { Text(errorMessage).font(.footnote).foregroundStyle(.red) }
            }
        }
        .navigationTitle("Konflikte: \(pairName)")
        .task { await loadConflicts() }
        .refreshable { await loadConflicts() }
    }

    private func loadConflicts() async {
        guard let client = model.client else { return }
        do {
            response = try await client.getPairConflicts(name: pairName)
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func resolve(group: ConflictGroupItem, choice: String) async {
        guard let client = model.client else { errorMessage = APIError.unauthenticated.localizedDescription; return }
        working = true
        feedback = nil
        errorMessage = nil
        defer { working = false }
        do {
            let res = try await client.resolvePairConflict(
                name: pairName,
                basePath: group.basePath,
                choice: choice,
                password: password.isEmpty ? nil : password
            )
            feedback = "Aufgelöst: \(res.basePath) (\(res.choice))"
            await loadConflicts()
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}

private struct ConflictGroupRow: View {
    let group: ConflictGroupItem
    let working: Bool
    let onResolve: (String) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(group.basePath)
                .font(.subheadline.weight(.semibold))
            Text("Seite 1: \(group.side1.count) • Seite 2: \(group.side2.count)")
                .font(.caption)
                .foregroundStyle(.secondary)
            HStack(spacing: 8) {
                Button("Seite 1 behalten") { onResolve("keep_side1") }
                    .buttonStyle(.bordered)
                Button("Seite 2 behalten") { onResolve("keep_side2") }
                    .buttonStyle(.bordered)
                Button("Beide behalten") { onResolve("keep_both") }
                    .buttonStyle(.borderedProminent)
            }
            .font(.caption)
            .disabled(working)
        }
        .padding(.vertical, 4)
    }
}

// MARK: - 5. Zeilenweiser Text-Diff (Staging & Zeitreise)

struct LineDiffViewerView: View {
    let diff: TextDiffResponse

    var body: some View {
        List {
            Section("Zusammenfassung") {
                Text(diff.path).font(.subheadline.monospaced())
                HStack(spacing: 12) {
                    Text("+\(diff.stats.insert)").foregroundStyle(.green)
                    Text("-\(diff.stats.delete)").foregroundStyle(.red)
                    Text("~\(diff.stats.replace)").foregroundStyle(.orange)
                    Text("=\(diff.stats.equal)").foregroundStyle(.secondary)
                }
                .font(.caption.monospaced())
            }
            Section("Zeilenvergleich") {
                ForEach(diff.rows) { row in
                    DiffLineRowView(row: row)
                }
            }
        }
        .navigationTitle("Text-Diff")
    }
}

private struct DiffLineRowView: View {
    let row: TextDiffRow

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            if row.type != "insert", let leftText = row.left {
                HStack(alignment: .top, spacing: 6) {
                    Text(row.leftNo.map(String.init) ?? "-")
                        .frame(width: 32, alignment: .trailing)
                        .foregroundStyle(.secondary)
                    Text("- " + leftText)
                        .foregroundStyle(row.type == "equal" ? Color.primary : Color.red)
                }
            }
            if row.type != "delete", let rightText = row.right, row.type != "equal" {
                HStack(alignment: .top, spacing: 6) {
                    Text(row.rightNo.map(String.init) ?? "+")
                        .frame(width: 32, alignment: .trailing)
                        .foregroundStyle(.secondary)
                    Text("+ " + rightText)
                        .foregroundStyle(.green)
                }
            }
        }
        .font(.caption.monospaced())
    }
}

// MARK: - 6. Interaktiver Zeitreise-Browser

struct TimeTravelBrowserView: View {
    @EnvironmentObject var model: AppModel
    let identity: String
    let pairName: String
    @State private var points: [RecoveryPoint] = []
    @State private var selectedPointID: String = "current"
    @State private var currentPath: String = ""
    @State private var items: [TimeTravelItem] = []
    @State private var historyResult: TimeTravelHistoryResponse?
    @State private var diffResult: TextDiffResponse?
    @State private var feedback: String?
    @State private var errorMessage: String?

    var body: some View {
        List {
            pointPickerSection
            pathNavigationSection
            itemsSection
            if let feedback {
                Section { Text(feedback).font(.footnote).foregroundStyle(.green) }
            }
            if let errorMessage {
                Section { Text(errorMessage).font(.footnote).foregroundStyle(.red) }
            }
        }
        .navigationTitle("Zeitreise: \(pairName)")
        .task { await loadInitial() }
        .sheet(item: $diffResult) { diff in
            NavigationStack {
                LineDiffViewerView(diff: diff)
            }
        }
    }

    private var pointPickerSection: some View {
        Section("Zeitpunkt wählen") {
            Picker("Zeitpunkt", selection: $selectedPointID) {
                Text("Aktuell (Live)").tag("current")
                ForEach(points) { pt in
                    if pt.id != "current" {
                        Text(pt.label).tag(pt.id)
                    }
                }
            }
            .onChange(of: selectedPointID) { _, _ in
                Task { await browse() }
            }
        }
    }

    private var pathNavigationSection: some View {
        Section("Pfad") {
            HStack {
                Text(currentPath.isEmpty ? "/" : "/\(currentPath)")
                    .font(.caption.monospaced())
                Spacer()
                if !currentPath.isEmpty {
                    Button("Ebene hoch") {
                        let parts = currentPath.split(separator: "/").dropLast()
                        currentPath = parts.joined(separator: "/")
                        Task { await browse() }
                    }
                    .font(.caption)
                }
            }
        }
    }

    private var itemsSection: some View {
        Section("Dateibaum zum Zeitpunkt (\(items.count))") {
            ForEach(items) { item in
                TimeTravelItemRow(
                    item: item,
                    onOpenFolder: {
                        currentPath = item.path
                        Task { await browse() }
                    },
                    onDiffToCurrent: {
                        Task { await loadDiff(for: item) }
                    },
                    onStageRestore: {
                        Task { await restoreItemToStaging(item) }
                    }
                )
            }
        }
    }

    private func loadInitial() async {
        guard let client = model.client else { return }
        do {
            let pts = try await client.getRecoveryPoints(identity: identity)
            points = pts.points
            await browse()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func browse() async {
        guard let client = model.client else { return }
        errorMessage = nil
        do {
            let res = try await client.browseTimeTravel(identity: identity, pointID: selectedPointID, path: currentPath)
            items = res.items
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func loadDiff(for item: TimeTravelItem) async {
        guard let client = model.client else { return }
        errorMessage = nil
        do {
            diffResult = try await client.getTimeTravelDiff(
                identity: identity,
                path: item.path,
                fromPoint: item.sourcePoint,
                toPoint: "current"
            )
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func restoreItemToStaging(_ item: TimeTravelItem) async {
        guard let client = model.client else { errorMessage = APIError.unauthenticated.localizedDescription; return }
        feedback = nil
        errorMessage = nil
        do {
            let req = SelectiveRestoreRequest(
                identity: identity,
                paths: [item.path],
                maxTotalMB: 500,
                pointID: item.sourcePoint
            )
            let res = try await client.startSelectiveRestore(req)
            feedback = "Wiederherstellung ins Staging gestartet (Job #\(res.jobID))."
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}

extension TextDiffResponse: Identifiable {
    var id: String { "\(path):\(fromPoint ?? ""):\(toPoint ?? "")" }
}

private struct TimeTravelItemRow: View {
    let item: TimeTravelItem
    let onOpenFolder: () -> Void
    let onDiffToCurrent: () -> Void
    let onStageRestore: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Image(systemName: item.isDirectory ? "folder.fill" : "doc.text")
                    .foregroundStyle(item.isDirectory ? .blue : .secondary)
                if item.isDirectory {
                    Button(item.name, action: onOpenFolder)
                        .font(.subheadline.weight(.medium))
                } else {
                    Text(item.name).font(.subheadline)
                }
                Spacer()
                Text(statusBadgeText(item.statusAtPoint))
                    .font(.caption2)
                    .padding(.horizontal, 6)
                    .padding(.vertical, 2)
                    .background(Color.secondary.opacity(0.15))
                    .clipShape(Capsule())
            }
            if !item.isDirectory {
                HStack(spacing: 12) {
                    Button("Diff zu Live", action: onDiffToCurrent)
                        .buttonStyle(.bordered)
                    Button("Ins Staging holen", action: onStageRestore)
                        .buttonStyle(.borderedProminent)
                }
                .font(.caption2)
            }
        }
        .padding(.vertical, 2)
    }

    private func statusBadgeText(_ status: String) -> String {
        switch status {
        case "archived": return "Archiviert"
        case "displaced_later": return "Später überschrieben"
        case "unchanged": return "Unverändert"
        default: return "Live"
        }
    }
}

// MARK: - 7. Zero-Knowledge Crypt-Assistent

struct CryptRemoteWizardView: View {
    @EnvironmentObject var model: AppModel
    @State private var candidates: [CryptCandidateItem] = []
    @State private var loading = false
    @State private var working = false
    @State private var name = ""
    @State private var baseRemote = ""
    @State private var basePath = "sicherpfad-crypt"
    @State private var filenameEncryption = "standard"
    @State private var directoryNameEncryption = true
    @State private var useCustomKeys = false
    @State private var customPassword = ""
    @State private var customPassword2 = ""
    @State private var created: CryptCreateResponse?
    @State private var verifyFeedback: String?
    @State private var errorMessage: String?

    var body: some View {
        Form {
            Section {
                Text("Legt ein clientseitig verschlüsseltes rclone-Crypt-Remote über einem bestehenden Cloud-Ziel an. Ohne die Wiederherstellungsschlüssel kann selbst der Cloud-Anbieter keine Inhalte lesen.")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }

            if candidates.isEmpty && !loading {
                Section("Basis-Remote") {
                    Text("Noch kein geeignetes Cloud-Remote vorhanden. Verbinde zuerst unter „Neues Remote einbinden“ ein Cloud-Ziel.")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
            } else {
                Section("Crypt-Konfiguration") {
                    TextField("Name des Crypt-Remotes (z. B. pcloud-crypt)", text: $name)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    Picker("Basis-Remote", selection: $baseRemote) {
                        ForEach(candidates) { item in
                            Text("\(item.name) (\(item.type))").tag(item.name)
                        }
                    }
                    TextField("Unterordner auf Basis-Remote", text: $basePath)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    Picker("Dateinamen-Verschlüsselung", selection: $filenameEncryption) {
                        Text("Standard (empfohlen)").tag("standard")
                        Text("Verschleiert (obfuscate)").tag("obfuscate")
                        Text("Aus (nur Dateiinhalt)").tag("off")
                    }
                    Toggle("Ordnernamen verschlüsseln", isOn: $directoryNameEncryption)
                }

                Section("Schlüsselmaterial") {
                    Toggle("Eigene Passphrasen vorgeben", isOn: $useCustomKeys)
                    if useCustomKeys {
                        SecureField("Hauptschlüssel (mind. 16 Zeichen)", text: $customPassword)
                        SecureField("Salt / Zweitschlüssel (optional)", text: $customPassword2)
                    } else {
                        Text("Der Server erzeugt kryptographisch sichere 256-Bit-Zufallsschlüssel und zeigt sie einmalig zur externen Sicherung an.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    Button {
                        Task { await createCrypt() }
                    } label: {
                        HStack {
                            if working { ProgressView() }
                            Label("Crypt-Remote erstellen", systemImage: "lock.shield")
                        }
                    }
                    .disabled(working || !canSubmit)
                }
            }

            if let created {
                Section("Einmalige Wiederherstellungsschlüssel") {
                    Label(created.warning, systemImage: "exclamationmark.triangle.fill")
                        .font(.caption)
                        .foregroundStyle(.orange)
                    LabeledContent("Crypt-Remote", value: "\(created.name):")
                    LabeledContent("Zielpfad", value: created.remote)
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Passwort (Key 1)").font(.caption.weight(.semibold))
                        Text(created.keys.password)
                            .font(.system(.caption, design: .monospaced))
                            .textSelection(.enabled)
                        Text("Salt (Key 2)").font(.caption.weight(.semibold)).padding(.top, 4)
                        Text(created.keys.password2)
                            .font(.system(.caption, design: .monospaced))
                            .textSelection(.enabled)
                    }
                    Button {
                        UIPasteboard.general.string = [
                            "Sicherpfad Crypt-Remote: \(created.name):",
                            "Basis: \(created.remote)",
                            "password: \(created.keys.password)",
                            "password2: \(created.keys.password2)"
                        ].joined(separator: "\n")
                        verifyFeedback = "Schlüssel in die Zwischenablage kopiert – jetzt im Passwort-Tresor sichern."
                    } label: {
                        Label("Schlüssel kopieren", systemImage: "doc.on.doc")
                    }
                    Button {
                        Task { await verifyCreated(name: created.name) }
                    } label: {
                        Label("Verschlüsselungs-Roundtrip prüfen", systemImage: "checkmark.seal")
                    }
                    .disabled(working)
                }
            }

            if let verifyFeedback {
                Section {
                    Text(verifyFeedback).font(.footnote).foregroundStyle(.green)
                }
            }
            if let errorMessage {
                Section {
                    Text(errorMessage).font(.footnote).foregroundStyle(.red)
                }
            }
        }
        .navigationTitle("Crypt-Assistent")
        .task { await loadCandidates() }
        .refreshable { await loadCandidates() }
        .onDisappear {
            customPassword = ""
            customPassword2 = ""
        }
    }

    private var canSubmit: Bool {
        let cleanName = name.trimmingCharacters(in: .whitespacesAndNewlines)
        let cleanPath = basePath.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cleanName.isEmpty, !baseRemote.isEmpty, !cleanPath.isEmpty else { return false }
        if useCustomKeys && customPassword.count < 16 { return false }
        return true
    }

    private func loadCandidates() async {
        guard let client = model.client else { return }
        loading = true
        defer { loading = false }
        do {
            let response = try await client.getCryptCandidates()
            candidates = response.candidates
            if baseRemote.isEmpty, let first = response.candidates.first {
                baseRemote = first.name
            }
            errorMessage = nil
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func createCrypt() async {
        guard let client = model.client else { errorMessage = APIError.unauthenticated.localizedDescription; return }
        working = true
        errorMessage = nil
        verifyFeedback = nil
        defer { working = false }
        do {
            let request = CryptCreateRequest(
                name: name.trimmingCharacters(in: .whitespacesAndNewlines),
                baseRemote: baseRemote,
                basePath: basePath.trimmingCharacters(in: .whitespacesAndNewlines),
                filenameEncryption: filenameEncryption,
                directoryNameEncryption: directoryNameEncryption,
                password: useCustomKeys ? customPassword : nil,
                password2: useCustomKeys && !customPassword2.isEmpty ? customPassword2 : nil
            )
            let result = try await client.createCryptRemote(request)
            created = result
            customPassword = ""
            customPassword2 = ""
            verifyFeedback = "Crypt-Remote „\(result.name):“ wurde angelegt."
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func verifyCreated(name remoteName: String) async {
        guard let client = model.client else { return }
        working = true
        errorMessage = nil
        defer { working = false }
        do {
            let result = try await client.verifyCryptRemote(name: remoteName)
            verifyFeedback = result.ok
                ? "Roundtrip-Prüfung für „\(remoteName):“ erfolgreich bestanden."
                : "Roundtrip-Prüfung lieferte kein OK."
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}

// MARK: - 8. GFS-Versionsverwaltung (Aufbewahrung & Ausdünnung)

struct VersionsLifecycleView: View {
    @EnvironmentObject var model: AppModel
    @State private var selectedPair = ""
    @State private var preview: VersionsPreviewResponse?
    @State private var loading = false
    @State private var working = false
    @State private var showPruneAuth = false
    @State private var currentPassword = ""
    @State private var feedback: String?
    @State private var errorMessage: String?

    private var pairs: [PairConfig] {
        model.config?.backup.pairs ?? []
    }

    var body: some View {
        List {
            Section {
                Text("Prüft datierte Versionsablagen (backup_dir mit {date}) nach dem Großvater-Vater-Sohn-Prinzip (täglich, wöchentlich, monatlich) und dünnt abgelaufene Stände kontrolliert aus.")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }

            if pairs.isEmpty {
                Section("Datenweg") {
                    Text("Noch keine Datenwege konfiguriert.")
                        .foregroundStyle(.secondary)
                }
            } else {
                Section("Datenweg auswählen") {
                    Picker("Datenweg", selection: $selectedPair) {
                        ForEach(pairs) { pair in
                            Text(pair.name).tag(pair.name)
                        }
                    }
                    .onChange(of: selectedPair) { _, newValue in
                        guard !newValue.isEmpty else { return }
                        Task { await loadPreview(for: newValue) }
                    }
                    if let activePair = pairs.first(where: { $0.name == selectedPair }),
                       !activePair.backupDir.contains("{date}") {
                        Label(
                            "Dieser Datenweg nutzt aktuell keinen {date}-Platzhalter in der Versionsablage.",
                            systemImage: "info.circle"
                        )
                        .font(.caption)
                        .foregroundStyle(.orange)
                    }
                    Button {
                        Task { await loadPreview(for: selectedPair) }
                    } label: {
                        HStack {
                            if loading { ProgressView() }
                            Label("Aufbewahrungs-Vorschau laden", systemImage: "magnifyingglass")
                        }
                    }
                    .disabled(loading || selectedPair.isEmpty)
                }
            }

            if let preview {
                if preview.skipped == true {
                    Section("Status") {
                        Text("Versions-Aufbewahrung ist für diesen Datenweg nicht aktiv (\(preview.reason ?? "deaktiviert")).")
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                    }
                } else {
                    ForEach(preview.roots) { root in
                        Section(root.root) {
                            LabeledContent("Gefundene Stände", value: "\(root.total)")
                            LabeledContent("Behalten", value: "\(root.keep.count)")
                            LabeledContent("Zusammenführen", value: "\(root.merge.count)")
                            LabeledContent("Ausdünnen (Löschen)", value: "\(root.purge.count)")
                            if !root.purged.isEmpty {
                                LabeledContent("Bereits entfernt", value: "\(root.purged.count)")
                                    .foregroundStyle(.green)
                            }
                            if let err = root.error, !err.isEmpty {
                                Text(err).font(.caption).foregroundStyle(.red)
                            }
                            ForEach(root.failed) { fail in
                                Text(fail.error).font(.caption2).foregroundStyle(.red)
                            }
                        }
                    }

                    if !preview.warnings.isEmpty {
                        Section("Hinweise") {
                            ForEach(preview.warnings, id: \.self) { warning in
                                Text(warning).font(.caption).foregroundStyle(.orange)
                            }
                        }
                    }

                    let totalPurge = preview.roots.reduce(0) { $0 + $1.purge.count + $1.merge.count }
                    if totalPurge > 0 {
                        Section {
                            Button(role: .destructive) {
                                currentPassword = ""
                                showPruneAuth = true
                            } label: {
                                HStack {
                                    if working { ProgressView() }
                                    Label("Abgelaufene Generationen jetzt bereinigen", systemImage: "trash")
                                }
                            }
                            .disabled(working)
                        }
                    }
                }
            }

            if let feedback {
                Section { Text(feedback).font(.footnote).foregroundStyle(.green) }
            }
            if let errorMessage {
                Section { Text(errorMessage).font(.footnote).foregroundStyle(.red) }
            }
        }
        .navigationTitle("Versionsverwaltung")
        .task {
            if selectedPair.isEmpty {
                selectedPair = pairs.first(where: { $0.backupDir.contains("{date}") })?.name
                    ?? pairs.first?.name
                    ?? ""
            }
            if !selectedPair.isEmpty {
                await loadPreview(for: selectedPair)
            }
        }
        .alert("Versionsablage bereinigen", isPresented: $showPruneAuth) {
            SecureField("Aktuelles Serverpasswort", text: $currentPassword)
            Button("Abbrechen", role: .cancel) { currentPassword = "" }
            Button("Bereinigen", role: .destructive) {
                let pwd = currentPassword
                currentPassword = ""
                Task { await executePrune(password: pwd) }
            }
            .disabled(currentPassword.isEmpty)
        } message: {
            Text("Abgelaufene Stände für „\(selectedPair)“ unwiderruflich gemäß GFS-Regeln entfernen?")
        }
    }

    private func loadPreview(for pair: String) async {
        guard !pair.isEmpty, let client = model.client else { return }
        loading = true
        errorMessage = nil
        defer { loading = false }
        do {
            preview = try await client.previewPairVersions(pair: pair)
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func executePrune(password: String) async {
        guard !selectedPair.isEmpty, let client = model.client else { return }
        working = true
        feedback = nil
        errorMessage = nil
        defer { working = false }
        do {
            let outcome = try await client.prunePairVersions(pair: selectedPair, password: password)
            preview = outcome
            let purgedCount = outcome.roots.reduce(0) { $0 + $1.purged.count }
            let mergedCount = outcome.roots.reduce(0) { $0 + $1.merged.count }
            feedback = "Bereinigung abgeschlossen (\(purgedCount) entfernt, \(mergedCount) zusammengeführt)."
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}

// MARK: - 9. Bandbreiten-Heatmap & Zeitfenster-Vorschläge

struct BandwidthHeatmapView: View {
    @EnvironmentObject var model: AppModel
    @State private var selectedDays = 90
    @State private var heatmap: BandwidthHeatmapResponse?
    @State private var suggestions: BandwidthSuggestionsResponse?
    @State private var loading = false
    @State private var errorMessage: String?

    private let weekdayLabels = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]

    var body: some View {
        List {
            Section {
                Picker("Zeitraum", selection: $selectedDays) {
                    Text("30 Tage").tag(30)
                    Text("90 Tage").tag(90)
                    Text("180 Tage").tag(180)
                }
                .pickerStyle(.segmented)
                .onChange(of: selectedDays) { _, _ in
                    Task { await reload() }
                }
            }

            if let suggestions, !suggestions.windows.isEmpty {
                Section("Schnellste Zeitfenster") {
                    ForEach(suggestions.windows.prefix(5)) { window in
                        HStack {
                            Label(window.label, systemImage: "bolt.horizontal.circle.fill")
                                .font(.subheadline.weight(.medium))
                            Spacer()
                            Text("\(AppFormat.bytes(Int64(window.score)))/s")
                                .font(.caption.monospacedDigit())
                                .foregroundStyle(.green)
                        }
                    }
                }
            }

            if let heatmap {
                Section("Durchsatz nach Wochentag (\(heatmap.timezone))") {
                    let totalSamples = heatmap.samples.flatMap { $0 }.reduce(0, +)
                    if totalSamples == 0 {
                        Text("Noch keine ausreichenden Laufdaten für die Bandbreiten-Auswertung vorhanden.")
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                    } else {
                        ForEach(Array(weekdayLabels.enumerated()), id: \.offset) { index, day in
                            let rowCells = index < heatmap.cells.count ? heatmap.cells[index] : []
                            let rowSamples = index < heatmap.samples.count ? heatmap.samples[index] : []
                            let validSpeeds = rowCells.compactMap { $0 }
                            let avgSpeed = validSpeeds.isEmpty ? nil : validSpeeds.reduce(0, +) / Double(validSpeeds.count)
                            let daySamples = rowSamples.reduce(0, +)
                            HStack {
                                Text(day)
                                    .font(.subheadline.weight(.semibold))
                                    .frame(width: 34, alignment: .leading)
                                if let avgSpeed {
                                    Text("Ø \(AppFormat.bytes(Int64(avgSpeed)))/s")
                                        .font(.caption.monospacedDigit())
                                } else {
                                    Text("Keine Messpunkte")
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                }
                                Spacer()
                                Text("\(daySamples) Läufe")
                                    .font(.caption2)
                                    .foregroundStyle(.secondary)
                            }
                        }
                    }
                }
            } else if loading {
                Section {
                    HStack(spacing: 10) {
                        ProgressView()
                        Text("Bandbreitendaten werden ausgewertet …").foregroundStyle(.secondary)
                    }
                }
            }

            if let suggestions, !suggestions.schedules.isEmpty {
                Section("Zeitplan-Empfehlungen") {
                    ForEach(suggestions.schedules) { proposal in
                        VStack(alignment: .leading, spacing: 4) {
                            Text(proposal.name).font(.subheadline.weight(.semibold))
                            HStack(spacing: 6) {
                                Text(proposal.currentSchedule)
                                    .font(.system(.caption, design: .monospaced))
                                    .foregroundStyle(.secondary)
                                Image(systemName: "arrow.right")
                                    .font(.caption2)
                                    .foregroundStyle(.secondary)
                                Text(proposal.proposedSchedule)
                                    .font(.system(.caption, design: .monospaced))
                                    .foregroundStyle(.green)
                            }
                            Text(proposal.reason)
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                        }
                        .padding(.vertical, 2)
                    }
                }
            }

            if let errorMessage {
                Section {
                    Text(errorMessage).font(.footnote).foregroundStyle(.red)
                }
            }
        }
        .navigationTitle("Bandbreiten-Heatmap")
        .task { await reload() }
        .refreshable { await reload() }
    }

    private func reload() async {
        guard let client = model.client else { return }
        loading = true
        errorMessage = nil
        defer { loading = false }
        do {
            async let heatmapTask = client.getBandwidthHeatmap(days: selectedDays)
            async let suggestionsTask = client.getBandwidthSuggestions()
            let (freshHeatmap, freshSuggestions) = try await (heatmapTask, suggestionsTask)
            heatmap = freshHeatmap
            suggestions = freshSuggestions
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}
