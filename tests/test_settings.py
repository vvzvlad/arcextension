import pytest
from pydantic import ValidationError

from src.settings import Settings


def test_loads_from_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "abc:123")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    s = Settings(_env_file=None)
    assert s.telegram_bot_token == "abc:123"
    assert s.log_level == "DEBUG"


def test_missing_credential_fails(monkeypatch):
    # A missing credential must blow up at construction time, not silently default.
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
