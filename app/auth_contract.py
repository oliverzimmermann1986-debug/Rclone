"""Stable JSON contract used by native clients during authentication."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

NATIVE_LOGIN_PATH = "/api/auth/login"


class NativeLoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)
    login_csrf: str = Field(min_length=20, max_length=256)


class NativeLoginChallenge(BaseModel):
    status: Literal["csrf_ready"] = "csrf_ready"
    login_csrf: str


class NativeLoginResponse(BaseModel):
    status: Literal[
        "success",
        "invalid_credentials",
        "rate_limited",
        "csrf_failed",
        "origin_failed",
    ]
    retry_after_seconds: int | None = None
