"""Configuration loading, the registry, and the doctor report.

Environment is driven through monkeypatch — no real `.env` is ever read.
"""

from __future__ import annotations

import pytest

from pap.config import ConfigError, load_settings
from pap.core.registry import available, get
from pap.ops.doctor import MISSING, OK, collect_checks, render

REQUIRED = {"PG_DB": "pap", "PG_USER": "pap", "PG_PASSWORD": "a-long-enough-password"}


@pytest.fixture
def env(monkeypatch):
    """A clean environment holding only what the test sets."""
    for key in list(__import__("os").environ):
        if key.split("_")[0] in {"PG", "TELEGRAM", "SMTP", "EMAIL", "GOOGLE", "SENTRY",
                                 "HEALTHCHECKS", "BACKUP", "PAP", "LLM", "STUDEO",
                                 "ANTHROPIC", "OPENAI", "GEMINI", "HTTP", "RETRY", "LOG"}:
            monkeypatch.delenv(key, raising=False)
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)
    return monkeypatch


def _settings(env, **extra):
    for key, value in extra.items():
        env.setenv(key, value)
    # dotenv_path pointing at a nonexistent file keeps a developer's real .env
    # from leaking into the test run.
    return load_settings(dotenv_path="/nonexistent/.env")


# -- required values --------------------------------------------------------
@pytest.mark.parametrize("missing", ["PG_DB", "PG_USER", "PG_PASSWORD"])
def test_missing_required_value_fails_fast(env, missing):
    env.delenv(missing, raising=False)
    with pytest.raises(ConfigError) as exc:
        _settings(env)
    assert missing in str(exc.value)


def test_a_blank_value_counts_as_missing(env):
    env.setenv("PG_DB", "   ")
    with pytest.raises(ConfigError):
        _settings(env)


def test_non_numeric_port_is_rejected_with_the_variable_name(env):
    with pytest.raises(ConfigError) as exc:
        _settings(env, PG_PORT="not-a-number")
    assert "PG_PORT" in str(exc.value)


# -- defaults and derived state --------------------------------------------
def test_defaults_are_applied(env):
    settings = _settings(env)
    assert settings.pg_port == 5432
    assert settings.log_level == "INFO"
    assert settings.http.timeout_seconds == 30


def test_conninfo_safe_never_contains_the_password(env):
    settings = _settings(env)
    assert "a-long-enough-password" not in settings.conninfo_safe
    assert "dbname=pap" in settings.conninfo_safe


@pytest.mark.parametrize("password", [
    "simple",
    "@passwordTemporaly",          # '@' — fine in key=value, breaks a URL-style DSN
    "with a space",                # would produce "missing = after" without quoting
    "with'quote",
    "back\\slash",
])
def test_conninfo_escapes_awkward_passwords(env, password):
    """Hand-formatting the connection string works until someone picks a password
    with a space in it, then fails as a libpq parse error that looks nothing like
    a credential problem. make_conninfo escapes; round-tripping proves it."""
    from psycopg.conninfo import conninfo_to_dict

    from pap.db import conninfo

    env.setenv("PG_PASSWORD", password)
    settings = _settings(env)
    parsed = conninfo_to_dict(conninfo(settings))
    assert parsed["password"] == password
    assert parsed["dbname"] == "pap"


def test_conninfo_can_target_another_database(env):
    """Backup verification restores into a throwaway database on the same server."""
    from psycopg.conninfo import conninfo_to_dict

    from pap.db import conninfo

    parsed = conninfo_to_dict(conninfo(_settings(env), dbname="pap_verify_1"))
    assert parsed["dbname"] == "pap_verify_1"


def test_secret_values_are_registered_for_scrubbing(env):
    settings = _settings(env, TELEGRAM_BOT_TOKEN="12345:aaaaaaaaaaaaaaaa")
    assert "a-long-enough-password" in settings.secret_values
    assert "12345:aaaaaaaaaaaaaaaa" in settings.secret_values


def test_short_values_are_not_registered_for_scrubbing(env):
    """Redacting a 3-character string would corrupt unrelated text throughout an
    error report, which is worse than not redacting it."""
    settings = _settings(env, TELEGRAM_BOT_TOKEN="abc")
    assert "abc" not in settings.secret_values


# -- feature gating ---------------------------------------------------------
def test_telegram_needs_both_token_and_chat_id(env):
    assert not _settings(env, TELEGRAM_BOT_TOKEN="t-123456789").telegram.configured
    assert _settings(env, TELEGRAM_BOT_TOKEN="t-123456789",
                     TELEGRAM_CHAT_ID="99").telegram.configured


def test_disabled_beats_configured(env):
    settings = _settings(env, TELEGRAM_ENABLED="false", TELEGRAM_BOT_TOKEN="t-123456789",
                         TELEGRAM_CHAT_ID="99")
    assert not settings.telegram.configured


