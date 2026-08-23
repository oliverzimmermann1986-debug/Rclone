import subprocess

import pytest
from fastapi import HTTPException

from app.routes import api_browse


def test_pcloud_crypto_folder_is_hidden_from_remote_browser(monkeypatch):
    monkeypatch.setattr(api_browse, "_rclone_remotes", lambda: ["pcloud:"])
    monkeypatch.setattr(
        api_browse,
        "_rclone_directories",
        lambda _path: (["Crypto Folder", "Photos", "RcloneVault"], False),
    )
    monkeypatch.setattr(
        api_browse,
        "_hidden_remote_paths",
        lambda: {"pcloud:/crypto folder"},
    )

    result = api_browse.browse_rclone("pcloud:")

    assert [entry["name"] for entry in result["entries"]] == [
        "Photos",
        "RcloneVault",
    ]


def test_hidden_remote_path_cannot_be_opened_directly(monkeypatch):
    monkeypatch.setattr(api_browse, "_rclone_remotes", lambda: ["pcloud:"])
    monkeypatch.setattr(
        api_browse,
        "_hidden_remote_paths",
        lambda: {"pcloud:/crypto folder"},
    )

    with pytest.raises(HTTPException) as exc:
        api_browse.browse_rclone("pcloud:/Crypto Folder")

    assert exc.value.status_code == 403


def test_dot_segments_cannot_bypass_hidden_path(monkeypatch):
    monkeypatch.setattr(api_browse, "_rclone_remotes", lambda: ["pcloud:"])
    monkeypatch.setattr(
        api_browse,
        "_hidden_remote_paths",
        lambda: {"pcloud:/crypto folder"},
    )
    for path in ("pcloud:/./Crypto Folder", "pcloud://Crypto Folder"):
        with pytest.raises(HTTPException) as excinfo:
            api_browse.browse_rclone(path)
        assert excinfo.value.status_code == 403
    with pytest.raises(HTTPException) as excinfo:
        api_browse.browse_rclone("pcloud:/Fotos/../Crypto Folder")
    assert excinfo.value.status_code == 400


@pytest.mark.parametrize(
    "path",
    (
        "pcloud:../secret",
        "pcloud:/../secret",
        "pcloud:folder/../../secret",
        "pcloud:..",
    ),
)
def test_parent_segments_after_remote_separator_are_rejected(monkeypatch, path):
    monkeypatch.setattr(api_browse, "_rclone_remotes", lambda: ["pcloud:"])
    called = False

    def list_directories(_path):
        nonlocal called
        called = True
        return [], False

    monkeypatch.setattr(api_browse, "_rclone_directories", list_directories)

    with pytest.raises(HTTPException) as excinfo:
        api_browse.browse_rclone(path)

    assert excinfo.value.status_code == 400
    assert not called


def test_create_local_directory_stays_inside_configured_root(tmp_path, monkeypatch):
    monkeypatch.setattr(api_browse, "_browse_roots", lambda: [tmp_path.resolve()])

    result = api_browse.create_directory(
        api_browse.CreateDirectoryPayload(
            kind="local", parent=str(tmp_path), name="Neue Sicherung"
        )
    )

    created = tmp_path / "Neue Sicherung"
    assert created.is_dir()
    assert result == {"ok": True, "kind": "local", "path": str(created.resolve())}


def test_create_local_directory_rejects_parent_outside_root(tmp_path, monkeypatch):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    monkeypatch.setattr(api_browse, "_browse_roots", lambda: [root.resolve()])

    with pytest.raises(HTTPException) as exc:
        api_browse.create_directory(
            api_browse.CreateDirectoryPayload(
                kind="local", parent=str(outside), name="Nicht erlaubt"
            )
        )

    assert exc.value.status_code == 403


@pytest.mark.parametrize("name", ("..", ".versteckt", "a/b", "a\\b", "  "))
def test_create_directory_rejects_unsafe_names(name):
    with pytest.raises((HTTPException, ValueError)):
        payload = api_browse.CreateDirectoryPayload(
            kind="local", parent="/mnt", name=name
        )
        api_browse.create_directory(payload)


def test_create_remote_directory_uses_argument_boundary(monkeypatch):
    monkeypatch.setattr(api_browse, "_rclone_remotes", lambda: ["pcloud:"])
    monkeypatch.setattr(api_browse, "_rclone_directories", lambda _path: ([], False))
    monkeypatch.setattr(api_browse, "_hidden_remote_paths", lambda: set())
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(api_browse.subprocess, "run", fake_run)

    result = api_browse.create_directory(
        api_browse.CreateDirectoryPayload(
            kind="remote", parent="pcloud:/Fotos", name="2026 Urlaub"
        )
    )

    assert result == {
        "ok": True,
        "kind": "remote",
        "path": "pcloud:/Fotos/2026 Urlaub",
    }
    assert calls[0][0] == [
        "rclone",
        "mkdir",
        "--",
        "pcloud:/Fotos/2026 Urlaub",
    ]
    assert calls[0][1]["stdin"] is subprocess.DEVNULL


def test_create_remote_directory_rejects_existing_name(monkeypatch):
    monkeypatch.setattr(api_browse, "_rclone_remotes", lambda: ["pcloud:"])
    monkeypatch.setattr(
        api_browse, "_rclone_directories", lambda _path: (["Fotos"], False)
    )
    monkeypatch.setattr(api_browse, "_hidden_remote_paths", lambda: set())

    with pytest.raises(HTTPException) as exc:
        api_browse.create_directory(
            api_browse.CreateDirectoryPayload(
                kind="remote", parent="pcloud:", name="fotos"
            )
        )

    assert exc.value.status_code == 409


def test_create_remote_directory_cannot_enter_hidden_path(monkeypatch):
    monkeypatch.setattr(api_browse, "_rclone_remotes", lambda: ["pcloud:"])
    monkeypatch.setattr(
        api_browse, "_hidden_remote_paths", lambda: {"pcloud:/crypto folder"}
    )

    with pytest.raises(HTTPException) as exc:
        api_browse.create_directory(
            api_browse.CreateDirectoryPayload(
                kind="remote", parent="pcloud:", name="Crypto Folder"
            )
        )

    assert exc.value.status_code == 403
