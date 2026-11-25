import SwiftUI

struct SourceFolderGroup: Identifiable {
    let id: String
    let title: String
    let sourcePath: String
    let pairs: [StoragePair]

    var primaryPair: StoragePair { pairs[0] }

    var localSize: PathSize? {
        for pair in pairs {
            let candidate = pair.source == pair.local ? pair.sourceSize : pair.targetSize
            if candidate?.bytes != nil || candidate?.count != nil {
                return candidate
            }
        }
        return primaryPair.source == primaryPair.local ? primaryPair.sourceSize : primaryPair.targetSize
    }

    var distinctCloudCount: Int {
        Set(pairs.compactMap { CloudTargetPresentation.remoteKey(from: $0.remote ?? $0.target) }).count
    }

    var allRestoreCurrent: Bool {
        !pairs.isEmpty && pairs.allSatisfy { $0.restoreEvidence?.isCurrent == true }
    }

    var hasRestoreFailure: Bool {
        pairs.contains { $0.restoreEvidence?.state == "failed" }
    }

    static func group(_ pairs: [StoragePair]) -> [SourceFolderGroup] {
        var order: [String] = []
        var buckets: [String: [StoragePair]] = [:]

        for pair in pairs {
            let key: String
            if pair.direction.lowercased() == "push", !pair.local.isEmpty {
                key = "push:\(pair.local.trimmingCharacters(in: .whitespacesAndNewlines).lowercased())"
            } else {
                key = "single:\(pair.id)"
            }
            if buckets[key] == nil {
                order.append(key)
                buckets[key] = []
            }
            buckets[key]?.append(pair)
        }

        return order.compactMap { key in
            guard let items = buckets[key], let first = items.first else { return nil }
            let title = deriveTitle(for: items, fallbackPath: first.local)
            return SourceFolderGroup(
                id: key,
                title: title,
                sourcePath: first.local,
                pairs: items
            )
        }
    }

    private static func deriveTitle(for pairs: [StoragePair], fallbackPath: String) -> String {
        if pairs.count == 1 {
            return pairs[0].name
        }
        let cleaned = fallbackPath.trimmingCharacters(in: CharacterSet(charactersIn: "/ "))
        if let last = cleaned.split(separator: "/").last, !last.isEmpty {
            let segment = String(last)
            return segment.prefix(1).uppercased() + segment.dropFirst()
        }
        return pairs[0].name
    }
}

enum CloudTargetPresentation {
    static func remoteKey(from path: String) -> String? {
        guard let colon = path.firstIndex(of: ":") else { return nil }
        let name = String(path[..<colon]).trimmingCharacters(in: .whitespacesAndNewlines)
        return name.isEmpty ? nil : name.lowercased()
    }

    static func label(for path: String) -> String {
        guard let key = remoteKey(from: path) else {
            return path.hasPrefix("/") ? "Lokal" : path
        }
        switch key {
        case "pcloud": return "pCloud"
        case "pcloud-tresor": return "pCloud Tresor"
        case "gdrive": return "Google Drive"
        case "gdrive-tresor": return "GDrive Tresor"
        case "icloud", "iclouddrive": return "iCloud Drive"
        case "pbs-sftp": return "PBS (SFTP)"
        default:
            return key
                .replacingOccurrences(of: "-", with: " ")
                .replacingOccurrences(of: "_", with: " ")
                .localizedCapitalized
        }
    }

    static func isEncryptedVault(_ path: String) -> Bool {
        guard let key = remoteKey(from: path) else { return false }
        return key.contains("tresor") || key.contains("crypt") || key.contains("vault")
    }

    static func symbol(for path: String) -> String {
        if isEncryptedVault(path) { return "lock.icloud.fill" }
        if path.hasPrefix("/") { return "folder.fill" }
        return "icloud.fill"
    }

    static func accent(for path: String) -> Color {
        guard let key = remoteKey(from: path) else { return .secondary }
        if key.contains("pcloud") { return SicherpfadGlassPalette.teal }
        if key.contains("gdrive") || key.contains("drive") { return SicherpfadGlassPalette.emerald }
        if key.contains("icloud") { return .blue }
        return SicherpfadGlassPalette.emerald
    }
}

struct CloudTargetChip: View {
    let remotePath: String

