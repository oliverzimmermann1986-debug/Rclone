import Combine
import Foundation

@MainActor
final class ConfigurationDraftStore: ObservableObject {
    @Published private(set) var pairs: [PairConfig] = []
    @Published private(set) var definitions: [JobDefinition] = []
    @Published private(set) var baseRevision: String?
    @Published private(set) var isDirty = false

    func load(from snapshot: ConfigSnapshot?, force: Bool = false) {
        guard let snapshot else { return }
        guard force || !isDirty else { return }
        pairs = snapshot.backup.pairs
        definitions = snapshot.backup.jobs
        baseRevision = snapshot.revision
        isDirty = false
    }

    func upsertPair(_ pair: PairConfig, at index: Int?) {
        if let index, pairs.indices.contains(index) {
            pairs[index] = pair
        } else {
            pairs.append(pair)
        }
        isDirty = true
    }

    func removePair(at index: Int) {
        guard pairs.indices.contains(index) else { return }
        pairs.remove(at: index)
        isDirty = true
    }

    func upsertDefinition(_ definition: JobDefinition, at index: Int?) {
        if let index, definitions.indices.contains(index) {
            definitions[index] = definition
        } else {
            definitions.append(definition)
        }
        isDirty = true
    }

    func removeDefinition(at index: Int) {
        guard definitions.indices.contains(index) else { return }
        definitions.remove(at: index)
        isDirty = true
    }
}
