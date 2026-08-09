"""Studeo parsing, validated against a REAL captured payload.

No network. `QUESTIONARIO_367928` is the actual response from
`/objeto-ensino-api-controller/api/questionario/367928`, so these tests fail the
day Studeo changes shape rather than the day a deadline goes missing.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from pap.core.dedupe import content_hash
from pap.sources.studeo import (
    STUDEO_TZ,
    activity_label,
    epoch_ms_to_datetime,
    module_code_from_descricao,
    module_code_from_discipline_id,
    parse_questionario,
)

DISCIPLINA = "2026_26_CURSO15NA-53_EGRAD_DISC100_024"

QUESTIONARIO_367928 = {
    "idQuestionario": 367928,
    "descricao": "ATIVIDADE 1 - ESOFT - FUNDAMENTOS DE REDES DE COMPUTADORES - 53_2026",
    "aleatorias": False,
    "valorQuestionario": 0.50,
    "gabarito": False,
    "dataGabarito": 1788145199000,
    "dataInicial": 1784545259000,
    "dataFinal": 1788145199000,
    "situacao": {"codigo": "A", "descricao": "ABERTO"},
    "especialDataFinal": None,
    "especialPorcentagem": 0,
    "dataAtual": 1786247041949,
    "tempoMax": 0,
    "liberaNota": False,
    "statusLimiteVisualizacao": "NAO_TEM",
    "dataLimiteVisualizacao": None,
    "mostrarNota": True,
    "isMultiTentativas": False,
    "qtdMultiTentativas": 1,
    "finalizado": False,
    "dataFinalizado": None,
    "notaAluno": None,
    "questionarioFinalizado": None,
    "idTipoEvento": 3,
    "feedback": False,
    "dataFeedback": None,
}


# -- timestamps -------------------------------------------------------------
def test_datafinal_is_end_of_day_in_sao_paulo():
    """The single most important assertion in this file. 1788145199000 is
    2026-08-30 23:59:59-03:00 — the end-of-day submission cutoff. Converting with
    the wrong zone shifts every prazo by hours while still looking plausible."""
    due = epoch_ms_to_datetime(1788145199000)
    assert due.strftime("%Y-%m-%d %H:%M:%S") == "2026-08-30 23:59:59"
    assert due.utcoffset().total_seconds() == -3 * 3600


def test_conversion_is_always_timezone_aware():
    """gcalendar.event_body refuses naive datetimes, so this is what keeps the
    two ends compatible."""
    assert epoch_ms_to_datetime(1784545259000).tzinfo is not None


def test_datainicial_matches_the_captured_value():
    opens = epoch_ms_to_datetime(1784545259000)
    assert opens.strftime("%Y-%m-%d %H:%M:%S") == "2026-07-20 08:00:59"


def test_conversion_does_not_depend_on_the_hosts_timezone():
    """Built in UTC then converted; constructing in local time would make the
    result depend on the server's TZ, which differs between WSL and the box."""
    expected = datetime(2026, 8, 31, 2, 59, 59, tzinfo=timezone.utc)
    assert epoch_ms_to_datetime(1788145199000) == expected


@pytest.mark.parametrize("value", [None, "", "abc", {}, []])
def test_missing_or_junk_timestamps_are_none_not_errors(value):
    """Studeo uses null liberally — dataFinalizado stays null until submission."""
    assert epoch_ms_to_datetime(value) is None


def test_string_timestamps_are_accepted():
    assert epoch_ms_to_datetime("1788145199000") == epoch_ms_to_datetime(1788145199000)


# -- module codes -----------------------------------------------------------
def test_module_code_from_the_discipline_id():
    assert module_code_from_discipline_id(DISCIPLINA) == "53/2026"


def test_module_code_from_the_description():
    """An independent second source, used to cross-check the id."""
    assert module_code_from_descricao(QUESTIONARIO_367928["descricao"]) == "53/2026"


def test_both_sources_agree_on_the_real_payload():
    assert (module_code_from_discipline_id(DISCIPLINA)
            == module_code_from_descricao(QUESTIONARIO_367928["descricao"]))


@pytest.mark.parametrize("disciplina,expected", [
    ("2026_26_CURSO14NA-52_EGRAD_DISC200_026", "52/2026"),
    ("2025_26_CURSO14NA-54_EGRAD_DISC200_026", "54/2025"),
    ("2026_26_CURSO14NA-7_EGRAD_DISC200_026", "7/2026"),
])
def test_module_code_across_observed_ids(disciplina, expected):
    assert module_code_from_discipline_id(disciplina) == expected


