import UIKit
import UniformTypeIdentifiers

@MainActor
final class ShareViewController: UIViewController {
    private let statusLabel = UILabel()
    private let saveButton = UIButton(type: .system)
    private let doneButton = UIButton(type: .system)
    private var providers: [NSItemProvider] = []
    private var isSaving = false

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .systemGroupedBackground
        providers = (extensionContext?.inputItems as? [NSExtensionItem] ?? []).flatMap { $0.attachments ?? [] }
        let title = UILabel()
        title.text = "In Sicherpfad vormerken"
        title.font = .preferredFont(forTextStyle: .title2)
        title.adjustsFontForContentSizeCategory = true
        title.numberOfLines = 0
        statusLabel.font = .preferredFont(forTextStyle: .body)
        statusLabel.adjustsFontForContentSizeCategory = true
        statusLabel.numberOfLines = 0
        statusLabel.text = "\(providers.count) Dateien lokal vormerken. Öffne danach Sicherpfad → Sichern → Geräte-Vault und wähle deinen Datenweg."
        var saveConfiguration = UIButton.Configuration.filled()
        saveConfiguration.title = "Lokal vormerken"
        saveConfiguration.baseBackgroundColor = .systemGreen
        saveButton.configuration = saveConfiguration
        saveButton.addAction(UIAction { [weak self] _ in self?.save() }, for: .touchUpInside)
        var doneConfiguration = UIButton.Configuration.plain()
        doneConfiguration.title = "Abbrechen"
        doneButton.configuration = doneConfiguration
        doneButton.addAction(UIAction { [weak self] _ in
            self?.extensionContext?.completeRequest(returningItems: nil)
        }, for: .touchUpInside)
        let stack = UIStackView(arrangedSubviews: [title, statusLabel, saveButton, doneButton])
        stack.axis = .vertical
        stack.spacing = 24
        stack.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: view.safeAreaLayoutGuide.leadingAnchor, constant: 24),
            stack.trailingAnchor.constraint(equalTo: view.safeAreaLayoutGuide.trailingAnchor, constant: -24),
            stack.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor, constant: 32),
            stack.bottomAnchor.constraint(lessThanOrEqualTo: view.safeAreaLayoutGuide.bottomAnchor, constant: -24)
        ])
        if providers.isEmpty || providers.count > 100 {
            statusLabel.text = "Wähle zwischen 1 und 100 Dateien oder Fotos aus."
            saveButton.isEnabled = false
        }
    }

    private func save() {
        guard !isSaving else { return }
        isSaving = true
        saveButton.isEnabled = false
        doneButton.isEnabled = false
        Task {
            var completed = 0
            var failures = 0
            do {
                let inbox = try VaultInbox()
                for provider in providers {
                    do { _ = try await stage(provider, in: inbox); completed += 1 }
                    catch { failures += 1 }
                    statusLabel.text = "\(completed) von \(providers.count) Dateien lokal vorgemerkt …"
                }
                statusLabel.text = failures == 0
                    ? "\(completed) Dateien sind vorgemerkt. Öffne Sicherpfad → Sichern → Geräte-Vault und übernimm sie in deinen gewünschten Datenweg. Es wurde noch nichts hochgeladen."
                    : "\(completed) Dateien sind vorgemerkt; \(failures) konnten nicht gelesen werden. Bitte teile die fehlenden Dateien erneut. Öffne Sicherpfad, um den Datenweg für die vorgemerkten Dateien zu wählen."
            } catch {
                statusLabel.text = error.localizedDescription
            }
            doneButton.configuration?.title = "Fertig"
            doneButton.isEnabled = true
            isSaving = false
        }
    }

    private func stage(_ provider: NSItemProvider, in inbox: VaultInbox) async throws -> VaultInboxItem {
        if provider.hasItemConformingToTypeIdentifier(UTType.fileURL.identifier) {
            return try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<VaultInboxItem, Error>) in
                provider.loadItem(forTypeIdentifier: UTType.fileURL.identifier, options: nil) { value, error in
                    do {
                        if let error { throw error }
                        guard let url = value as? URL, url.isFileURL else { throw CocoaError(.fileReadUnsupportedScheme) }
                        let isImage = UTType(filenameExtension: url.pathExtension)?.conforms(to: .image) == true
                        continuation.resume(returning: try inbox.stage(url, filename: url.lastPathComponent, sourceType: isImage ? "photo" : "file"))
                    } catch { continuation.resume(throwing: error) }
                }
            }
        }
        guard let identifier = provider.registeredTypeIdentifiers.first(where: {
            guard let type = UTType($0) else { return false }
            return type.conforms(to: .image) || type.conforms(to: .data)
        }) else { throw CocoaError(.fileReadUnknown) }
        let suggestedName = provider.suggestedName
        return try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<VaultInboxItem, Error>) in
            provider.loadFileRepresentation(forTypeIdentifier: identifier) { url, error in
                do {
                    if let error { throw error }
                    guard let url, url.isFileURL else { throw CocoaError(.fileReadUnknown) }
                    var filename = suggestedName ?? url.lastPathComponent
                    if URL(fileURLWithPath: filename).pathExtension.isEmpty,
                       let suffix = UTType(identifier)?.preferredFilenameExtension { filename += ".\(suffix)" }
                    // Copy while this callback owns the temporary representation.
                    // NSItemProvider deletes it as soon as the callback returns.
                    let item = try inbox.stage(url, filename: filename,
                        sourceType: UTType(identifier)?.conforms(to: .image) == true ? "photo" : "file")
                    continuation.resume(returning: item)
                } catch { continuation.resume(throwing: error) }
            }
        }
    }
}
