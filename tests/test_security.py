from pathlib import Path

from app import auth
from app.security import is_relative_to


def test_path_prefix_is_not_treated_as_containment(tmp_path: Path):
    root = tmp_path / "logs"
    sibling = tmp_path / "logs-evil" / "file.log"
    root.mkdir()
    sibling.parent.mkdir()
    sibling.write_text("x", encoding="utf-8")
    assert is_relative_to(sibling, root) is False


class _UnicodeAuthConfig:
    def __init__(self, username: str, password: str):
        self.data = {
            "web": {
                "username": username,
                "password": password,
                "password_hash": "",
            }
        }

    def get(self, *keys, default=None):
        value = self.data
        for key in keys:
            if not isinstance(value, dict) or key not in value:
                return default
            value = value[key]
        return value

    def update(self, updater):
        updater(self.data)


def test_unicode_credentials_are_compared_without_ascii_type_error(monkeypatch):
    config = _UnicodeAuthConfig("Jörg", "sëcret-password")
    monkeypatch.setattr(auth, "get_config", lambda: config)

    assert auth.verify_password("JÖRG", "sëcret-password") is True
    assert config.get("web", "password") == ""
    assert config.get("web", "password_hash").startswith("$2")


def test_wrong_unicode_credentials_return_false_instead_of_raising(monkeypatch):
    config = _UnicodeAuthConfig("admin", "plain-password")
    monkeypatch.setattr(auth, "get_config", lambda: config)

    assert auth.verify_password("admïn", "plain-password") is False
    assert auth.verify_password("admin", "pässword") is False
