"""Google credential resolution from environment variables.

No network and no Google libraries required: only the settings logic and the
access-token cache are exercised.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pap.config import GoogleSettings
from pap.core.secrets import SecretStore
from pap.sinks.google_auth import (
    ACCESS_TOKEN_STORE,
    GoogleAuthError,
    _access_token_store,
    _build_flow,
    _cached_access_token,
    _fingerprint,
    client_config,
    naive_utc_now,
)


def _google(**over) -> GoogleSettings:
    base = {
        "enabled": True,
        "client_id": "client-id-123.apps.googleusercontent.com",
        "client_secret": "client-secret-value",
        "refresh_token": "refresh-token-value",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_secrets_file": "",
        "token_file": "",
        "drive_root_folder": "Studeo",
        "drive_backup_folder": "_backups/pap",
        "calendar_id": "primary",
    }
    return GoogleSettings(**{**base, **over})


# -- which source wins ------------------------------------------------------
def test_env_credentials_are_enough_with_no_token_file():
    """The whole point: a headless server needs no file copied to it."""
    settings = _google()
    assert settings.uses_env_credentials
    assert settings.configured


@pytest.mark.parametrize("missing", ["client_id", "client_secret", "refresh_token"])
def test_all_three_env_values_are_required(missing):
    settings = _google(**{missing: ""})
    assert not settings.uses_env_credentials


def test_a_token_file_still_counts_as_configured():
    settings = _google(client_id="", client_secret="", refresh_token="",
                       token_file="/somewhere/google_token.json")
    assert not settings.uses_env_credentials
    assert settings.configured


def test_disabled_beats_having_credentials():
    assert not _google(enabled=False).configured


def test_nothing_configured_is_not_configured():
    assert not _google(client_id="", client_secret="", refresh_token="").configured


# -- access-token cache -----------------------------------------------------
def test_no_state_dir_means_no_cache():
    """Caching is an optimisation; without a state dir the run just refreshes."""
    assert _access_token_store(_google(), None) is None
    assert _cached_access_token(None) == (None, None)


def test_a_fresh_cached_token_is_reused(tmp_path):
    settings = _google()
    store = _access_token_store(settings, str(tmp_path))
    expiry = naive_utc_now() + timedelta(hours=1)
    store.save({"access_token": "AT-1", "expiry": expiry.isoformat()})

    token, cached_expiry = _cached_access_token(store)
    assert token == "AT-1"
    assert cached_expiry is not None


def test_an_expired_cached_token_is_ignored(tmp_path):
    store = _access_token_store(_google(), str(tmp_path))
    store.save({"access_token": "AT-1",
                "expiry": (naive_utc_now() - timedelta(minutes=1)).isoformat()})
    assert _cached_access_token(store) == (None, None)


def test_a_token_about_to_expire_is_ignored(tmp_path):
    """Refreshing early avoids the token dying mid-upload of a large book PDF."""
    store = _access_token_store(_google(), str(tmp_path))
    store.save({"access_token": "AT-1",
                "expiry": (naive_utc_now() + timedelta(minutes=1)).isoformat()})
    assert _cached_access_token(store) == (None, None)


def test_a_timezone_aware_expiry_is_normalised(tmp_path):
    """google-auth compares expiry against naive UTC; handing it an aware datetime
    raises TypeError deep inside the library instead of simply expiring."""
    store = _access_token_store(_google(), str(tmp_path))
    aware = datetime.now(timezone.utc) + timedelta(hours=1)
    store.save({"access_token": "AT-1", "expiry": aware.isoformat()})

    token, expiry = _cached_access_token(store)
    assert token == "AT-1"
    assert expiry is not None and expiry.tzinfo is None


def test_a_corrupt_expiry_is_ignored_not_fatal(tmp_path):
    store = _access_token_store(_google(), str(tmp_path))
    store.save({"access_token": "AT-1", "expiry": "not-a-date"})
    assert _cached_access_token(store) == (None, None)


def test_a_cache_entry_without_a_token_is_ignored(tmp_path):
    store = _access_token_store(_google(), str(tmp_path))
    store.save({"expiry": (naive_utc_now() + timedelta(hours=1)).isoformat()})
    assert _cached_access_token(store) == (None, None)


def test_rotating_the_refresh_token_invalidates_the_cache(tmp_path):
    """Otherwise a rotated credential would keep handing back an access token
    minted for the previous account."""
    original = _google()
    _access_token_store(original, str(tmp_path)).save(
        {"access_token": "AT-1",
         "expiry": (naive_utc_now() + timedelta(hours=1)).isoformat()})

    rotated = _google(refresh_token="a-different-refresh-token")
    assert _fingerprint(rotated) != _fingerprint(original)
    assert _cached_access_token(_access_token_store(rotated, str(tmp_path))) == (None, None)


def test_the_cache_is_a_separate_file_from_any_token_file(tmp_path):
    """The disposable access token must never overwrite a durable credential."""
    store = _access_token_store(_google(), str(tmp_path))
    assert store.path.endswith(f"{ACCESS_TOKEN_STORE}.json")


def test_the_cached_access_token_is_written_0600(tmp_path):
    import os
    import stat

    store = _access_token_store(_google(), str(tmp_path))
    store.save({"access_token": "AT-1",
                "expiry": (naive_utc_now() + timedelta(hours=1)).isoformat()})
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600


# -- consent flow construction ----------------------------------------------
class _FakeFlow:
    """Records which constructor the flow was built through."""

    def __init__(self, source, payload):
        self.source = source
        self.payload = payload

    @classmethod
    def from_client_config(cls, config, scopes):
        return cls("config", config)

    @classmethod
    def from_client_secrets_file(cls, path, scopes):
        return cls("file", path)


def test_the_flow_is_built_from_env_without_any_json_file():
    """The point: having client id + secret in .env means nothing to download."""
    flow = _build_flow(_google(), _FakeFlow)
    assert flow.source == "config"
    installed = flow.payload["installed"]
    assert installed["client_id"] == "client-id-123.apps.googleusercontent.com"
    assert installed["client_secret"] == "client-secret-value"


def test_env_credentials_win_over_a_secrets_file(tmp_path):
    path = tmp_path / "client_secret.json"
    path.write_text("{}")
    flow = _build_flow(_google(client_secrets_file=str(path)), _FakeFlow)
    assert flow.source == "config"


def test_it_falls_back_to_the_secrets_file(tmp_path):
    path = tmp_path / "client_secret.json"
    path.write_text("{}")
    settings = _google(client_id="", client_secret="", client_secrets_file=str(path))
    flow = _build_flow(settings, _FakeFlow)
    assert flow.source == "file"


def test_no_client_at_all_names_both_ways_to_fix_it():
    settings = _google(client_id="", client_secret="", client_secrets_file="")
    with pytest.raises(GoogleAuthError) as exc:
        _build_flow(settings, _FakeFlow)
    message = str(exc.value)
    assert "GOOGLE_CLIENT_ID" in message
    assert "GOOGLE_CLIENT_SECRETS_FILE" in message


def test_a_missing_secrets_file_is_not_silently_accepted():
    settings = _google(client_id="", client_secret="",
                       client_secrets_file="/nonexistent/client_secret.json")
    with pytest.raises(GoogleAuthError):
        _build_flow(settings, _FakeFlow)


def test_client_config_has_the_shape_google_expects():
    config = client_config("cid", "secret", "https://oauth2.googleapis.com/token")
    installed = config["installed"]
    assert set(installed) == {"client_id", "client_secret", "auth_uri", "token_uri",
                              "redirect_uris"}
    assert installed["redirect_uris"] == ["http://localhost"]


def test_client_config_defaults_the_token_uri():
    assert client_config("cid", "secret", "")["installed"]["token_uri"] == (
        "https://oauth2.googleapis.com/token")


# -- doctor -----------------------------------------------------------------
def test_doctor_reports_env_credentials_without_printing_them():
    from pap.ops.doctor import collect_checks, render
    from types import SimpleNamespace

    settings = SimpleNamespace(
        pg_password="pg-password-value", conninfo_safe="host=x dbname=pap user=pap",
        state_dir="/tmp", google=_google(),
        telegram=SimpleNamespace(enabled=False, configured=False),
        email=SimpleNamespace(enabled=False, configured=False),
        observability=SimpleNamespace(sentry_configured=False, healthchecks_configured=False,
                                      release="dev", environment="test",
                                      healthchecks_base_url=""),
        backup=SimpleNamespace(enabled=False, directory="/tmp", keep_daily=14, keep_weekly=8),
    )
    output = render(collect_checks(settings, check_db=False))
    assert "refresh-token-value" not in output
    assert "client-secret-value" not in output
    assert "GOOGLE_REFRESH_TOKEN" in output