def test_email_recipients_are_split_and_trimmed(env):
    settings = _settings(env, EMAIL_TO=" a@example.com , b@example.com ")
    assert settings.email.recipients == ("a@example.com", "b@example.com")


# -- healthchecks ping urls -------------------------------------------------
def test_ping_url_is_none_without_a_key(env):
    assert _settings(env).observability.ping_url("studeo") is None


def test_ping_url_uses_the_slug_prefix(env):
    settings = _settings(env, HEALTHCHECKS_BASE_URL="http://hc:8000/",
                         HEALTHCHECKS_PING_KEY="key-123")
    assert settings.observability.ping_url("studeo") == "http://hc:8000/ping/key-123/pap-studeo"


# -- registry ---------------------------------------------------------------
def test_the_fake_source_is_registered():
    import pap.sources  # noqa: F401  (import registers the adapters)

    assert "fake" in available()
    assert get("fake").name == "fake"


def test_an_unknown_source_names_the_ones_that_exist():
    import pap.sources  # noqa: F401

    with pytest.raises(KeyError) as exc:
        get("nope")
    assert "fake" in str(exc.value)


# -- doctor -----------------------------------------------------------------
def test_doctor_never_prints_a_value(env):
    """The whole point of the command: safe to paste anywhere, including here."""
    settings = _settings(env, TELEGRAM_BOT_TOKEN="12345:secret-token-value",
                         TELEGRAM_CHAT_ID="99", SMTP_PASSWORD="smtp-secret-value")
    output = render(collect_checks(settings, check_db=False))
    assert "secret-token-value" not in output
    assert "smtp-secret-value" not in output
    assert "a-long-enough-password" not in output
    assert "TELEGRAM_BOT_TOKEN" in output


@pytest.mark.parametrize("chat_id", ["987654321", "-1001234567890", "@meucanal"])
def test_doctor_accepts_valid_chat_id_shapes(env, chat_id):
    settings = _settings(env, TELEGRAM_BOT_TOKEN="12345:aaaaaaaa", TELEGRAM_CHAT_ID=chat_id)
    states = {c.name: c.state for c in collect_checks(settings, check_db=False)}
    assert states["TELEGRAM_CHAT_ID"] == OK


@pytest.mark.parametrize("chat_id", ["pap-server_bot", "meu_usuario", "not an id"])
def test_doctor_rejects_a_username_as_chat_id(env, chat_id):
    """Telegram answers `400: chat not found` at send time, naming neither the
    variable nor the reason — so the shape is worth checking up front."""
    settings = _settings(env, TELEGRAM_BOT_TOKEN="12345:aaaaaaaa", TELEGRAM_CHAT_ID=chat_id)
    checks = {c.name: c for c in collect_checks(settings, check_db=False)}
    assert checks["TELEGRAM_CHAT_ID"].state == MISSING
    assert "numeric chat id" in checks["TELEGRAM_CHAT_ID"].detail


def test_doctor_calls_out_a_bot_username_specifically(env):
    settings = _settings(env, TELEGRAM_BOT_TOKEN="12345:aaaaaaaa",
                         TELEGRAM_CHAT_ID="pap-server_bot")
    checks = {c.name: c for c in collect_checks(settings, check_db=False)}
    assert "cannot message itself" in checks["TELEGRAM_CHAT_ID"].detail


def test_doctor_flags_having_no_delivery_channel_at_all(env):
    settings = _settings(env, TELEGRAM_ENABLED="false", EMAIL_ENABLED="false")
    checks = collect_checks(settings, check_db=False)
    assert any(c.name == "any channel" and c.state == MISSING for c in checks)


def test_doctor_is_quiet_when_a_channel_works(env):
    settings = _settings(env, TELEGRAM_BOT_TOKEN="12345:aaaaaaaa", TELEGRAM_CHAT_ID="99")
    checks = collect_checks(settings, check_db=False)
    assert not any(c.name == "any channel" for c in checks)


def test_doctor_warns_that_backups_stay_on_the_failing_disk(env):
    """Google unconfigured means dumps never leave the machine — which defeats the
    purpose when the local disk is the thing expected to fail."""
    settings = _settings(env, BACKUP_ENABLED="true", GOOGLE_ENABLED="false")
    checks = collect_checks(settings, check_db=False)
    assert any(c.name == "off-machine copy" and c.state == MISSING for c in checks)


def test_doctor_reports_observability_as_off_rather_than_broken(env):
    settings = _settings(env)
    states = {c.name: c.state for c in collect_checks(settings, check_db=False)}
    assert states["SENTRY_DSN"] != OK
    assert states["HEALTHCHECKS_PING_KEY"] != OK
