from app.secret_redaction import REDACTED, is_secret_key, redact_secrets


def test_unknown_nested_secrets_and_list_headers_are_redacted():
    payload = {
        "plugin": {
            "vendorCredentialV2": "credential-canary",
            "nested": [
                {"headers": {"X-Auth-Token": "header-canary"}},
                {"name": "Authorization", "value": "Bearer descriptor-canary"},
                {"serviceSigningKey": "signing-canary"},
                {"opaque_material": "aB3dE5fG7hI9jK1mN3pQ5rS7tU9vW1xY"},
            ],
        }
    }

    redacted = redact_secrets(payload)

    assert redacted["plugin"]["vendorCredentialV2"] == REDACTED
    assert redacted["plugin"]["nested"][0]["headers"]["X-Auth-Token"] == REDACTED
    assert redacted["plugin"]["nested"][1]["value"] == REDACTED
    assert redacted["plugin"]["nested"][2]["serviceSigningKey"] == REDACTED
    assert redacted["plugin"]["nested"][3]["opaque_material"] == REDACTED
    assert not any(
        canary in str(redacted)
        for canary in (
            "credential-canary",
            "header-canary",
            "descriptor-canary",
            "signing-canary",
            "aB3dE5fG7hI9jK1mN3pQ5rS7tU9vW1xY",
        )
    )


def test_urls_and_structured_tokens_are_redacted_without_hiding_safe_parts():
    payload = {
        "callback": (
            "request https://reviewer:password@example.test/hook?"
            "page=2&access_token=query-canary#result"
        ),
        "message": "Authorization: Bearer bearer-canary-value",
        "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature123",
    }

    redacted = redact_secrets(payload)

    callback = redacted["callback"]
    assert "reviewer" not in callback
    assert "password" not in callback
    assert "query-canary" not in callback
    assert "page=2" in callback
    assert "example.test/hook" in callback
    assert "bearer-canary-value" not in redacted["message"]
    assert redacted["jwt"] == REDACTED


def test_harmless_metadata_and_similar_words_remain_visible():
    payload = {
        "token_count": 12,
        "credential_type": "oauth",
        "password_policy": "strong",
        "authorization_mode": "interactive",
        "monkey": "banana",
        "public_key_name": "review-key",
        "endpoint": "https://example.test/api?page=2",
        "revision_hash": "aB3dE5fG7hI9jK1mN3pQ5rS7tU9vW1xY",
    }

    assert redact_secrets(payload) == payload
    assert not is_secret_key("token_count")
    assert not is_secret_key("monkey")
    assert is_secret_key("newVendorRefreshTokenV3")


def test_secret_like_numeric_operational_metadata_is_not_needlessly_redacted():
    payload = {"token_count": 4, "secret_present": True, "password_length": 72}

    assert redact_secrets(payload) == payload
