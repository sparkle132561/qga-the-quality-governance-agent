"""JWT会话密钥fail-fast：非fake模式拒绝静默随机默认。"""

import pytest
from pydantic import ValidationError
from quality.config import Settings


def test_fake_mode_generates_session_secret_when_unset():
    settings = Settings(database_url="sqlite:///:memory:", model_mode="fake")
    assert settings.jwt_secret and len(settings.jwt_secret) >= 32


def test_live_mode_without_explicit_secret_fails_fast():
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        Settings(database_url="sqlite:///:memory:", model_mode="live")


def test_live_mode_accepts_explicit_secret():
    settings = Settings(
        database_url="sqlite:///:memory:", model_mode="live", jwt_secret="explicit-value"
    )
    assert settings.jwt_secret == "explicit-value"
