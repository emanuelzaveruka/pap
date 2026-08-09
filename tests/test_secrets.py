"""Credential store: permissions, atomicity, and degrading rather than failing.

Exercised on tmp_path — no network, no real credentials.
"""

from __future__ import annotations

import json
import os
import stat
import time

import pytest

from pap.core.secrets import SecretStore, account_fingerprint, human_duration

FP = "fingerprint-a"


def _store(tmp_path, *, name="google_token", **kwargs) -> SecretStore:
    kwargs.setdefault("fingerprint", FP)
    return SecretStore(str(tmp_path / "state"), name, **kwargs)


def test_save_then_load_round_trips(tmp_path):
    store = _store(tmp_path)
    store.save({"refresh_token": "RT-1"})
    loaded = store.load()
    assert loaded is not None
    assert loaded.data["refresh_token"] == "RT-1"
    assert loaded.age_seconds() < 5


def test_file_is_0600_and_directory_is_0700(tmp_path):
    """This file IS a live credential. 0644 would expose the Google refresh token
    to every account on the box."""
    store = _store(tmp_path)
    store.save({"refresh_token": "RT-1"})
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(store.state_dir).st_mode) == 0o700


def test_missing_file_is_not_an_error(tmp_path):
    assert _store(tmp_path).load() is None


def test_corrupt_file_degrades_to_reauthenticate(tmp_path):
    """A half-written or truncated file must never crash a run — the correct
    behaviour is to authenticate again."""
    store = _store(tmp_path)
    store.save({"refresh_token": "RT-1"})
    with open(store.path, "w", encoding="utf-8") as fh:
        fh.write("{not json")
    assert store.load() is None


def test_credential_for_another_account_is_refused(tmp_path):
    _store(tmp_path, fingerprint="account-a").save({"refresh_token": "RT-A"})
    assert _store(tmp_path, fingerprint="account-b").load() is None


def test_expired_credential_is_ignored(tmp_path):
    store = _store(tmp_path, max_age_seconds=60)
    store.save({"refresh_token": "RT-1"}, now=time.time() - 3600)
    assert store.load() is None


def test_disabled_store_neither_reads_nor_writes(tmp_path):
    store = _store(tmp_path, enabled=False)
    store.save({"refresh_token": "RT-1"})
    assert not os.path.exists(store.path)
    assert store.load() is None


def test_invalidate_is_idempotent(tmp_path):
    store = _store(tmp_path)
    store.save({"refresh_token": "RT-1"})
    store.invalidate()
    assert store.load() is None
    store.invalidate()  # already gone — must not raise


def test_write_leaves_no_temporary_files_behind(tmp_path):
    store = _store(tmp_path)
    store.save({"refresh_token": "RT-1"})
    leftovers = [f for f in os.listdir(store.state_dir) if f.startswith(".tmp-")]
    assert leftovers == []


def test_stored_payload_shape_is_stable(tmp_path):
    store = _store(tmp_path)
    store.save({"refresh_token": "RT-1"})
    with open(store.path, encoding="utf-8") as fh:
        raw = json.load(fh)
    assert set(raw) == {"data", "issued_at", "fingerprint"}


# -- cooldown ---------------------------------------------------------------
def test_cooldown_is_active_then_expires(tmp_path):
    store = _store(tmp_path)
    assert store.cooldown_until() is None
    until = store.set_cooldown(900, reason="429")
    assert store.cooldown_until() == pytest.approx(until, abs=1)
    store.set_cooldown(0, reason="expired", now=time.time() - 10)
    assert store.cooldown_until() is None


def test_clear_cooldown(tmp_path):
    store = _store(tmp_path)
    store.set_cooldown(900, reason="429")
    store.clear_cooldown()
    assert store.cooldown_until() is None


def test_cooldown_is_separate_per_named_store(tmp_path):
    """Studeo being rate limited must not stop the platform calling Google."""
    studeo = _store(tmp_path, name="studeo_session")
    google = _store(tmp_path, name="google_token")
    studeo.set_cooldown(900, reason="429")
    assert studeo.cooldown_until() is not None
    assert google.cooldown_until() is None


# -- reporting --------------------------------------------------------------
def test_describe_never_leaks_the_secret(tmp_path):
    store = _store(tmp_path)
    store.save({"refresh_token": "SUPER-SECRET-VALUE"})
    store.set_cooldown(300, reason="429")
    text = store.describe()
    assert "SUPER-SECRET-VALUE" not in text
    assert "cooldown  : ACTIVE" in text
    assert store.path in text


def test_account_fingerprint_separates_accounts():
    a = account_fingerprint("google", "one@example.com")
    b = account_fingerprint("google", "two@example.com")
    assert a != b
    assert a == account_fingerprint("google", "one@example.com")


@pytest.mark.parametrize("seconds,expected", [(5, "5s"), (90, "1m30s"), (7200, "2h00m")])
def test_human_duration(seconds, expected):
    assert human_duration(seconds) == expected
