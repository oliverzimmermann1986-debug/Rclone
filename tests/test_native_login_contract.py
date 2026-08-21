import json
from pathlib import Path

import bcrypt
import yaml
from fastapi.testclient import TestClient

from app import config_store, db
from app.config_store import Config
from app.db import Database
from app.jobs import locks


CONTRACT = json.loads(
    (Path(__file__).parents[1] / "contracts" / "native_login_contract.json").read_text(
        encoding="utf-8"
    )
)


def _client(tmp_path: Path, monkeypatch, *, max_failures: int = 10) -> TestClient:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "web": {
                    "username": "admin",
                    "password": "",
                    "password_hash": bcrypt.hashpw(
                        b"very-secure-password", bcrypt.gensalt(rounds=4)
                    ).decode(),
                    "secret_key": "test-secret-which-is-long-enough-123456789",
                    "session_version": 1,
                    "allowed_hosts": ["testserver"],
                    "login_max_failures": max_failures,
                    "login_lock_seconds": 60,
                },
                "paths": {
                    "data_dir": str(tmp_path),
                    "logs_dir": str(tmp_path),
                    "temp_dir": str(tmp_path),
                },
                "backup": {"enabled": True, "pairs": []},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_store, "_config", Config(config_path))
    monkeypatch.setattr(db, "_db", Database(tmp_path / "app.db"))
    monkeypatch.setattr(locks, "LOCK_DIR", tmp_path / "locks")
    from app import main

    return TestClient(main.app, base_url="http://testserver")


def _challenge(client: TestClient) -> str:
    expected = CONTRACT["challenge"]
    response = client.get(CONTRACT["endpoint"])
    assert response.status_code == expected["http_status"]
    body = response.json()
    assert body["status"] == expected["body"]["status"]
    assert len(body["login_csrf"]) >= 20
    return body["login_csrf"]


def _expected(name: str) -> dict:
    return next(item for item in CONTRACT["outcomes"] if item["name"] == name)


def test_native_login_success_and_browser_login_remains_redirect_based(
    tmp_path: Path, monkeypatch
):
    with _client(tmp_path, monkeypatch) as client:
        nonce = _challenge(client)
        expected = _expected("success")
        response = client.post(
            CONTRACT["endpoint"],
            json={
                "username": "admin",
                "password": "very-secure-password",
                "login_csrf": nonce,
            },
            headers={"Origin": "http://testserver"},
            follow_redirects=False,
        )
        assert response.status_code == expected["http_status"]
        assert response.json() == expected["body"]
        assert client.cookies.get("rclone_sync_session")
        assert client.cookies.get("rclone_sync_csrf")

        browser_page = client.get("/login")
        browser_nonce = browser_page.cookies.get("rclone_sync_login_csrf")
        browser = client.post(
            "/login",
            data={
                "username": "admin",
                "password": "very-secure-password",
                "login_csrf": browser_nonce,
            },
            headers={"Origin": "http://testserver"},
            follow_redirects=False,
        )
        assert browser.status_code == 303
        assert browser.headers["location"] == "/"


def test_native_login_reports_invalid_csrf_and_origin_separately(
    tmp_path: Path, monkeypatch
):
    with _client(tmp_path, monkeypatch) as client:
        _challenge(client)
        csrf_expected = _expected("csrf_failed")
        csrf = client.post(
            CONTRACT["endpoint"],
            json={
                "username": "admin",
                "password": "very-secure-password",
                "login_csrf": "wrong-login-csrf-token-1234567890",
            },
            headers={"Origin": "http://testserver"},
        )
        assert csrf.status_code == csrf_expected["http_status"]
        assert csrf.json() == csrf_expected["body"]

        origin_expected = _expected("origin_failed")
        origin = client.post(
            CONTRACT["endpoint"],
            json={
                "username": "admin",
                "password": "very-secure-password",
                "login_csrf": "wrong-login-csrf-token-1234567890",
            },
            headers={"Origin": "https://evil.example"},
        )
        assert origin.status_code == origin_expected["http_status"]
        assert origin.json() == origin_expected["body"]


def test_native_login_reports_invalid_credentials_and_retry_seconds(
    tmp_path: Path, monkeypatch
):
    with _client(tmp_path, monkeypatch, max_failures=3) as client:
        nonce = _challenge(client)
        request = {
            "username": "admin",
            "password": "wrong-password",
            "login_csrf": nonce,
        }
        invalid_expected = _expected("invalid_credentials")
        for _ in range(2):
            invalid = client.post(
                CONTRACT["endpoint"],
                json=request,
                headers={"Origin": "http://testserver"},
            )
            assert invalid.status_code == invalid_expected["http_status"]
            assert invalid.json() == invalid_expected["body"]

        rate_expected = _expected("rate_limited")
        limited = client.post(
            CONTRACT["endpoint"],
            json=request,
            headers={"Origin": "http://testserver"},
        )
        assert limited.status_code == rate_expected["http_status"]
        assert limited.json()["status"] == rate_expected["body"]["status"]
        assert limited.json()["retry_after_seconds"] > 0
        assert int(limited.headers["Retry-After"]) > 0
