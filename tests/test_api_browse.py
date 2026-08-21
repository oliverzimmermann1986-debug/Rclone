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
