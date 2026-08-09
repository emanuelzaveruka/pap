"""Studeo (Unicesumar) source adapter.

API base: ``https://studeoapi.unicesumar.edu.br`` — note this is a *different*
host from the SPA at ``studeo.unicesumar.edu.br``, which returns 404 for these
paths.

**Read-only.** This adapter never submits, never posts progress, and deliberately
never touches ``/log-acesso-api-controller/api/evento/`` — that endpoint records
student activity telemetry, and a scraper writing to it would fabricate
engagement data about you.

### Time handling, which is the part most likely to go wrong

Every date is an **epoch millisecond timestamp in UTC**. Studeo's own
``/auth-api-controller/auth/token/time-info`` reports
``{"timezoneId": "America/Sao_Paulo", "offset": -180}``, and that is the zone the
academic calendar is expressed in: a `dataFinal` of ``1788145199000`` is
``2026-08-30 23:59:59-03:00`` — end of day, the classic submission cutoff.

Converting with the wrong zone shifts every prazo by hours while still *looking*
plausible, so conversion happens in exactly one place (``epoch_ms_to_datetime``)
and always yields a timezone-aware value. ``sinks/gcalendar.py`` refuses naive
datetimes for the same reason.

### Identifiers

A discipline id encodes the module::

    2026_26_CURSO15NA-53_EGRAD_DISC100_024
     |             |
     year          module sequence   ->  module code "53/2026"

Confirmed twice over: the activity's own ``descricao`` ends in ``53_2026``. The id
is used verbatim as the Studeo-side key; the derived ``NN/YYYY`` is what Drive
folders and ``pap.module`` use.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from ..core.models import Item
from ..core.registry import register
from .base import BaseSource

log = logging.getLogger(__name__)

BASE_URL = "https://studeoapi.unicesumar.edu.br"

# Studeo reports this itself at /auth-api-controller/auth/token/time-info; kept as
# a constant so parsing never depends on a network call, and verified against that
# endpoint by `pap run studeo` when it can.
STUDEO_TZ = ZoneInfo("America/Sao_Paulo")

EP_TIME_INFO = "/auth-api-controller/auth/token/time-info"
# The student's whole agenda in one call: live classes, grade publications, and
# every dated event, each tagged with its discipline. This is also how enrolled
# disciplines are discovered — no separate call and no configured list.
EP_PLANO_ESTUDO = "/objeto-ensino-api-controller/api/plano-estudo/disciplinas-usuario"
EP_PLANO_ESTUDO_NEXT = "/objeto-ensino-api-controller/api/plano-estudo/list-next/{disciplina}/{limit}/{offset}"
EP_QUESTIONARIO = "/objeto-ensino-api-controller/api/questionario/{id}"

# situacao.codigo -> our own vocabulary. Unknown codes pass through rather than
# being forced into a bucket, so a new Studeo state is visible instead of silently
# mislabelled.
SITUACAO = {"A": "aberto", "F": "fechado", "P": "pendente"}

_DISCIPLINA_ID_RE = re.compile(r"^(?P<year>\d{4})_\d+_[A-Z0-9]+-(?P<seq>\d{1,3})_", re.I)
_DESCRICAO_MODULE_RE = re.compile(r"(?P<seq>\d{1,3})_(?P<year>\d{4})\s*$")


def epoch_ms_to_datetime(value: Any, *, tz: ZoneInfo = STUDEO_TZ) -> datetime | None:
    """Convert Studeo's epoch-millisecond timestamps to an aware datetime.

    Returns None for null/empty, which Studeo uses liberally (``dataFinalizado``
    is null until submission, ``especialDataFinal`` usually stays null). Callers
    must treat "no date" as ordinary rather than exceptional.
    """
    if value is None or value == "":
        return None
    try:
        ms = int(value)
    except (TypeError, ValueError):
        log.warning("unparseable Studeo timestamp %r — ignoring it", value)
        return None
    # fromtimestamp with an explicit UTC tz, then convert: constructing in local
    # time would make the result depend on the server's TZ setting.
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(tz)


def module_code_from_discipline_id(disciplina_id: str) -> str | None:
    """``2026_26_CURSO15NA-53_EGRAD_...`` -> ``"53/2026"``, or None if unrecognised."""
    match = _DISCIPLINA_ID_RE.match(disciplina_id or "")
    if not match:
        return None
    return f"{int(match.group('seq'))}/{match.group('year')}"


def module_code_from_descricao(descricao: str) -> str | None:
    """``"ATIVIDADE 1 - ... - 53_2026"`` -> ``"53/2026"``.

    A second, independent source for the module. Used to cross-check the id-derived
    value; a disagreement means one of the two patterns has drifted and is worth
    logging rather than silently trusting either.
    """
    match = _DESCRICAO_MODULE_RE.search((descricao or "").strip())
    if not match:
        return None
    return f"{int(match.group('seq'))}/{match.group('year')}"


def activity_label(descricao: str) -> str:
    """Short label (``AE1``, ``MAPA``…) pulled from the long description.

    Studeo names activities like ``"ATIVIDADE 1 - ESOFT - FUNDAMENTOS ... - 53_2026"``.
    The full string is kept as the title; this is only for grouping and for the
    Telegram summary, where the full name is unreadable.
    """
    text = (descricao or "").upper()
    if "MAPA" in text:
        return "MAPA"
    if match := re.search(r"ATIVIDADE\s*(\d+)", text):
        return f"AE{match.group(1)}"
    if match := re.search(r"\bAE\s*(\d+)", text):
        return f"AE{match.group(1)}"
    return "ATIVIDADE"


def parse_questionario(payload: dict, *, disciplina_id: str) -> Item:
    """Map ``/api/questionario/{id}`` onto an ``Item``.

    ``dataFinal`` is the prazo — that is the field the Calendar sync depends on.
    ``external_id`` is scoped by discipline because a questionnaire id is only
    guaranteed unique within Studeo, and scoping costs nothing.
    """
    quest_id = payload.get("idQuestionario")
    descricao = (payload.get("descricao") or "").strip()
    situacao = payload.get("situacao") or {}
    codigo = (situacao.get("codigo") or "").upper()

    due_at = epoch_ms_to_datetime(payload.get("dataFinal"))
    opens_at = epoch_ms_to_datetime(payload.get("dataInicial"))

    from_id = module_code_from_discipline_id(disciplina_id)
    from_descricao = module_code_from_descricao(descricao)
    if from_id and from_descricao and from_id != from_descricao:
        log.warning("module code disagrees for %s: id says %s, descricao says %s — using the id",
                    quest_id, from_id, from_descricao)

    return Item(
        source="studeo",
        external_id=f"{disciplina_id}:questionario:{quest_id}",
        title=descricao or f"Questionário {quest_id}",
        kind="activity",
        url=(f"https://studeo.unicesumar.edu.br/#!/app/studeo/aluno/ambiente/"
             f"disciplina/{disciplina_id}/questionario/{quest_id}"),
        due_at=due_at,
        module_code=from_id or from_descricao,
        discipline_external_id=disciplina_id,
        payload={
            "label": activity_label(descricao),
            "questionario_id": quest_id,
            "situacao": SITUACAO.get(codigo, codigo.lower() or "desconhecido"),
            "situacao_descricao": situacao.get("descricao"),
            "finalizado": bool(payload.get("finalizado")),
            "nota_aluno": payload.get("notaAluno"),
            "valor": payload.get("valorQuestionario"),
            "opens_at": opens_at.isoformat() if opens_at else None,
            "due_at": due_at.isoformat() if due_at else None,
            "summary": _summary(descricao, due_at, payload),
        },
    )


def parse_plano_estudo_event(entry: dict) -> Item | None:
    """Map one ``plano-estudo/disciplinas-usuario`` entry onto an ``Item``.

    These are the student's agenda: live classes (``Aula``), grade publications
    (``Nota``), and other dated events, each carrying its discipline in
    ``cdShortname``. ``dhFinal`` is when the window closes, so it is the date worth
    putting in a calendar.

    The feed has **no id of its own** and repeats entries — the same live class
    appeared four times in one real response. ``external_id`` is therefore derived
    from the fields that identify the event, which makes the repeats collapse into
    one row via ``UNIQUE (source, external_id)`` instead of needing a dedupe pass.
    """
    shortname = (entry.get("cdShortname") or "").strip()
    tipo = (entry.get("dsPlanoDeEstudoTipoEvento") or "").strip()
    subtipo = (entry.get("dsPlanoDeEstudoSubTipoEvento") or "").strip()
    disciplina = (entry.get("nmDisciplina") or "").strip()

    starts_at = epoch_ms_to_datetime(entry.get("dhInicial"))
    ends_at = epoch_ms_to_datetime(entry.get("dhFinal"))
    if ends_at is None and starts_at is None:
        # Undated agenda entries carry no information this platform can act on.
        return None

    fingerprint = hashlib.sha256("|".join([
        shortname, tipo, subtipo,
        str(entry.get("dhInicial")), str(entry.get("dhFinal")),
    ]).encode("utf-8")).hexdigest()[:12]

    title = " — ".join(p for p in (subtipo or tipo, disciplina) if p)

    return Item(
        source="studeo",
        external_id=f"{shortname}:evento:{fingerprint}",
        title=title or "Evento do plano de estudo",
        kind="evento",
        url=(f"https://studeo.unicesumar.edu.br/#!/app/studeo/aluno/ambiente/"
             f"disciplina/{shortname}") if shortname else None,
        due_at=ends_at or starts_at,
        module_code=module_code_from_discipline_id(shortname),
        discipline_external_id=shortname or None,
        discipline_name=disciplina or None,
        payload={
            "tipo": tipo,
            "subtipo": subtipo,
            "alerta": entry.get("dsPlanoDeEstudoTipoAlerta"),
            "disciplina": disciplina,
            "starts_at": starts_at.isoformat() if starts_at else None,
            "ends_at": ends_at.isoformat() if ends_at else None,
            "summary": _event_summary(tipo, subtipo, disciplina, starts_at, ends_at),
        },
    )


def _event_summary(tipo: str, subtipo: str, disciplina: str,
                   starts_at: datetime | None, ends_at: datetime | None) -> str:
    parts = [p for p in (tipo, disciplina) if p]
    if starts_at and ends_at and starts_at.date() == ends_at.date():
        parts.append(f"{starts_at:%d/%m/%Y %H:%M}–{ends_at:%H:%M}")
    elif ends_at:
        parts.append(f"até {ends_at:%d/%m/%Y %H:%M}")
    return " — ".join(parts)


def disciplines_from_plano(entries: list[dict]) -> dict[str, str]:
    """Enrolled disciplines discovered from the agenda: ``{id: name}``.

    This is why no discipline list has to be configured — every agenda entry names
    the discipline it belongs to, so the enrolment falls out of a call the adapter
    already makes. Ordering is preserved so runs are reproducible.
    """
    found: dict[str, str] = {}
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        shortname = (entry.get("cdShortname") or "").strip()
        if shortname and shortname not in found:
            found[shortname] = (entry.get("nmDisciplina") or "").strip() or shortname
    return found


def _summary(descricao: str, due_at: datetime | None, payload: dict) -> str:
    parts = [activity_label(descricao)]
    if due_at:
        parts.append(f"prazo {due_at:%d/%m/%Y %H:%M}")
    if payload.get("finalizado"):
        parts.append("entregue")
    elif (payload.get("situacao") or {}).get("descricao"):
        parts.append(str(payload["situacao"]["descricao"]).lower())
    return " — ".join(parts)


@register
class StudeoSource(BaseSource):
    """Collects activities from Studeo.

    Authentication is not yet implemented: the login request has not been
    captured, so for now the adapter accepts a JWT copied from a browser session
    via ``STUDEO_TOKEN``. That is enough to prove the whole pipeline against real
    data; it is not enough to run unattended, because the token expires. Capturing
    ``POST /auth-api-controller/auth/token`` is the remaining piece.
    """

    name = "studeo"

    def __init__(self, settings, *, token: str | None = None) -> None:
        super().__init__(settings)
        import os

        self.base_url = os.environ.get("STUDEO_BASE_URL", BASE_URL).rstrip("/")
        self.token = token or os.environ.get("STUDEO_TOKEN", "").strip()
        self.username = os.environ.get("STUDEO_USERNAME", "").strip()
        self.password = os.environ.get("STUDEO_PASSWORD", "").strip()
        self.disciplines: dict[str, str] = {}

    @property
    def disabled_reason(self) -> str | None:
        if self.token:
            return None
        if self.username and self.password:
            return (
                "STUDEO_USERNAME/STUDEO_PASSWORD are set, but automatic login is not "
                "implemented yet: Studeo authenticates through sso.unicesumar.edu.br, a "
                "separate identity provider, and that request has not been captured. "
                "Capture the login POST (DevTools -> Network -> Preserve log -> sign in) "
                "and it can be automated. Until then set STUDEO_TOKEN."
            )
        return (
            "set STUDEO_TOKEN in .env — copy a JWT from an authenticated browser "
            "session: DevTools -> Network -> any studeoapi request -> Request Headers "
            "-> Authorization (paste the value WITHOUT the leading 'Bearer ')"
        )

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}

    def check_server_timezone(self) -> None:
        """Confirm Studeo still reports the timezone this module assumes.

        Cheap, unauthenticated, and guards the one failure that would corrupt every
        deadline while looking entirely normal.
        """
        try:
            info = self.session.get_json(f"{self.base_url}{EP_TIME_INFO}")
        except Exception as exc:  # noqa: BLE001 - advisory only
            log.debug("could not read Studeo's time-info: %s", exc)
            return
        reported = info.get("timezoneId")
        if reported and reported != str(STUDEO_TZ):
            log.warning("Studeo reports timezone %s but this adapter assumes %s — "
                        "deadlines may be offset", reported, STUDEO_TZ)

    def collect(self) -> Iterable[Item]:
        if not self.token:
            raise RuntimeError(self.disabled_reason or "Studeo is not configured")

        self.check_server_timezone()

        entries = self.session.get_json(
            f"{self.base_url}{EP_PLANO_ESTUDO}", headers=self._headers()
        )
        if not isinstance(entries, list):
            entries = (entries or {}).get("content") or []

        # Enrolment falls out of the agenda — no configured list, no extra request.
        self.disciplines = disciplines_from_plano(entries)
        log.info("studeo: %d agenda entr(ies) across %d discipline(s)",
                 len(entries), len(self.disciplines))
        for shortname, nome in self.disciplines.items():
            log.debug("  %s  %s", shortname, nome)

        for entry in entries:
            item = parse_plano_estudo_event(entry)
            if item is not None:
                yield item

    def fetch_questionario(self, questionario_id: int, disciplina_id: str) -> Item:
        """One activity by id, with its prazo (``dataFinal``).

        Not reachable from ``collect()`` yet: the agenda feed carries no
        questionnaire ids, so there is currently no way to enumerate them. Kept
        because the mapping is confirmed against a real payload and only the
        enumeration is missing.
        """
        payload = self.session.get_json(
            f"{self.base_url}{EP_QUESTIONARIO.format(id=questionario_id)}",
            headers=self._headers(),
        )
        return parse_questionario(payload, disciplina_id=disciplina_id)
