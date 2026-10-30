import json
from types import SimpleNamespace

import pytest

from app import s3_object_lock as s3
from app.s3_object_lock import EMPTY_SHA256, evaluate, object_lock_report, sign_request

SECRET = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
NS = ' xmlns="http://s3.amazonaws.com/doc/2006-03-01/"'
LOCK_COMPLIANCE = (
    f"<ObjectLockConfiguration{NS}><ObjectLockEnabled>Enabled</ObjectLockEnabled>"
    "<Rule><DefaultRetention><Mode>COMPLIANCE</Mode><Days>30</Days>"
    "</DefaultRetention></Rule></ObjectLockConfiguration>"
)
LOCK_NO_RULE = (
    f"<ObjectLockConfiguration{NS}><ObjectLockEnabled>Enabled</ObjectLockEnabled>"
    "</ObjectLockConfiguration>"
)
NOT_FOUND = "<Error><Code>ObjectLockConfigurationNotFoundError</Code></Error>"
VERSIONED = (
    f"<VersioningConfiguration{NS}><Status>Enabled</Status></VersioningConfiguration>"
)
UNVERSIONED = f"<VersioningConfiguration{NS}/>"


@pytest.fixture(autouse=True)
def _reset_cache():
    s3._cache = None
    yield
    s3._cache = None


def test_sigv4_get_vanilla_vector():
    headers = sign_request(
        "GET",
        "https://example.amazonaws.com/",
        access_key="AKIDEXAMPLE",
        secret_key=SECRET,
        region="us-east-1",
        service="service",
        amz_date="20150830T123600Z",
        payload_hash=EMPTY_SHA256,
    )
    assert headers["Host"] == "example.amazonaws.com"
    assert headers["Authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/service/aws4_request, "
        "SignedHeaders=host;x-amz-date, "
        "Signature=5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31"
    )


def test_s3_signs_content_sha_header():
    headers = sign_request(
        "GET",
        "https://s3.eu-central-1.amazonaws.com/b?versioning",
        access_key="AK",
        secret_key="SK",
        region="eu-central-1",
        service="s3",
        amz_date="20260101T000000Z",
        payload_hash=EMPTY_SHA256,
        extra_headers={"x-amz-security-token": "tok"},
    )
    assert headers["x-amz-content-sha256"] == EMPTY_SHA256
    assert (
        "SignedHeaders=host;x-amz-content-sha256;x-amz-date;x-amz-security-token,"
        in (headers["Authorization"])
    )


def test_canonical_query_sorts_and_encodes():
    assert s3._canonical_query("versioning") == "versioning="
    assert (
        s3._canonical_query("b=2&a=x y&a=1&k=a/b~c&object-lock")
        == "a=1&a=x%20y&b=2&k=a%2Fb~c&object-lock="
    )
    assert s3._canonical_query("k=a%2Fb") == s3._canonical_query("k=a/b")


@pytest.mark.parametrize(
    ("lock_xml", "status", "versioning_xml", "level", "retention", "versioning"),
    [
        (
            LOCK_COMPLIANCE,
            200,
            VERSIONED,
            "immutable",
            {"mode": "COMPLIANCE", "days": 30},
            True,
        ),
        (
            LOCK_COMPLIANCE.replace("COMPLIANCE", "GOVERNANCE").replace(
                "<Days>30</Days>", "<Years>1</Years>"
            ),
            200,
            VERSIONED,
            "immutable",
            {"mode": "GOVERNANCE", "years": 1},
            True,
        ),
        (LOCK_NO_RULE, 200, VERSIONED, "lock_no_default", None, True),
        (NOT_FOUND, 404, VERSIONED, "versioned", None, True),
        (NOT_FOUND, 400, VERSIONED, "versioned", None, True),
        (NOT_FOUND, 404, UNVERSIONED, "unprotected", None, False),
        (NOT_FOUND, 404, None, "unknown", None, None),
        ("<Error/>", 403, VERSIONED, "unknown", None, True),
        ("<Error/>", 500, None, "unknown", None, None),
    ],
)
def test_evaluate_levels(
    lock_xml, status, versioning_xml, level, retention, versioning
):
    result = evaluate(lock_xml, status, versioning_xml)
    assert (result["level"], result["retention"], result["versioning"]) == (
        level,
        retention,
        versioning,
    )
    assert result["label"] and result["hint"]


