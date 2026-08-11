"""Studeo parsing, validated against a REAL captured payload.

No network. `QUESTIONARIO_367928` is the actual response from
`/objeto-ensino-api-controller/api/questionario/367928`, so these tests fail the
day Studeo changes shape rather than the day a deadline goes missing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pap.core.dedupe import content_hash
from pap.sources.studeo import (
    disciplines_from_plano,
    parse_plano_estudo_event,
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


# -- plano de estudo (the agenda feed) --------------------------------------
# Verbatim from /objeto-ensino-api-controller/api/plano-estudo/disciplinas-usuario.
# Note the repeated AULA entries — the real feed returns duplicates.
PLANO_ESTUDO = [
    {"dhInicial": 1786757400000, "dhFinal": 1786762740000,
     "dsPlanoDeEstudoTipoEvento": "Nota",
     "dsPlanoDeEstudoSubTipoEvento": "LANÇAMENTO DE NOTA DAS DISCIPLINAS ECT",
     "dsPlanoDeEstudoTipoAlerta": "Nota", "tpCor": "info",
     "nmDisciplina": "ESTUDO CONTEMPORÂNEO E TRANSVERSAL: COMUNICAÇÃO ASSERTIVA E INTERPESSOAL",
     "cdShortname": "2026_26_CURSO14NA-52_EGRAD_DTR020_008"},
    {"dhInicial": 1786757400000, "dhFinal": 1786762740000,
     "dsPlanoDeEstudoTipoEvento": "Nota", "dsPlanoDeEstudoSubTipoEvento": "PUBLICAÇÃO DE NOTA",
     "dsPlanoDeEstudoTipoAlerta": "Nota", "tpCor": "info",
     "nmDisciplina": "TÓPICOS EM COMPUTAÇÃO II",
     "cdShortname": "2026_26_CURSO14NA-52_EGRAD_DISC100_023"},
    {"dhInicial": 1786757400000, "dhFinal": 1786762740000,
     "dsPlanoDeEstudoTipoEvento": "Nota", "dsPlanoDeEstudoSubTipoEvento": "PUBLICAÇÃO DE NOTA",
     "dsPlanoDeEstudoTipoAlerta": "Nota", "tpCor": "info",
     "nmDisciplina": "EMPREENDEDORISMO",
     "cdShortname": "2026_26_CURSO14NA-52_EGRAD_DISC200_026"},
    {"dhInicial": 1785985200000, "dhFinal": 1786071540000,
     "dsPlanoDeEstudoTipoEvento": "Aula", "dsPlanoDeEstudoSubTipoEvento": "AULA",
     "dsPlanoDeEstudoTipoAlerta": "Ao Vivo", "tpCor": "success",
     "nmDisciplina": "FUNDAMENTOS DE REDES DE COMPUTADORES",
     "cdShortname": "2026_26_CURSO15NA-53_EGRAD_DISC100_024"},
    {"dhInicial": 1786590000000, "dhFinal": 1786676340000,
     "dsPlanoDeEstudoTipoEvento": "Aula", "dsPlanoDeEstudoSubTipoEvento": "AULA",
     "dsPlanoDeEstudoTipoAlerta": "Ao Vivo", "tpCor": "success",
     "nmDisciplina": "FUNDAMENTOS DE REDES DE COMPUTADORES",
     "cdShortname": "2026_26_CURSO15NA-53_EGRAD_DISC100_024"},
    {"dhInicial": 1785985200000, "dhFinal": 1786071540000,
     "dsPlanoDeEstudoTipoEvento": "Aula", "dsPlanoDeEstudoSubTipoEvento": "AULA",
     "dsPlanoDeEstudoTipoAlerta": "Ao Vivo", "tpCor": "success",
     "nmDisciplina": "FUNDAMENTOS DE REDES DE COMPUTADORES",
     "cdShortname": "2026_26_CURSO15NA-53_EGRAD_DISC100_024"},
    {"dhInicial": 1786590000000, "dhFinal": 1786676340000,
     "dsPlanoDeEstudoTipoEvento": "Aula", "dsPlanoDeEstudoSubTipoEvento": "AULA",
     "dsPlanoDeEstudoTipoAlerta": "Ao Vivo", "tpCor": "success",
     "nmDisciplina": "FUNDAMENTOS DE REDES DE COMPUTADORES",
     "cdShortname": "2026_26_CURSO15NA-53_EGRAD_DISC100_024"},
]


def test_disciplines_are_discovered_from_the_agenda():
    """This is what removes the need for a configured discipline list."""
    found = disciplines_from_plano(PLANO_ESTUDO)
    assert set(found) == {
        "2026_26_CURSO14NA-52_EGRAD_DTR020_008",
        "2026_26_CURSO14NA-52_EGRAD_DISC100_023",
        "2026_26_CURSO14NA-52_EGRAD_DISC200_026",
        "2026_26_CURSO15NA-53_EGRAD_DISC100_024",
    }
    assert found["2026_26_CURSO14NA-52_EGRAD_DISC200_026"] == "EMPREENDEDORISMO"


def test_discovered_disciplines_span_both_modules():
    codes = {module_code_from_discipline_id(d) for d in disciplines_from_plano(PLANO_ESTUDO)}
    assert codes == {"52/2026", "53/2026"}


def test_repeated_agenda_entries_collapse_to_one_id():
    """The real feed returned the same live class four times. A derived
    external_id makes UNIQUE(source, external_id) absorb that, so no dedupe pass
    is needed and no duplicate notification is sent."""
    ids = [parse_plano_estudo_event(e).external_id for e in PLANO_ESTUDO]
    assert len(ids) == 7
    assert len(set(ids)) == 5      # 7 entries, 2 exact repeats


def test_different_events_do_not_collide():
    a, b = parse_plano_estudo_event(PLANO_ESTUDO[0]), parse_plano_estudo_event(PLANO_ESTUDO[1])
    assert a.external_id != b.external_id


def test_event_due_at_is_the_window_close():
    item = parse_plano_estudo_event(PLANO_ESTUDO[3])
    assert item.due_at.strftime("%Y-%m-%d %H:%M") == "2026-08-06 23:59"


def test_event_carries_discipline_module_and_type():
    item = parse_plano_estudo_event(PLANO_ESTUDO[3])
    assert item.kind == "evento"
    assert item.module_code == "53/2026"
    assert item.discipline_external_id == "2026_26_CURSO15NA-53_EGRAD_DISC100_024"
    assert item.discipline_name == "FUNDAMENTOS DE REDES DE COMPUTADORES"
    assert item.payload["tipo"] == "Aula"
    assert item.payload["alerta"] == "Ao Vivo"


def test_event_title_reads_naturally():
    assert parse_plano_estudo_event(PLANO_ESTUDO[1]).title == (
        "PUBLICAÇÃO DE NOTA — TÓPICOS EM COMPUTAÇÃO II")


def test_same_day_event_summary_shows_a_time_range():
    assert "20:00" not in parse_plano_estudo_event(PLANO_ESTUDO[3]).payload["summary"]
    assert "06/08/2026" in parse_plano_estudo_event(PLANO_ESTUDO[3]).payload["summary"]


def test_an_undated_entry_is_skipped_rather_than_stored():
    assert parse_plano_estudo_event(
        {"cdShortname": "x", "dsPlanoDeEstudoTipoEvento": "Nota"}) is None


def test_agenda_entries_are_stable_across_fetches():
    """Nothing volatile may reach the hash, or every run re-notifies."""
    first = [content_hash(parse_plano_estudo_event(e)) for e in PLANO_ESTUDO]
    second = [content_hash(parse_plano_estudo_event(dict(e))) for e in PLANO_ESTUDO]
    assert first == second


def test_disciplines_from_an_empty_or_malformed_feed():
    assert disciplines_from_plano([]) == {}
    assert disciplines_from_plano([None, "junk", {}]) == {}


# -- auth header ------------------------------------------------------------
def _source(monkeypatch, **env):
    from types import SimpleNamespace
    from pap.sources.studeo import StudeoSource
    for k, v in {"STUDEO_TOKEN": "", "STUDEO_BASE_URL": "", "STUDEO_USERNAME": "",
                 "STUDEO_PASSWORD": "", **env}.items():
        monkeypatch.setenv(k, v)
    return StudeoSource(SimpleNamespace(http=None))


def test_the_token_is_sent_raw_without_a_bearer_prefix(monkeypatch):
    """Verified against the live API: `Bearer <jwt>` is rejected 401 with
    "TOKEN_IS_NULL_OR_EMPTY" while the bare token returns 200. The error claims the
    token is MISSING rather than malformed, so a Bearer-prefixed request is
    indistinguishable from sending nothing — which is why Postman's Bearer Token
    auth type cannot be used here."""
    headers = _source(monkeypatch, STUDEO_TOKEN="jwt-value")._headers()
    assert headers["Authorization"] == "jwt-value"
    assert "Bearer" not in headers["Authorization"]


def test_the_spa_host_is_corrected_to_the_api_host(monkeypatch):
    """studeo.unicesumar.edu.br serves the Angular app and 404s every API path, so
    it is never a valid value — obeying it would fail for a reason the 404 does
    not mention."""
    src = _source(monkeypatch, STUDEO_TOKEN="t",
                  STUDEO_BASE_URL="https://studeo.unicesumar.edu.br")
    assert src.base_url == "https://studeoapi.unicesumar.edu.br"


def test_an_explicit_api_host_is_respected(monkeypatch):
    src = _source(monkeypatch, STUDEO_TOKEN="t",
                  STUDEO_BASE_URL="https://studeoapi.unicesumar.edu.br/")
    assert src.base_url == "https://studeoapi.unicesumar.edu.br"


def test_the_default_host_is_the_api_host(monkeypatch):
    assert _source(monkeypatch, STUDEO_TOKEN="t").base_url == "https://studeoapi.unicesumar.edu.br"


def test_a_token_alone_is_enough_to_be_enabled(monkeypatch):
    assert _source(monkeypatch, STUDEO_TOKEN="t").enabled


def test_username_and_password_are_enough_to_run_unattended(monkeypatch):
    """The whole point of the login support: no pasted token, no 4-hour babysitting."""
    src = _source(monkeypatch, STUDEO_USERNAME="ra", STUDEO_PASSWORD="pw")
    assert src.enabled
    assert src.disabled_reason is None


def test_with_no_credentials_at_all_it_asks_for_the_login_pair(monkeypatch):
    src = _source(monkeypatch)
    assert not src.enabled
    assert "STUDEO_USERNAME" in src.disabled_reason
    assert "STUDEO_PASSWORD" in src.disabled_reason


# -- token lifetime ---------------------------------------------------------
def _fake_jwt(exp: int) -> str:
    import base64, json
    body = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJSUzI1NiJ9.{body}.sig"


def test_jwt_expiry_is_read_from_the_token_itself():
    """Taking the lifetime from the token beats assuming the observed 4 hours,
    which is a server-side policy that can change without notice."""
    from pap.sources.studeo import jwt_expiry

    expiry = jwt_expiry(_fake_jwt(1786394857))
    assert expiry == datetime(2026, 8, 10, 20, 47, 37, tzinfo=timezone.utc)


def test_the_real_captured_token_had_a_four_hour_lifetime():
    from pap.sources.studeo import jwt_expiry

    issued, expires = 1786380457, 1786394857
    assert (jwt_expiry(_fake_jwt(expires))
            - datetime.fromtimestamp(issued, tz=timezone.utc)) == timedelta(hours=4)


@pytest.mark.parametrize("bad", ["", "not-a-jwt", "a.b", "eyJ.@@@.sig"])
def test_an_unparseable_token_reports_no_expiry_rather_than_raising(bad):
    """Callers treat None as 'log in again', which is the safe reading."""
    from pap.sources.studeo import jwt_expiry

    assert jwt_expiry(bad) is None


# -- list-next: the per-discipline academic calendar ------------------------
# Verbatim from /objeto-ensino-api-controller/api/plano-estudo/list-next/{d}/50/0.
# NOTE cdShortname and nmDisciplina are null — the discipline is implied by the URL.
LIST_NEXT_MAPA = {
    "dhInicial": 1784545200000, "dhFinal": 1789959540000,
    "dsPlanoDeEstudoTipoEvento": "MAPA",
    "dsPlanoDeEstudoSubTipoEvento": "Material de Avaliação Prática da Aprendizagem",
    "dsPlanoDeEstudoTipoAlerta": "MAPA", "tpCor": "success",
    "nmDisciplina": None, "cdShortname": None,
}
LIST_NEXT_AE1 = {
    "dhInicial": 1784545200000, "dhFinal": 1788145199000,
    "dsPlanoDeEstudoTipoEvento": "Atividade",
    "dsPlanoDeEstudoSubTipoEvento": "REALIZAÇÃO DE ATIVIDADE 1",
    "dsPlanoDeEstudoTipoAlerta": "Atividade", "tpCor": "success",
    "nmDisciplina": None, "cdShortname": None,
}
LIST_NEXT_NOTA = {
    "dhInicial": 1786757400000, "dhFinal": 1786762740000,
    "dsPlanoDeEstudoTipoEvento": "Nota",
    "dsPlanoDeEstudoSubTipoEvento": "PUBLICAÇÃO DE NOTA",
    "dsPlanoDeEstudoTipoAlerta": "Nota", "tpCor": "info",
    "nmDisciplina": None, "cdShortname": None,
}


def test_the_discipline_is_injected_when_the_feed_omits_it():
    """list-next nulls both fields. Without injection every discipline's events
    would share an external_id and collapse into one."""
    item = parse_plano_estudo_event(LIST_NEXT_MAPA, disciplina_id=DISCIPLINA,
                                    disciplina_name="FUNDAMENTOS DE REDES")
    assert item.discipline_external_id == DISCIPLINA
    assert item.discipline_name == "FUNDAMENTOS DE REDES"
    assert item.module_code == "53/2026"
    assert item.external_id.startswith(f"{DISCIPLINA}:evento:")


def test_the_same_event_in_two_disciplines_does_not_collide():
    a = parse_plano_estudo_event(LIST_NEXT_MAPA, disciplina_id="2026_26_X-52_EGRAD_A_001")
    b = parse_plano_estudo_event(LIST_NEXT_MAPA, disciplina_id="2026_26_X-52_EGRAD_B_002")
    assert a.external_id != b.external_id


@pytest.mark.parametrize("entry,kind", [
    (LIST_NEXT_MAPA, "activity"),
    (LIST_NEXT_AE1, "activity"),
    (LIST_NEXT_NOTA, "evento"),
])
def test_graded_work_is_distinguished_from_information(entry, kind):
    """Missing a MAPA costs marks; missing a grade publication costs nothing."""
    assert parse_plano_estudo_event(entry, disciplina_id=DISCIPLINA).kind == kind


def test_the_mapa_deadline_is_read_correctly():
    item = parse_plano_estudo_event(LIST_NEXT_MAPA, disciplina_id=DISCIPLINA)
    assert item.due_at.strftime("%Y-%m-%d %H:%M") == "2026-09-20 23:59"


def test_an_activity_deadline_matches_the_questionario_it_belongs_to():
    """AE1's dhFinal in list-next equals dataFinal on questionario 367928 —
    the two endpoints agree, which is why either can drive the calendar."""
    from_plano = parse_plano_estudo_event(LIST_NEXT_AE1, disciplina_id=DISCIPLINA).due_at
    from_quest = parse_questionario(QUESTIONARIO_367928, disciplina_id=DISCIPLINA).due_at
    assert from_plano == from_quest


def test_an_event_appearing_in_both_feeds_collapses_to_one_item():
    """The dashboard agenda and the per-discipline plan overlap. Identical fields
    must yield one row, not two notifications for the same deadline."""
    from_agenda = parse_plano_estudo_event(
        {**LIST_NEXT_NOTA, "cdShortname": DISCIPLINA, "nmDisciplina": "X"})
    from_plano = parse_plano_estudo_event(LIST_NEXT_NOTA, disciplina_id=DISCIPLINA,
                                          disciplina_name="X")
    assert from_agenda.external_id == from_plano.external_id
