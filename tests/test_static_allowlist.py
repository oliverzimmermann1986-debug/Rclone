from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import main
from app.static_assets import AllowlistedStaticFiles


def _mounted_static_app() -> AllowlistedStaticFiles:
    route = next(
        route for route in main.app.routes if getattr(route, "name", None) == "static"
    )
    assert isinstance(route.app, AllowlistedStaticFiles)
    return route.app


def test_production_static_mount_has_an_explicit_complete_allowlist():
    mounted = _mounted_static_app()

    assert mounted.allowed_files == main.STATIC_ASSET_ALLOWLIST
    assert main.STATIC_ASSET_ALLOWLIST == {
        "alpine.min.js",
        "app.js",
        "ui-helpers.js",
        "style.css",
        "manifest.json",
        "sw.js",
        "app-icon-192.png",
        "app-icon-512.png",
        "app-icon-1024.png",
        "webauthn.js",
    }
    assert all((main.STATIC_DIR / name).is_file() for name in mounted.allowed_files)
    assert "index.html" not in mounted.allowed_files
    assert "login.html" not in mounted.allowed_files


@pytest.fixture
def static_client(tmp_path: Path):
    (tmp_path / "app.js").write_bytes(b"console.log('ok');")
    (tmp_path / "index.html").write_text("private index", encoding="utf-8")
    (tmp_path / "login.html").write_text("private login", encoding="utf-8")
    (tmp_path / "_preview-audit.html").write_text("preview canary", encoding="utf-8")
    (tmp_path / "_stub-methods.js").write_text("stub canary", encoding="utf-8")
    (tmp_path / ".env").write_text("secret canary", encoding="utf-8")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "app.js").write_text("nested canary", encoding="utf-8")

    test_app = FastAPI()
    static_files = AllowlistedStaticFiles(
        directory=tmp_path,
        allowed_files={"app.js"},
    )
    test_app.mount("/static", static_files, name="static")
    with TestClient(test_app) as client:
        yield client, tmp_path


def test_allowed_asset_preserves_query_head_range_and_conditional_requests(
    static_client,
):
    client, _ = static_client

    response = client.get("/static/app.js?v=2.2.1")
    assert response.status_code == 200
    assert response.content == b"console.log('ok');"
    assert response.headers["content-type"].startswith("text/javascript")
    assert response.headers["accept-ranges"] == "bytes"

    head = client.head("/static/app.js?v=2.2.1")
    assert head.status_code == 200
    assert head.content == b""
    assert head.headers["content-length"] == str(len(response.content))

    partial = client.get("/static/app.js", headers={"Range": "bytes=0-6"})
    assert partial.status_code == 206
    assert partial.content == b"console"
    assert partial.headers["content-range"] == (f"bytes 0-6/{len(response.content)}")

    not_modified = client.get(
        "/static/app.js",
        headers={"If-None-Match": response.headers["etag"]},
    )
    assert not_modified.status_code == 304
    assert not_modified.content == b""


@pytest.mark.parametrize(
    "path",
    [
        "index.html",
        "login.html",
        "_preview-audit.html",
        "_stub-methods.js",
        ".env",
        "nested/app.js",
        "unknown-later.js",
    ],
)
def test_unlisted_files_fail_closed_without_path_leak(static_client, path: str):
    client, tmp_path = static_client
    if path == "unknown-later.js":
        (tmp_path / path).write_text("late canary", encoding="utf-8")

    response = client.get(f"/static/{path}?cache=bust")

    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}
    assert str(tmp_path) not in response.text
    assert "canary" not in response.text


def test_allowlist_rejects_hidden_or_nested_configuration(tmp_path: Path):
    with pytest.raises(ValueError, match="top-level filenames"):
        AllowlistedStaticFiles(
            directory=tmp_path,
            allowed_files={"app.js", "nested/extra.js", ".secret"},
        )
