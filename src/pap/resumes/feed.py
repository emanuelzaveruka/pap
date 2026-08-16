"""The serial learning feed: one unit of one book, per run.

The shape you asked for, and the reason each rule exists:

**One unit per run.** The value is in reading a little every day, not in
receiving a book. The timer decides the cadence; this module always advances by
exactly one.

**It stops by itself.** When the last unit is delivered the book is marked
``completed`` and every later run skips it. A feed that silently restarts at
chapter one would be worse than useless — you would stop trusting the messages.

**Re-summoning is a deeper pass, not a repeat.** ``--again`` increments
``current_pass``; the prompt asks for more detail, exercises and
cross-references rather than the same notes again. Earlier passes are kept so
they can be compared.

**Nothing is generated twice.** ``UNIQUE (book_id, pass_number, unit_index)``
enforces it in the schema. `save_resume` returning None means another run got
there first, and this one steps aside rather than paying for a second call.

**Generated and sent are different facts.** The resume row is written before the
notification is queued, and ``sent_at`` is stamped separately. A delivery failure
must never cost the generation — the text is already stored, and `pap dispatch`
will retry the message.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from ..config import ResumeSettings, Settings
from ..db import Database
from ..llm import router
from .chunker import Unit, plan_units

log = logging.getLogger(__name__)

PURPOSE = "book_resume"
PROMPT_VERSION = "1"

SYSTEM = """Você escreve resumos de estudo a partir do material da disciplina, em \
{lang}. Quem lê está estudando para a prova e tem poucos minutos.

