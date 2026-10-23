"""Database settings: special-char-safe URL building and DB_* env overrides.

README's external-DB docker-compose promises DB_HOST/DB_PORT/DB_NAME/DB_USER/
DB_PASSWORD; previously those env vars were ignored and the DB was taken only
from YAML. Passwords with @, : or / broke the f-string URL (same defect was
found in the Go port).
"""

import pytest
from sqlalchemy import make_url

from src.config import get_settings
from src.config.settings import DatabaseSettings


def test_database_url_roundtrips_special_chars():
    db = DatabaseSettings(
        username="tg user", password="p@ss/wo:rd", host="db.local", port=5432, name="tgdb"
    )
    url = make_url(db.url)
    assert url.username == "tg user"
    assert url.password == "p@ss/wo:rd"
    assert url.host == "db.local"
    assert url.database == "tgdb"


def test_simple_url_unchanged():
    db = DatabaseSettings(username="telegram_mcp", password="pw", host="postgres", port=5432, name="db")
    url = make_url(db.url)
    assert url.username == "telegram_mcp"
    assert url.password == "pw"
    assert url.host == "postgres"


def test_db_env_overrides_win_over_defaults(monkeypatch):
    monkeypatch.setenv("DB_HOST", "env-host")
    monkeypatch.setenv("DB_PORT", "6543")
    monkeypatch.setenv("DB_USER", "env-user")
    monkeypatch.setenv("DB_PASSWORD", "env-pass")
    monkeypatch.setenv("DB_NAME", "env-db")
    get_settings.cache_clear()
    try:
        settings = get_settings()
        db = settings.database
        assert db.host == "env-host"
        assert db.port == 6543
        assert db.username == "env-user"
        assert db.password == "env-pass"
        assert db.name == "env-db"
        url = make_url(db.url)
        assert url.host == "env-host"
        assert url.database == "env-db"
    finally:
        get_settings.cache_clear()


def test_env_absent_keeps_yaml_style_defaults(monkeypatch):
    for var in ("DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD", "DB_NAME"):
        monkeypatch.delenv(var, raising=False)
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.database.host == "postgres"  # YAML default from get_settings
        assert settings.database.name == "telegram_mcp"
    finally:
        get_settings.cache_clear()