    var body: some View {
        let color = CloudTargetPresentation.accent(for: remotePath)
        Label(
            CloudTargetPresentation.label(for: remotePath),
            systemImage: CloudTargetPresentation.symbol(for: remotePath)
        )
        .font(.caption2.weight(.semibold))
        .foregroundStyle(color)
        .lineLimit(1)
        .padding(.horizontal, 8)
        .padding(.vertical, 4)
        .sicherpfadGlassBadge(color: color)
    }
}

struct GroupedSourceCopyCard: View {
    let group: SourceFolderGroup
    let isMeasuring: Bool
    let isRestoreTesting: (String) -> Bool
    let onSelectPair: (StoragePair) -> Void

    private var redundancyColor: Color {
        if group.hasRestoreFailure { return .red }
        return group.distinctCloudCount >= 2 ? .green : .orange
    }

    private var redundancyLabel: String {
        if group.distinctCloudCount >= 2 {
            return "\(group.distinctCloudCount) Clouds · 3-2-1"
        }
        return group.distinctCloudCount == 1 ? "1 Cloud" : "Lokal"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .top, spacing: 11) {
                Image(systemName: group.sourcePath.hasPrefix("/") ? "folder.fill" : "externaldrive.connected.to.line.below.fill")
                    .font(.headline)
                    .foregroundStyle(SicherpfadGlassPalette.emerald)
                    .frame(width: 34, height: 34)
                    .background(SicherpfadGlassPalette.emerald.opacity(0.16), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                    .overlay {
                        RoundedRectangle(cornerRadius: 10, style: .continuous)
                            .strokeBorder(Color.white.opacity(0.38), lineWidth: 0.8)
                    }

                VStack(alignment: .leading, spacing: 2) {
                    Text(group.title)
                        .font(.headline)
                        .fixedSize(horizontal: false, vertical: true)
                    Text(group.sourcePath)
                        .font(.caption2.monospaced())
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .truncationMode(.middle)
                }

                Spacer(minLength: 6)

                Text(redundancyLabel)
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(redundancyColor)
                    .padding(.horizontal, 8)
                    .padding(.vertical, 4)
                    .sicherpfadGlassBadge(color: redundancyColor)
            }

            if let localSize = group.localSize, (localSize.bytes != nil || localSize.count != nil) {
                HStack(spacing: 12) {
                    Label(AppFormat.bytes(localSize.bytes), systemImage: "internaldrive")
                        .font(.caption.monospacedDigit().weight(.medium))
                        .foregroundStyle(.secondary)
                    if let count = localSize.count {
                        Text("\(AppFormat.count(count)) Dateien")
                            .font(.caption.monospacedDigit())
                            .foregroundStyle(.secondary)
                    }
                    if isMeasuring {
                        ProgressView().controlSize(.mini)
                    }
                }
            }

            VStack(spacing: 8) {
                ForEach(group.pairs) { pair in
                    targetRow(for: pair)
                }
            }
        }
        .padding(.vertical, 6)
    }

    private func targetRow(for pair: StoragePair) -> some View {
        let targetPath = pair.remote ?? pair.target
        let targetSize = pair.source == pair.remote ? pair.sourceSize : pair.targetSize
        let accent = CloudTargetPresentation.accent(for: targetPath)

        return Button {
            onSelectPair(pair)
        } label: {
            HStack(alignment: .center, spacing: 10) {
                Image(systemName: CloudTargetPresentation.symbol(for: targetPath))
                    .font(.subheadline)
                    .foregroundStyle(accent)
                    .frame(width: 24)

                VStack(alignment: .leading, spacing: 3) {
                    HStack(spacing: 6) {
                        Text(CloudTargetPresentation.label(for: targetPath))
                            .font(.subheadline.weight(.semibold))
                            .foregroundStyle(.primary)
                        Spacer(minLength: 4)
                        if let bytes = targetSize?.bytes {
                            Text(AppFormat.bytes(bytes))
                                .font(.caption.monospacedDigit().weight(.medium))
                                .foregroundStyle(.secondary)
                        }
                    }
                    HStack(spacing: 8) {
                        Text(pair.lastSync.map { AppFormat.relative($0) } ?? "Noch kein Lauf")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                        Spacer(minLength: 4)
                        RestoreEvidenceBadge(
                            evidence: pair.restoreEvidence,
                            isRunning: isRestoreTesting(pair.name)
                        )
                    }
                }

                Image(systemName: "chevron.right")
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(.tertiary)
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 10)
            .sicherpfadGlassCard(cornerRadius: 14, accent: accent, prominent: false)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("\(group.title) nach \(CloudTargetPresentation.label(for: targetPath))")
        .accessibilityHint("Öffnet Statistiken, Schutzpfad und Restore-Nachweis.")
    }
}

