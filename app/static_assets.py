"""Fail-closed delivery for the web application's public static assets."""

from __future__ import annotations

from collections.abc import Collection
from hashlib import sha256
from os import PathLike
from pathlib import Path

from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope


WEB_CACHE_ASSETS = (
    "style.css",
    "alpine.min.js",
    "ui-helpers.js",
    "app.js",
    "manifest.json",
    "app-icon-1024.png",
)


def web_asset_cache_revision(directory: Path, version: str) -> str:
    """Bind the public web bundle's cache key to its current file contents."""

    manifest = sha256()
    for name in WEB_CACHE_ASSETS:
        manifest.update(name.encode("utf-8") + b"\0")
        manifest.update(sha256((directory / name).read_bytes()).digest())
    return f"{version}-{manifest.hexdigest()[:16]}"


class AllowlistedStaticFiles(StaticFiles):
    """Serve only explicitly named, top-level files from a static directory.

    Keeping the allowlist in front of :class:`StaticFiles` prevents development,
    preview, or diagnostic artifacts from becoming public merely because they
    were copied into the directory.  Allowed responses are still produced by
    Starlette so MIME types, HEAD, range, and conditional requests retain their
    normal behavior.
    """

    def __init__(
        self,
        *,
        directory: PathLike[str] | str,
        allowed_files: Collection[str],
        check_dir: bool = True,
    ) -> None:
        allowed = frozenset(allowed_files)
        invalid = sorted(
            name
            for name in allowed
            if not name
            or name.startswith(".")
            or "/" in name
            or "\\" in name
            or name in {".", ".."}
        )
        if invalid:
            raise ValueError(
                "Static allowlist entries must be visible top-level filenames: "
                + ", ".join(repr(name) for name in invalid)
            )
        self.allowed_files = allowed
        super().__init__(directory=directory, check_dir=check_dir, html=False)

    async def get_response(self, path: str, scope: Scope) -> Response:
        if path not in self.allowed_files:
            raise HTTPException(status_code=404)
        return await super().get_response(path, scope)