Regras:
- Baseie-se EXCLUSIVAMENTE no trecho fornecido. Não acrescente informação de fora \
e não invente exemplos, números, autores ou datas.
- Se o trecho não sustentar um ponto, omita-o. Um resumo curto e correto vale mais \
do que um completo e inventado.
- Vá direto ao conteúdo. Sem preâmbulo, sem "neste capítulo veremos", sem repetir \
o título.
- Destaque os termos técnicos que valem memorizar.
- Alvo: cerca de {target_words} palavras."""

STYLES = {
    "study-notes": (
        "Formato: notas de estudo. Uma frase de abertura com a ideia central, "
        "depois tópicos curtos com os conceitos-chave e, ao final, uma linha "
        "'Para lembrar:' com o que mais cai em prova."
    ),
    "summary": "Formato: resumo corrido em parágrafos, sem listas.",
    "outline": "Formato: apenas tópicos hierárquicos, sem prosa.",
}

# What a re-summoned pass asks for. Passing the same prompt again would deliver
# the same notes and the feature would be pointless.
PASS_INSTRUCTIONS = {
    1: "",
    2: ("Esta é a SEGUNDA passagem sobre o mesmo material. Vá além do resumo "
        "inicial: detalhe os mecanismos, relacione com os outros trechos da obra "
        "e proponha 3 perguntas de autoavaliação com resposta."),
}
DEEPER_PASS = (
    "Esta é a passagem {n} sobre o mesmo material. Assuma que o básico já é "
    "conhecido: aprofunde, traga implicações práticas, casos-limite e exercícios."
)


@dataclass
class Delivered:
    book_id: int
    book_title: str
    pass_number: int
    unit_index: int
    unit_label: str
    total_units: int
    resume_id: int | None
    queued: list[str]
    completed: bool
    words: int = 0


def pass_instruction(pass_number: int) -> str:
    if pass_number in PASS_INSTRUCTIONS:
        return PASS_INSTRUCTIONS[pass_number]
    return DEEPER_PASS.format(n=pass_number)


def build_prompt(unit: Unit, resume: ResumeSettings, pass_number: int,
                 book_title: str) -> str:
    parts = [
        f"Obra: {book_title}",
        f"Unidade: {unit.label} (páginas {unit.start_page}–{unit.end_page})",
        STYLES.get(resume.style, STYLES["study-notes"]),
    ]
    instruction = pass_instruction(pass_number)
    if instruction:
        parts.append(instruction)
    parts += ["\n# TRECHO DO MATERIAL\n", unit.text,
              "\nEscreva agora o resumo desta unidade."]
    return "\n\n".join(parts)


def deliver_next(
    db: Database,
    settings: Settings,
    book: dict,
    *,
    dry_run: bool = False,
    provider: str | None = None,
    channels: list[str] | None = None,
) -> Delivered | None:
    """Generate and queue the next unit for one book. None when nothing is due."""
    resume_settings = settings.resume.merged(book.get("config"))
    path = book.get("local_path") or ""
    if not path or not os.path.exists(path):
        log.error("book %s has no readable file at %r", book["id"], path)
        return None

    units = plan_units(path, unit=resume_settings.unit,
                       pages_per_unit=resume_settings.pages_per_unit)
    if not units:
        log.error("book %s produced no units", book["id"])
        return None

    pass_number = book.get("current_pass") or 1
    if book.get("total_units") != len(units):
        db.set_total_units(book["id"], len(units))

    index = db.next_unit_index(book["id"], pass_number)
    if index >= len(units):
        # Everything in this pass is delivered. Say so once, then stop.
        if book.get("status") != "completed":
            db.complete_book(book["id"])
            queued = _announce_completion(db, settings, book, pass_number,
                                          len(units), channels, dry_run=dry_run)
            log.info("book %s completed after %d unit(s)", book["id"], len(units))
            return Delivered(book["id"], book["title"], pass_number, len(units) - 1,
                             "", len(units), None, queued, True)
        return None

    unit = units[index]
    log.info("book %s pass %d: unit %d/%d — %s",
             book["id"], pass_number, index + 1, len(units), unit.label)

    if dry_run:
        return Delivered(book["id"], book["title"], pass_number, index, unit.label,
                         len(units), None, [], False)

    completion = router.complete(
        db, settings,
        purpose=PURPOSE,
        prompt=build_prompt(unit, resume_settings, pass_number, book["title"]),
        system=SYSTEM.format(lang=resume_settings.lang,
                             target_words=resume_settings.target_words),
        provider=provider,
    )
    text = completion.text.strip()
    if not text:
        raise ValueError(
            f"{completion.provider} returned an empty resume "
            f"(stop_reason={completion.stop_reason}) — raise LLM_MAX_TOKENS if the "
            f"budget went on thinking."
        )

    resume_id = db.save_resume(
        book_id=book["id"], pass_number=pass_number, unit_index=index,
        unit_label=unit.label, text=text,
        provider=completion.provider, model=completion.model,
        prompt_version=PROMPT_VERSION,
    )
    if resume_id is None:
        # Another run got this unit first. Its message is already queued.
        log.info("unit %d of book %s was already stored — skipping", index, book["id"])
        return None

    queued = _queue(db, settings, book, unit, index, len(units), pass_number,
                    text, channels)
    db.mark_resume_sent(resume_id)
    return Delivered(book["id"], book["title"], pass_number, index, unit.label,
                     len(units), resume_id, queued, False, len(text.split()))


def resume_dedupe_key(book_id: int, pass_number: int, unit_index: int, channel: str) -> str:
    return f"resume:{book_id}:{pass_number}:{unit_index}:{channel}"


def _queue(db, settings, book, unit, index, total, pass_number, text, channels) -> list[str]:
    from ..sinks.dispatcher import default_channels

    header = (f"<b>{book['title']}</b>\n"
              f"{unit.label} — {index + 1}/{total}"
              + (f" (passagem {pass_number})" if pass_number > 1 else ""))
    queued = []
    for channel in (channels or default_channels(settings)):
        if db.enqueue_notification(
            dedupe_key=resume_dedupe_key(book["id"], pass_number, index, channel),
            channel=channel,
            title=f"Resumo: {book['title']} — {unit.label}"[:120],
            body=f"{header}\n\n{text}",
            payload={"book_id": book["id"], "pass_number": pass_number,
                     "unit_index": index, "total_units": total},
        ):
            queued.append(channel)
    return queued


def _announce_completion(db, settings, book, pass_number, total, channels,
                         *, dry_run: bool) -> list[str]:
    """The explicit warning that the book is finished — and that the feed will not
    run again on its own."""
    from ..sinks.dispatcher import default_channels

    if dry_run:
        return []
    body = (f"<b>{book['title']}</b>\n"
            f"Passagem {pass_number} concluída — {total} unidade(s) entregues.\n\n"
            f"O feed deste livro para por aqui e não recomeça sozinho.\n"
            f"Para uma leitura mais profunda do mesmo material:\n"
            f"<code>pap resume book {book['id']} --again</code>")
    queued = []
    for channel in (channels or default_channels(settings)):
        if db.enqueue_notification(
            dedupe_key=f"resume-done:{book['id']}:{pass_number}:{channel}",
            channel=channel,
            title=f"Concluído: {book['title']}"[:120],
            body=body,
        ):
            queued.append(channel)
    return queued


def run_feed(
    db: Database,
    settings: Settings,
    *,
    book_id: int | None = None,
    dry_run: bool = False,
    provider: str | None = None,
    limit: int = 1,
) -> list[Delivered]:
    """Advance the feed. By default one unit, from the first book that has one."""
    if book_id is not None:
        books = [db.resume_progress(book_id)]
        if not books[0]:
            log.error("no book with id %s", book_id)
            return []
    else:
        books = db.books_pending_resume()

    delivered: list[Delivered] = []
    for book in books:
        if len(delivered) >= limit:
            break
        try:
            result = deliver_next(db, settings, book, dry_run=dry_run, provider=provider)
        except Exception as exc:  # noqa: BLE001 - one book must not stop the rest
            db.rollback()
            log.error("resume failed for book %s: %s", book.get("id"),
                      " ".join(str(exc).split())[:200])
            continue
        if result is not None:
            delivered.append(result)
    return delivered
