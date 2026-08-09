"""Module-code parsing and content hashing. Pure logic — no DB, no network."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from pap.core.dedupe import canonical_json, content_hash
from pap.core.models import Item, parse_module_code

BASE = {
    "source": "studeo",
    "external_id": "act-1",
    "title": "Atividade 1",
    "kind": "activity",
    "url": "https://example.invalid/1",
    "payload": {"summary": "hello"},
}


def _item(**over) -> Item:
    return Item(**{**BASE, **over})


# -- module codes -----------------------------------------------------------
@pytest.mark.parametrize("raw,seq,year", [
    ("54/2025", 54, 2025),
    ("51/2026", 51, 2026),
    (" 52 / 2026 ", 52, 2026),
    ("7/2026", 7, 2026),
])
def test_parse_module_code_splits_seq_and_year(raw, seq, year):
    parsed = parse_module_code(raw)
    assert (parsed.seq, parsed.year) == (seq, year)
    assert parsed.code == raw.strip()


def test_folder_name_replaces_the_slash():
    assert parse_module_code("54/2025").folder_name == "54-2025"
    assert parse_module_code("7/2026").folder_name == "07-2026"


def test_unparseable_code_is_kept_not_rejected():
    """An unexpected format must degrade to 'unsorted but still filed'. Dropping
    the module would lose the activity entirely."""
    parsed = parse_module_code("MODULO-ESPECIAL")
    assert parsed.code == "MODULO-ESPECIAL"
    assert parsed.seq is None and parsed.year is None
    assert "/" not in parsed.folder_name


def test_empty_code_still_yields_a_usable_folder_name():
    assert parse_module_code("").folder_name == "unknown"


def test_folder_name_never_contains_a_path_separator():
    assert "/" not in parse_module_code("54/2025/extra").folder_name


# -- content hashing --------------------------------------------------------
def test_same_content_hashes_the_same():
    assert content_hash(_item()) == content_hash(_item())


def test_payload_key_order_does_not_change_the_hash():
    """Dicts preserve insertion order, so two adapters building the same payload
    differently would otherwise look like a change on every run."""
    a = _item(payload={"a": 1, "b": 2})
    b = _item(payload={"b": 2, "a": 1})
    assert content_hash(a) == content_hash(b)


@pytest.mark.parametrize("field,value", [
    ("title", "Atividade 2"),
    ("url", "https://example.invalid/2"),
    ("kind", "material"),
    ("payload", {"summary": "changed"}),
])
def test_meaningful_changes_change_the_hash(field, value):
    assert content_hash(_item(**{field: value})) != content_hash(_item())


def test_due_date_change_changes_the_hash():
    """A moved prazo must be detected — it is the whole point of the Calendar sync."""
    original = _item(due_at=datetime(2026, 3, 1, tzinfo=timezone.utc))
    moved = _item(due_at=datetime(2026, 3, 8, tzinfo=timezone.utc))
    assert content_hash(original) != content_hash(moved)


def test_hash_ignores_fields_that_carry_no_meaning():
    """discipline_name is presentation, not content: a renamed discipline must not
    make every activity in it look newly changed."""
    assert content_hash(_item(discipline_name="Outro Nome")) == content_hash(_item())


def test_canonical_json_is_stable_and_sorted():
    assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'


def test_canonical_json_keeps_non_ascii_readable():
    assert "ç" in canonical_json({"x": "atenção"})
