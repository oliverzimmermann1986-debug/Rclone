"""Conservative recursive redaction for exported support data."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REDACTED = "***REDACTED***"

_WORD_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|[^A-Za-z0-9]+")
_DIRECT_SECRET_TOKENS = {
    "auth",
    "authorization",
    "bearer",
    "cookie",
    "credential",
    "credentials",
    "passwd",
    "passphrase",
    "password",
    "secret",
    "signature",
    "token",
}
_SECRET_KEY_PAIRS = {
    ("access", "key"),
    ("api", "key"),
    ("client", "key"),
    ("customer", "key"),
    ("encryption", "key"),
    ("private", "key"),
    ("session", "id"),
    ("session", "key"),
    ("signing", "key"),
}
_SAFE_METADATA_SUFFIXES = {
    "algorithm",
    "count",
    "enabled",
    "expires",
    "expiry",
    "hint",
    "last4",
    "length",
    "mode",
    "name",
    "policy",
    "prefix",
    "present",
    "provider",
    "required",
    "status",
    "type",
    "url",
}
_SENSITIVE_HEADER_NAMES = {
    "authorization",
    "cookie",
    "proxy-authorization",
    "set-cookie",
    "x-api-key",
    "x-auth-token",
}
_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_AUTH_SCHEME_RE = re.compile(r"(?i)\b(?P<scheme>bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_KNOWN_TOKEN_RE = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,}|"
    r"xox[baprs]-[A-Za-z0-9-]{16,}|AKIA[A-Z0-9]{16})\b"
)
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----.*?"
    r"-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY-----",
    re.DOTALL,
)
_HIGH_ENTROPY_RE = re.compile(r"^[A-Za-z0-9_+/=-]{32,512}$")
_SAFE_VALUE_PATH_WORDS = {
    "checksum",
    "digest",
    "fingerprint",
    "hash",
    "id",
    "revision",
    "sha1",
    "sha256",
    "uuid",
}


def _key_words(key: object) -> tuple[str, ...]:
    return tuple(word.casefold() for word in _WORD_BOUNDARY_RE.split(str(key)) if word)


def is_secret_key(key: object) -> bool:
    """Classify secret-bearing field names without substring false positives."""

    raw = str(key).strip().casefold()
    if raw in _SENSITIVE_HEADER_NAMES:
        return True
    words = _key_words(key)
    if not words:
        return False
    if words[-1] in _SAFE_METADATA_SUFFIXES:
        return False
    if any(word in _DIRECT_SECRET_TOKENS for word in words):
        return True
    word_pairs = set(zip(words, words[1:]))
    return bool(word_pairs & _SECRET_KEY_PAIRS)


def _redact_url(raw_url: str, placeholder: str) -> str:
    trailing = ""
    while raw_url and raw_url[-1] in ").,;]}":
        trailing = raw_url[-1] + trailing
        raw_url = raw_url[:-1]
    try:
        parsed = urlsplit(raw_url)
        hostname = parsed.hostname
        if not hostname:
            return raw_url + trailing
        host = f"[{hostname}]" if ":" in hostname else hostname
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        netloc = f"{placeholder}@{host}" if parsed.username is not None else host
        query = urlencode(
            [
                (key, placeholder if is_secret_key(key) else value)
                for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            ],
            doseq=True,
        )
        return (
            urlunsplit((parsed.scheme, netloc, parsed.path, query, parsed.fragment))
            + trailing
        )
    except ValueError:
        return placeholder + trailing


def redact_secret_text(value: str, *, placeholder: str = REDACTED) -> str:
    """Redact structured secrets and secret-bearing URLs inside free text."""

    text = _PRIVATE_KEY_RE.sub(placeholder, str(value))
    text = _AUTH_SCHEME_RE.sub(
        lambda match: f"{match.group('scheme')} {placeholder}", text
    )
    text = _JWT_RE.sub(placeholder, text)
    text = _KNOWN_TOKEN_RE.sub(placeholder, text)
    return _URL_RE.sub(lambda match: _redact_url(match.group(0), placeholder), text)


def _looks_like_opaque_secret(value: str, path: tuple[str, ...]) -> bool:
    """Recognize high-entropy credential material while retaining hashes and IDs."""

    if not _HIGH_ENTROPY_RE.fullmatch(value):
        return False
    path_words = {word for part in path for word in _key_words(part)}
    if path_words & _SAFE_VALUE_PATH_WORDS:
        return False
    return (
        any(character.islower() for character in value)
        and any(character.isupper() for character in value)
        and any(character.isdigit() for character in value)
    )


def _descriptor_secret_key(value: Mapping[object, Any]) -> object | None:
    """Detect ``{name/key: Authorization, value: ...}`` style header lists."""

    for descriptor in ("name", "key", "field", "header"):
        candidate = next(
            (raw_key for raw_key in value if str(raw_key).casefold() == descriptor),
            None,
        )
        if candidate is not None and is_secret_key(value[candidate]):
            return candidate
    return None


def redact_secrets(
    value: Any,
    *,
    placeholder: str = REDACTED,
    path: tuple[str, ...] = (),
) -> Any:
    """Return a recursively redacted copy of dictionaries, lists and strings."""

    if isinstance(value, Mapping):
        descriptor = _descriptor_secret_key(value)
        redacted: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            key_path = (*path, key)
            in_headers = any(part.casefold() in {"header", "headers"} for part in path)
            descriptor_value = descriptor is not None and key.casefold() in {
                "value",
                "values",
            }
            if (
                item not in (None, "")
                and not isinstance(item, (bool, int, float))
                and (
                    is_secret_key(key)
                    or (in_headers and is_secret_key(key))
                    or descriptor_value
                )
            ):
                redacted[key] = placeholder
            else:
                redacted[key] = redact_secrets(
                    item, placeholder=placeholder, path=key_path
                )
        return redacted
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            redact_secrets(item, placeholder=placeholder, path=(*path, str(index)))
            for index, item in enumerate(value)
        ]
    if isinstance(value, str):
        redacted = redact_secret_text(value, placeholder=placeholder)
        if redacted == value and _looks_like_opaque_secret(value, path):
            return placeholder
        return redacted
    return value
