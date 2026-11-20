import SwiftUI

struct CloudsScreen: View {
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var configurationDraft: ConfigurationDraftStore
    @Binding var showingSettings: Bool
    @State private var showingSetup = false
    @State private var result: BrowseResponse?
    @State private var errorMessage: String?
    @State private var isLoading = true
    @State private var requestID = UUID()

    var body: some View {
        List {
            Section("Eingerichtete Zugänge") {
                if let errorMessage {
                    LoadFailureView(title: "Cloud-Zugänge nicht geladen", message: errorMessage) {
                        Task { await load() }
                    }
                }
                if let result {
                    ForEach(result.entries) { remote in
                        NavigationLink {
                            DataPathsScreen(showingSettings: $showingSettings, cloudPath: remote.path)
                        } label: {
                            VStack(alignment: .leading, spacing: 4) {
                                Text(remote.name).font(.headline)
                                if configurationDraft.baseRevision != nil {
                                    let count = configurationDraft.pairs.filter { $0.referencesCloud(remote.path) }.count
                                    Text(configurationPathCount(count))
                                        .font(.subheadline)
                                        .foregroundStyle(.secondary)
                                }
                            }
                        }
                    }
                    if result.truncated == true {
                        Text("Der Server hat die Liste begrenzt.").foregroundStyle(.secondary)
                    }
                    if result.entries.isEmpty, errorMessage == nil, !isLoading {
                        ContentUnavailableView(
                            "Keine Cloud-Zugänge",
                            systemImage: "cloud",
                            description: Text("Richte einen Zugang ein, um ihn als Quelle oder Ziel zu verwenden.")
                        )
                    }
                } else if isLoading {
                    LoadingSection(label: "Cloud-Zugänge werden geladen …")
                }
                Button { showingSetup = true } label: {
                    Label("Cloud verbinden", systemImage: "plus")
                }
            }
            Section("Cloud-Werkzeuge") {
                NavigationLink { CryptRemoteWizardView() } label: {
                    Label("Verschlüsselten Zugang anlegen", systemImage: "lock.icloud")
                }
                NavigationLink { CloudQuotaAndBudgetView() } label: {
                    Label("Cloud-Kontingente ansehen", systemImage: "chart.bar")
                }
            }
        }
        .listStyle(.insetGrouped)
        .navigationTitle("Clouds")
        .task(id: model.serverAddress) {
            configurationDraft.load(from: model.config)
            await load(reset: true)
        }
        .refreshable { await load() }
        .sheet(isPresented: $showingSetup, onDismiss: { Task { await load() } }) {
            RemoteSetupView()
        }
    }

    private func load(reset: Bool = false) async {
        let currentRequestID = UUID()
        requestID = currentRequestID
        isLoading = true
        errorMessage = nil
        if reset { result = nil }
        defer {
            if requestID == currentRequestID { isLoading = false }
        }
        do {
            let response = try await model.withCurrentClient { try await $0.browseRemote(path: "") }
            guard requestID == currentRequestID, !Task.isCancelled else { return }
            if let error = response.error {
                errorMessage = error
            } else {
                result = response
            }
        } catch is CancellationError {
        } catch {
            guard requestID == currentRequestID, !Task.isCancelled else { return }
            errorMessage = error.localizedDescription
        }
    }
}