def test_evaluate_labels_and_hints():
    assert evaluate(LOCK_COMPLIANCE, 200, VERSIONED)["label"] == "Ransomware-immun"
    assert "30 Tage" in evaluate(LOCK_COMPLIANCE, 200, VERSIONED)["hint"]
    assert evaluate(NOT_FOUND, 404, VERSIONED)["label"] == "versioniert"
    assert evaluate(NOT_FOUND, 404, UNVERSIONED)["label"] == "ohne Schutz"
    assert evaluate("", 403, None)["hint"] == (
        "keine Berechtigung (s3:GetBucketObjectLockConfiguration)"
    )
    assert "explizit" in evaluate(LOCK_NO_RULE, 200, None)["hint"]


def test_targets_from_enabled_pairs():
    cfg = {
        "backup": {
            "pairs": [
                {"remote": "aws:/bucket-a/pre/fix", "enabled": True},
                {"remote": "aws:bucket-a/other"},
                {"remote": "aws,region=x:bucket-b"},
                {"remote": "aws:bucket-c", "enabled": False},
                {"remote": "aws:"},
                {"remote": "/srv/local"},
                "kaputt",
            ]
        }
    }
    assert s3._targets(cfg) == (("aws", "bucket-a"), ("aws", "bucket-b"))


def _dump(**remotes):
    return lambda args, **kwargs: SimpleNamespace(
        returncode=0, stdout=json.dumps(remotes), stderr=""
    )


AWS = {
    "type": "s3",
    "provider": "AWS",
    "access_key_id": "AKID",
    "secret_access_key": SECRET,
}
CFG = {
    "backup": {
        "pairs": [
            {"remote": "aws:/vault/daily", "enabled": True},
            {"remote": "gdrive:backup", "enabled": True},
            {"remote": "envs3:other", "enabled": True},
        ]
    }
}


def test_report_cache_and_refresh():
    calls = []

    def fetch(url, headers):
        calls.append((url, headers))
        return (
            (200, LOCK_COMPLIANCE) if url.endswith("object-lock") else (200, VERSIONED)
        )

    run = _dump(
        aws=AWS, gdrive={"type": "drive"}, envs3={"type": "s3", "env_auth": "true"}
    )
    report = object_lock_report(CFG, run=run, fetch=fetch)
    assert [(b["remote"], b["bucket"], b["level"]) for b in report["buckets"]] == [
        ("aws", "vault", "immutable"),
        ("envs3", "other", "unknown"),
    ]
    assert report["buckets"][1]["hint"] == "Zugangsdaten nicht aus rclone-Config lesbar"
    assert [url for url, _ in calls] == [
        "https://s3.us-east-1.amazonaws.com/vault?object-lock",
        "https://s3.us-east-1.amazonaws.com/vault?versioning",
    ]
    assert calls[0][1]["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKID/")

    assert object_lock_report(CFG, run=run, fetch=fetch) is report
    assert len(calls) == 2
    assert object_lock_report(CFG, run=run, fetch=fetch, refresh=True) is not report
    assert len(calls) == 4


def test_report_custom_endpoint_and_network_error():
    def fetch(url, headers):
        raise OSError(f"connection refused {SECRET}")

    conf = {**AWS, "endpoint": "http://minio:9000/", "region": "eu"}
    report = object_lock_report(CFG, run=_dump(aws=conf), fetch=fetch)
    bucket = report["buckets"][0]
    assert (bucket["level"], bucket["error"]) == (
        "unknown",
        "Verbindung fehlgeschlagen (OSError)",
    )


def test_rclone_config_error_gives_empty_list_and_is_not_cached():
    def failing(args, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr=f"boom {SECRET}")

    report = object_lock_report(CFG, run=failing, fetch=None)
    assert report["buckets"] == []
    assert report["error"] == "rclone-Konfiguration nicht lesbar"
    assert s3._cache is None

    def missing(args, **kwargs):
        raise FileNotFoundError("rclone")

    assert object_lock_report(CFG, run=missing)["buckets"] == []


def test_secret_key_never_returned():
    for status, body in (
        (403, f"<Error><StringToSign>{SECRET}</StringToSign></Error>"),
        (200, LOCK_COMPLIANCE),
    ):
        s3._cache = None
        report = object_lock_report(
            CFG, run=_dump(aws=AWS), fetch=lambda url, headers: (status, body)
        )
        assert report["buckets"]
        assert SECRET not in json.dumps(report)


def test_route_registered():
    from app.routes.api_object_lock import router

    assert any(
        route.path == "/api/storage/object-lock" and "GET" in route.methods
        for route in router.routes
    )