@pytest.mark.parametrize("bad", ["", "nonsense", "2026-26-ESOFT", None])
def test_unrecognised_ids_return_none_rather_than_guessing(bad):
    assert module_code_from_discipline_id(bad) is None


# -- labels -----------------------------------------------------------------
@pytest.mark.parametrize("descricao,expected", [
    ("ATIVIDADE 1 - ESOFT - FUNDAMENTOS - 53_2026", "AE1"),
    ("ATIVIDADE 3 - ALGO", "AE3"),
    ("MAPA - ESOFT - ENGENHARIA - 53_2026", "MAPA"),
    ("AE2 - alguma coisa", "AE2"),
    ("Prova qualquer", "ATIVIDADE"),
])
def test_activity_label(descricao, expected):
    assert activity_label(descricao) == expected


# -- full mapping -----------------------------------------------------------
def test_parse_maps_the_prazo_to_due_at():
    item = parse_questionario(QUESTIONARIO_367928, disciplina_id=DISCIPLINA)
    assert item.due_at.strftime("%Y-%m-%d %H:%M:%S") == "2026-08-30 23:59:59"


def test_parse_produces_a_scoped_stable_external_id():
    item = parse_questionario(QUESTIONARIO_367928, disciplina_id=DISCIPLINA)
    assert item.external_id == f"{DISCIPLINA}:questionario:367928"
    assert item.source == "studeo"


def test_parse_keeps_the_full_description_as_the_title():
    item = parse_questionario(QUESTIONARIO_367928, disciplina_id=DISCIPLINA)
    assert item.title == QUESTIONARIO_367928["descricao"]


def test_parse_links_back_to_the_studeo_ui():
    item = parse_questionario(QUESTIONARIO_367928, disciplina_id=DISCIPLINA)
    assert item.url.endswith(f"disciplina/{DISCIPLINA}/questionario/367928")


def test_parse_carries_status_and_grade():
    item = parse_questionario(QUESTIONARIO_367928, disciplina_id=DISCIPLINA)
    assert item.payload["situacao"] == "aberto"
    assert item.payload["finalizado"] is False
    assert item.payload["nota_aluno"] is None
    assert item.payload["label"] == "AE1"


def test_parse_sets_the_module_for_drive_foldering():
    item = parse_questionario(QUESTIONARIO_367928, disciplina_id=DISCIPLINA)
    assert item.module_code == "53/2026"
    assert item.discipline_external_id == DISCIPLINA


def test_summary_is_readable_in_a_telegram_message():
    item = parse_questionario(QUESTIONARIO_367928, disciplina_id=DISCIPLINA)
    assert "AE1" in item.payload["summary"]
    assert "30/08/2026" in item.payload["summary"]


def test_an_unknown_situacao_code_passes_through_untranslated():
    """A new Studeo state must be visible, not silently bucketed."""
    payload = {**QUESTIONARIO_367928, "situacao": {"codigo": "X", "descricao": "NOVO"}}
    item = parse_questionario(payload, disciplina_id=DISCIPLINA)
    assert item.payload["situacao"] == "x"


def test_a_questionario_without_a_deadline_is_still_collected():
    """No prazo is ordinary — it just cannot be put in the calendar."""
    payload = {**QUESTIONARIO_367928, "dataFinal": None}
    item = parse_questionario(payload, disciplina_id=DISCIPLINA)
    assert item.due_at is None
    assert item.external_id.endswith(":367928")


# -- change detection -------------------------------------------------------
def test_the_hash_is_stable_across_identical_fetches():
    """dataAtual is the server clock and changes on EVERY request. If it reached
    the hash, every run would look like a change and re-notify."""
    first = parse_questionario({**QUESTIONARIO_367928, "dataAtual": 1786247041949},
                               disciplina_id=DISCIPLINA)
    second = parse_questionario({**QUESTIONARIO_367928, "dataAtual": 1786299999999},
                                disciplina_id=DISCIPLINA)
    assert content_hash(first) == content_hash(second)


def test_a_moved_prazo_changes_the_hash():
    """This is what drives the Calendar patch."""
    moved = parse_questionario({**QUESTIONARIO_367928, "dataFinal": 1788231599000},
                               disciplina_id=DISCIPLINA)
    original = parse_questionario(QUESTIONARIO_367928, disciplina_id=DISCIPLINA)
    assert content_hash(moved) != content_hash(original)


def test_submitting_an_activity_changes_the_hash():
    submitted = parse_questionario({**QUESTIONARIO_367928, "finalizado": True,
                                    "notaAluno": 0.5}, disciplina_id=DISCIPLINA)
    original = parse_questionario(QUESTIONARIO_367928, disciplina_id=DISCIPLINA)
    assert content_hash(submitted) != content_hash(original)
