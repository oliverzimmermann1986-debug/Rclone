import plistlib
from pathlib import Path

import yaml

IOS = Path(__file__).resolve().parents[1] / "ios"


def test_share_extension_embedded_with_matching_group_and_bounded_file_activation():
    project = yaml.safe_load((IOS / "project.yml").read_text(encoding="utf-8"))
    app = project["targets"]["RcloneMobile"]
    share = project["targets"]["RcloneShareExtension"]
    assert {"target": "RcloneShareExtension", "embed": True} in app["dependencies"]
    assert share["settings"]["base"]["APPLICATION_EXTENSION_API_ONLY"] is True
    assert (
        share["settings"]["base"]["PRODUCT_BUNDLE_IDENTIFIER"]
        == "de.oliverzimmermann.rclonesync.share"
    )
    info = plistlib.loads((IOS / "RcloneShareExtension/Info.plist").read_bytes())
    extension = info["NSExtension"]
    assert extension["NSExtensionPointIdentifier"] == "com.apple.share-services"
    assert extension["NSExtensionAttributes"]["NSExtensionActivationRule"] == {
        "NSExtensionActivationSupportsFileWithMaxCount": 100,
        "NSExtensionActivationSupportsImageWithMaxCount": 100,
    }
    app_groups = plistlib.loads(
        (IOS / "RcloneMobile/RcloneMobile.entitlements").read_bytes()
    )["com.apple.security.application-groups"]
    share_groups = plistlib.loads(
        (IOS / "RcloneShareExtension/RcloneShareExtension.entitlements").read_bytes()
    )["com.apple.security.application-groups"]
    assert share_groups == app_groups


def test_share_extension_has_no_client_or_credentials_and_selection_is_not_silently_truncated():
    extension = (IOS / "RcloneShareExtension/ShareViewController.swift").read_text(
        encoding="utf-8"
    )
    view = (IOS / "RcloneMobile/Views/DeviceVaultView.swift").read_text(
        encoding="utf-8"
    )
    assert "URLSession" not in extension and "APIClient" not in extension
    assert "CheckedContinuation<VaultInboxItem, Error>" in extension
    assert "urls.prefix" not in view
    assert "Warteschlange fortsetzen" in view
    assert "importInbox" in view


def test_vault_download_publishes_verified_export_only_after_success_and_refreshes_library():
    view = (IOS / "RcloneMobile/Views/DeviceVaultView.swift").read_text(
        encoding="utf-8"
    )
    restore = view.split("private func restore(_ item: VaultUploadStatus)", 1)[1].split(
        "private func exportQueued", 1
    )[0]
    # An earlier unverified queue export cannot remain visible during a new download
    # or be relabelled as verified when that download throws.
    download = restore.index("let url = try await model.withCurrentClient")
    assert restore.index("restoredURL = nil") < download
    assert restore.index("exportIsVerified = false") < download
    publish = restore.index("restoredURL = url")
    assert download < restore.index("guard queueScope == scope") < publish
    assert (
        publish < restore.index("exportIsVerified = true") < restore.index("} catch {")
    )
    error_path = restore.split("} catch {", 1)[1]
    assert "restoredURL =" not in error_path
    assert "exportIsVerified = true" not in error_path
    # Fallback can mark the server record ready OR failed: refresh outside do/catch.
    catch_end = error_path.index("\n        }\n")
    assert error_path.index("transfer.errorMessage =") < catch_end
    assert catch_end < error_path.index("await loadLibrary()")
    # Imported rescue receipts are intentionally not yet verified; the first
    # download is the operation that verifies them, so it must remain reachable.
    assert 'item.verified || item.status == "remote"' in view
    assert 'ProgressView("Datei wird zurückgeholt und geprüft …")' in view
    assert 'Button("Download abbrechen"' in view
    assert "try Task.checkCancellation()" in restore


def test_vault_share_inbox_uses_section_header_footer_initializer():
    view = (IOS / "RcloneMobile/Views/DeviceVaultView.swift").read_text(
        encoding="utf-8"
    )
    inbox_section = view.split("if !inboxItems.isEmpty, !model.isDemoMode {", 1)[
        1
    ].split("if isImporting {", 1)[0]
    # Section(_:content:) has EmptyView as Footer, so adding a footer to the
    # title shorthand parses but cannot be typechecked by SwiftUI.
    assert 'Section("Aus dem Teilen-Menü")' not in inbox_section
    assert (
        '} header: {\n                    Text("Aus dem Teilen-Menü")\n                } footer: {'
        in inbox_section
    )


def test_vault_repeated_rows_have_independent_swiftui_typechecking_boundaries():
    view = (IOS / "RcloneMobile/Views/DeviceVaultView.swift").read_text(
        encoding="utf-8"
    )
    for name in [
        "VaultQueuedRow",
        "VaultInboxRow",
        "VaultUnassignedRow",
        "VaultLibraryRow",
    ]:
        assert f"private struct {name}: View" in view
        assert f"{name}(" in view.split(f"private struct {name}: View", 1)[0]
    queued = view.split("private struct VaultQueuedRow: View", 1)[1].split(
        "private struct VaultInboxRow", 1
    )[0]
    assert "private var formattedSize: String" in queued
    assert "private var statusText: String" in queued
    assert "private var statusColor: Color" in queued
    assert "private var progress: Double" in queued
    assert "ProgressView(value: progress)" in queued
    # Extraction must keep parent-owned actions and the scope-aware guards.
    assert "transfer.removeQueued(entry, scope: scope)" in view
    assert "Task { await exportQueued(entry) }" in view
    assert "pendingReassignmentScope = queueScope" in view
    assert (
        "VaultLibraryRow(item: item, isRestoring: restoringItemID == item.id)" in view
    )


def test_vault_list_sections_and_event_handlers_typecheck_independently():
    view = (IOS / "RcloneMobile/Views/DeviceVaultView.swift").read_text(
        encoding="utf-8"
    )
    sections = [
        "heroSection",
        "destinationSection",
        "importSection",
        "inboxSection",
        "importProgressSection",
        "unassignedSection",
        "queueSection",
        "currentTransferSection",
        "librarySection",
    ]
    list_body = view.split("private var vaultList: some View {", 1)[1].split(
        "private var heroSection", 1
    )[0]
    for section in sections:
        assert section in list_body
        assert f"private var {section}: some View" in view
    assert "ForEach" not in list_body
    assert "AnyView" not in view
    assert "ForEach(transfer.queue, id: \\.id) { (entry: VaultQueueEntry) in" in view
    assert "private func queuedRow(_ entry: VaultQueueEntry) -> some View" in view
    assert "onCompletion: handleImportedFiles" in view
    assert "Task { await importPhotos(items) }" in view
    assert "private func importPhotos(_ items: [PhotosPickerItem]) async" in view
    removal = view.split("private func removeQueued(_ entry: VaultQueueEntry)", 1)[
        1
    ].split("private func requestReassignment", 1)[0]
    assert (
        "guard !transfer.isWorking, let scope = queueScope else { return }" in removal
    )
    assert "transfer.removeQueued(entry, scope: scope)" in removal
