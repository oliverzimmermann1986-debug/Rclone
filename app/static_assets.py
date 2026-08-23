"""Fail-closed delivery for the web application's public static assets."""

from __future__ import annotations

from collections.abc import Collection
from os import PathLike

from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope


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