struct MultiCloudSetupSheet: View {
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var configurationDraft: ConfigurationDraftStore
    @Environment(\.dismiss) private var dismiss

    @State private var folderName = ""
    @State private var localPath = "/mnt/data/"
    @State private var selectedRemotes: Set<String> = []
    @State private var remoteSubfolder = ""
    @State private var mode = "copy"
    @State private var schedulePreset = "0 3 * * *"
    @State private var runInParallel = true
    @State private var exclude = ".DS_Store\nThumbs.db"
    @State private var showingPathBrowser = false
    @State private var availableRemotes: [String] = []
    @State private var isLoadingRemotes = false

    private let schedulePresets: [(label: String, cron: String)] = [
        ("Täglich · 02:30", "30 2 * * *"),
        ("Täglich · 03:00", "0 3 * * *"),
        ("Täglich · 05:00", "0 5 * * *"),
        ("Sonntags · 04:00", "0 4 * * 0"),
        ("Nur manuell", "manual")
    ]

    private var effectiveSubfolder: String {
        let custom = remoteSubfolder.trimmingCharacters(in: CharacterSet(charactersIn: "/ "))
        if !custom.isEmpty { return custom }
        return folderName.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private var canCreate: Bool {
        !folderName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && !localPath.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && !selectedRemotes.isEmpty
    }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("Name (z. B. Dokumente)", text: $folderName)
                    HStack {
                        TextField("/mnt/data/dokumente", text: $localPath)
                            .font(.subheadline.monospaced())
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                        Button {
                            showingPathBrowser = true
                        } label: {
                            Image(systemName: "folder.badge.plus")
                        }
                        .accessibilityLabel("Quellordner auf dem Server auswählen")
                    }
                    TextField("Ziel-Unterordner (optional)", text: $remoteSubfolder)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                } header: {
                    Text("1 · Quellordner")
                } footer: {
                    Text("Ein Ordner wird einmal konfiguriert und automatisch auf alle gewählten Clouds verteilt.")
                }
                .sicherpfadGlassListRow()

                Section {
                    if isLoadingRemotes && availableRemotes.isEmpty {
                        ProgressView("Clouds werden geladen …")
                    } else if availableRemotes.isEmpty {
                        Text("Keine Cloud-Remotes gefunden. Richte zuerst unter Clouds mindestens ein Ziel ein.")
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                    } else {
                        ForEach(availableRemotes, id: \.self) { remote in
                            Toggle(isOn: Binding(
                                get: { selectedRemotes.contains(remote) },
                                set: { enabled in
                                    if enabled { selectedRemotes.insert(remote) } else { selectedRemotes.remove(remote) }
                                }
                            )) {
                                HStack(spacing: 10) {
                                    Image(systemName: CloudTargetPresentation.symbol(for: "\(remote):"))
                                        .foregroundStyle(CloudTargetPresentation.accent(for: "\(remote):"))
                                    VStack(alignment: .leading, spacing: 2) {
                                        Text(CloudTargetPresentation.label(for: "\(remote):"))
                                            .font(.body.weight(.medium))
                                        Text("\(remote):/\(effectiveSubfolder.isEmpty ? "…" : effectiveSubfolder)")
                                            .font(.caption2.monospaced())
                                            .foregroundStyle(.secondary)
                                    }
                                }
                            }
                        }
                    }
                } header: {
                    Text("2 · Ziel-Clouds wählen (\(selectedRemotes.count))")
                } footer: {
                    Text(selectedRemotes.count >= 2
                        ? "3-2-1-Schutz aktiv: derselbe Ordner wird redundant in \(selectedRemotes.count) verschiedene Clouds gesichert."
                        : "Wähle mindestens zwei Clouds für redundanten 3-2-1-Schutz.")
                }
                .sicherpfadGlassListRow()

