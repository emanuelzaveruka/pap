"""Notification keys, summarisation, and secret scrubbing. No DB, no network."""

from __future__ import annotations

from pap.observability import REDACTED, _scrub
from pap.sinks.dispatcher import _summarise, item_dedupe_key


# -- dedupe keys ------------------------------------------------------------
def test_the_same_item_and_channel_yield_the_same_key():
    """This is what makes a re-run silent — the UNIQUE constraint rejects it."""
    assert (item_dedupe_key("studeo", "act-1", "telegram")
            == item_dedupe_key("studeo", "act-1", "telegram"))


def test_channel_is_part_of_the_key():
    """Enabling email later must still deliver items Telegram already covered,
    rather than treating them as already handled."""
    assert (item_dedupe_key("studeo", "act-1", "telegram")
            != item_dedupe_key("studeo", "act-1", "email"))


def test_source_is_part_of_the_key():
    """Two sources are free to use the same external_id — they often will."""
    assert (item_dedupe_key("studeo", "1", "telegram")
            != item_dedupe_key("akita", "1", "telegram"))


def test_key_survives_separator_characters_in_an_id():
    """A colon inside an id must not collide two different items.

    Portal ids frequently contain colons and slashes. Without escaping, the
    collision would be invisible: the UNIQUE constraint would treat the second
    item as already-notified and silently never send it.
    """
    a = item_dedupe_key("studeo", "a:b", "telegram")
    b = item_dedupe_key("studeo", "a", "b:telegram")
    assert a != b


def test_key_escapes_but_stays_readable():
    """The key is inspected by hand in the notification table, so it should not
    degrade into an opaque hash."""
    key = item_dedupe_key("studeo", "atividade/42", "telegram")
    assert key.startswith("item:studeo:")
    assert key.endswith(":telegram")
    assert "/" not in key.split(":")[2]


# -- summarisation ----------------------------------------------------------
def test_summary_is_taken_from_the_payload():
    assert _summarise({"payload": {"summary": "resumo curto"}}) == "resumo curto"


def test_it_falls_back_through_the_known_keys():
    assert _summarise({"payload": {"description": "descricao"}}) == "descricao"
    assert _summarise({"payload": {"excerpt": "trecho"}}) == "trecho"


def test_whitespace_is_collapsed():
    assert _summarise({"payload": {"summary": "a\n\n  b\t c"}}) == "a b c"


def test_long_summaries_are_truncated():
    result = _summarise({"payload": {"summary": "x" * 5000}}, limit=100)
    assert len(result) == 100
    assert result.endswith("…")


def test_a_deadline_is_used_when_there_is_no_prose():
    assert _summarise({"payload": {"due_at": "2026-03-01"}}) == "Prazo: 2026-03-01"


def test_an_unknown_payload_shape_is_not_an_error():
    """Adapters are not required to provide a summary."""
    assert _summarise({"payload": {}}) == ""
    assert _summarise({}) == ""


def test_a_non_string_summary_is_skipped_not_rendered():
    assert _summarise({"payload": {"summary": {"nested": "object"}}}) == ""


# -- scrubbing --------------------------------------------------------------
def _with_secrets(*values):
    import pap.observability as obs

    obs._secret_values = values


def test_a_secret_is_removed_from_a_nested_event(monkeypatch):
    """A password reaches an error report through the connection string in an
    exception message far more often than through any field we could allowlist."""
    monkeypatch.setattr("pap.observability._secret_values", ("hunter2-long-enough",))
    event = {"exception": {"values": [
        {"value": "connection failed: password=hunter2-long-enough host=db"}
    ]}}
    scrubbed = _scrub(event)
    assert "hunter2-long-enough" not in str(scrubbed)
    assert REDACTED in scrubbed["exception"]["values"][0]["value"]


def test_scrubbing_walks_lists_and_dicts(monkeypatch):
    monkeypatch.setattr("pap.observability._secret_values", ("tok-abcdefgh",))
    event = {"breadcrumbs": [{"message": "Bearer tok-abcdefgh"}], "tags": {"x": "tok-abcdefgh"}}
    scrubbed = _scrub(event)
    assert "tok-abcdefgh" not in str(scrubbed)


def test_non_string_values_pass_through_untouched(monkeypatch):
    monkeypatch.setattr("pap.observability._secret_values", ("secret-value",))
    assert _scrub({"n": 5, "ok": True, "none": None}) == {"n": 5, "ok": True, "none": None}


def test_scrubbing_is_a_no_op_without_registered_secrets(monkeypatch):
    monkeypatch.setattr("pap.observability._secret_values", ())
    assert _scrub({"message": "nothing to hide"}) == {"message": "nothing to hide"}
