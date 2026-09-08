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