                Section("3 · Verfahren & Zeitplan") {
                    Picker("Verfahren", selection: $mode) {
                        Text("Kopieren (ohne Löschen)").tag("copy")
                        Text("Spiegeln (Sync)").tag("sync")
                    }
                    Picker("Zeitplan", selection: $schedulePreset) {
                        ForEach(schedulePresets, id: \.cron) { preset in
                            Text(preset.label).tag(preset.cron)
                        }
                    }
                    if selectedRemotes.count >= 2 {
                        Toggle("Clouds gleichzeitig sichern (Parallel)", isOn: $runInParallel)
                    }
                }
                .sicherpfadGlassListRow()

                Section("Ausschlüsse (optional)") {
                    TextField(".DS_Store", text: $exclude, axis: .vertical)
                        .font(.caption.monospaced())
                        .lineLimit(2...4)
                }
                .sicherpfadGlassListRow()
            }
            .sicherpfadGlassScreen()
            .navigationTitle("Ordner → Mehrere Clouds")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Abbrechen") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Anlegen") { createMultiCloudPlan() }
                        .disabled(!canCreate)
                }
            }
            .sheet(isPresented: $showingPathBrowser) {
                PathBrowserSheet(kind: .local, initialPath: localPath, allowsKindSwitch: true) { picked in
                    localPath = picked
                    if folderName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                        let cleaned = picked.trimmingCharacters(in: CharacterSet(charactersIn: "/ "))
                        if let last = cleaned.split(separator: "/").last {
                            let name = String(last)
                            folderName = name.prefix(1).uppercased() + name.dropFirst()
                        }
                    }
                }
            }
            .task { await loadRemotes() }
        }
    }

    private func loadRemotes() async {
        isLoadingRemotes = true
        defer { isLoadingRemotes = false }

        var names: [String] = []
        if !model.isDemoMode {
            if let list = try? await model.withCurrentClient({ try await $0.browseRemote(path: "") }) {
                names = list.entries.map { $0.name.trimmingCharacters(in: CharacterSet(charactersIn: ": ")) }
            }
        }
        if names.isEmpty {
            let fromPairs = configurationDraft.pairs.compactMap { CloudTargetPresentation.remoteKey(from: $0.remote) }
            names = Array(Set(fromPairs)).sorted()
        }
        availableRemotes = Array(Set(names.filter { !$0.isEmpty })).sorted()
    }

    private func createMultiCloudPlan() {
        let cleanTitle = folderName.trimmingCharacters(in: .whitespacesAndNewlines)
        let cleanLocal = localPath.trimmingCharacters(in: .whitespacesAndNewlines)
        let cleanSub = effectiveSubfolder
        let sortedRemotes = selectedRemotes.sorted()
        let isManual = schedulePreset == "manual"

        var createdIDs: [String] = []
        for remote in sortedRemotes {
            let cloudLabel = CloudTargetPresentation.label(for: "\(remote):")
            let pairName = "\(cleanTitle) \(cloudLabel)"
            let remotePath = cleanSub.isEmpty ? "\(remote):" : "\(remote):/\(cleanSub)"
            let pair = PairConfig(
                stableID: nil,
                name: pairName,
                local: cleanLocal,
                remote: remotePath,
                direction: "push",
                mode: mode,
                enabled: !isManual,
                allowDelete: false,
                minLocalFiles: 1,
                maxSuccessAgeHours: isManual ? 0 : 48,
                exclude: exclude.trimmingCharacters(in: .whitespacesAndNewlines)
            )
            configurationDraft.upsertPair(pair, at: nil)
            createdIDs.append(pair.id)
        }

        let job = JobDefinition(
            id: "",
            name: cleanTitle,
            enabled: true,
            dataPathIDs: createdIDs,
            schedule: schedulePreset,
            executionMode: (selectedRemotes.count >= 2 && runInParallel) ? "parallel" : "sequential",
            maxParallel: (selectedRemotes.count >= 2 && runInParallel) ? min(4, selectedRemotes.count) : 1,
            retryMinutes: 60
        )
        configurationDraft.upsertDefinition(job, at: nil)
        dismiss()
    }
}
