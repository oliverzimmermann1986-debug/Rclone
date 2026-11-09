import SwiftUI

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
    }

    private var ransomwareSection: some View {
        Section("Ransomware-Früherkennung & Canary") {
            if let ransomware {
                ForEach(ransomware.pairs) { pair in
                    CanaryPairRow(pair: pair) {
                        Task { await initCanary(for: pair.name) }
                    }
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
                        .foregroundStyle(last.status == "success" ? .secondary : .red)
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
                        .foregroundStyle(last.status == "success" ? .secondary : .red)
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

    private func initCanary(for pairName: String) async {
        guard let client = model.client else { errorMessage = APIError.unauthenticated.localizedDescription; return }
        working = true
        feedback = nil
        errorMessage = nil
        defer { working = false }
        do {
            let res = try await client.initPairCanary(pair: pairName, filename: nil, currentPassword: nil)
            feedback = "Canary für \(res.pair) initialisiert (\(res.canaryFile))."
            ransomware = try? await client.getRansomwareStatus()
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
                    Text(pair.canary.ok ? "Canary OK" : "Canary Alarm!")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(pair.canary.ok ? .green : .red)
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
