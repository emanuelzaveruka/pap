"""Turns an activity brief into the body text of a deliverable.

The split from ``deliverable.py`` is deliberate: this module decides *what the
document says*, that one decides *what it looks like*. Generation needs a vendor
and a network; rendering needs neither. Keeping them apart is what lets the
document format be tested exhaustively without a key.

The prompt is assembled from three parts, in this order:

1. **The pattern spec** (``patterns/<name>.md``) — the shape the college requires.
   Its ``version`` is returned alongside the text and stored as
   ``deliverable.pattern_version``, so a regenerated file traces back to the rules
   that produced it. That guarantee is only worth something if the spec is passed
   verbatim rather than summarised, so it is.
2. **The brief** — the activity's own questions and requirements, from Studeo.
3. **Context** — the disciplina's livro, optional but what makes an answer specific
   rather than generic.

Nothing here knows which vendor answers. It calls ``llm/router.py`` with the
``deliverable`` purpose and takes what comes back, so switching Claude → Gemini for
this task is an ``.env`` line.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field

from ..config import Settings
from ..db import Database
from ..llm import router
from ..llm.base import Completion

log = logging.getLogger(__name__)

PURPOSE = "deliverable"

_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclass(frozen=True)
class Pattern:
    """A parsed pattern spec."""

    name: str
    version: int
    body: str
    path: str
    formats: tuple[str, ...] = ("docx",)
    template: str = ""
    meta: dict = field(default_factory=dict)


def load_pattern(path: str) -> Pattern:
    """Read a pattern spec and its frontmatter.

    The frontmatter is parsed with a deliberately small reader rather than a YAML
    dependency: it holds five scalar keys and one list, and a spec that fails to
    load stops a deliverable from being generated at all.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"pattern spec not found: {path}. Patterns live in patterns/ — see "
            f"patterns/README.md."
        )
    with open(path, encoding="utf-8") as handle:
        raw = handle.read()

    match = _FRONTMATTER.match(raw)
    if not match:
        raise ValueError(
            f"{path} has no frontmatter. Every pattern starts with a --- block "
            f"carrying at least `name` and `version` (patterns/README.md)."
        )

    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" not in line or line.strip().startswith("#"):
            continue
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip()

    if "version" not in meta:
        raise ValueError(
            f"{path} declares no `version`. It is recorded on every deliverable "
            f"row, so a spec without one makes the record untraceable."
        )
    try:
        version = int(meta["version"])
    except ValueError as exc:
        raise ValueError(f"{path}: version must be an integer, got {meta['version']!r}") from exc

    formats = tuple(
        f.strip() for f in meta.get("formats", "docx").strip("[]").split(",") if f.strip()
    )
    return Pattern(
        name=meta.get("name") or os.path.splitext(os.path.basename(path))[0],
        version=version,
        body=raw[match.end():].strip(),
        path=path,
        formats=formats or ("docx",),
        template=meta.get("template", ""),
        meta=meta,
    )


SYSTEM = """Você redige trabalhos acadêmicos de graduação em português do Brasil, \
na norma culta, em terceira pessoa impessoal.

Você recebe três coisas: a ESPECIFICAÇÃO do formato exigido pela instituição, o \
ENUNCIADO da atividade e, quando disponível, o MATERIAL da disciplina.

Regras invioláveis:
- Nunca invente citações, autores, anos, edições ou páginas. Cite APENAS usando os \
dados bibliográficos presentes no MATERIAL fornecido. Se o enunciado pedir \
referências e algum dado não estiver no material, escreva \
[INSERIR: dado da referência que falta] em vez de supor. Uma referência plausível \
e errada é pior do que uma lacuna visível: ela parece conferível e não é.
- Nunca invente dados, medições, resultados de pesquisa ou nomes de empresas como \
se fossem reais. Cenários vindos do enunciado podem e devem ser usados.
- Siga a ESPECIFICAÇÃO quanto a estrutura, numeração e extensão. Ela descreve o que \
a instituição aceita; não a substitua pelo seu próprio formato preferido.
- Responda ao que foi perguntado. Encher linguiça para atingir uma extensão é pior \
do que uma resposta mais curta e completa.
- Se o enunciado exigir diagrama, protótipo ou imagem, escreva um marcador explícito \
na forma [INSERIR: descrição do que falta] — nunca descreva uma figura inexistente \
como se ela estivesse no documento.

Devolva APENAS o corpo do trabalho em markdown. Não repita o cabeçalho de \
identificação (nome, RA, curso), que já existe no documento."""


def build_prompt(pattern: Pattern, brief: str, *, context: str | None = None) -> str:
    """Assemble the user prompt. The pattern goes in verbatim — see module docstring."""
    parts = [
        "# ESPECIFICAÇÃO DO FORMATO",
        pattern.body,
        "\n# ENUNCIADO DA ATIVIDADE",
        brief.strip(),
    ]
    if context:
        parts += ["\n# MATERIAL DA DISCIPLINA (fonte para fundamentar as respostas)",
                  context.strip()]
    parts.append(
        "\nRedija agora o corpo do trabalho, seguindo a especificação acima."
    )
    return "\n\n".join(parts)


@dataclass(frozen=True)
class Generated:
    body: str
    pattern_name: str
    pattern_version: int
    provider: str
    model: str
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int | None

    @property
    def words(self) -> int:
        return len(self.body.split())


def generate_body(
    settings: Settings,
    pattern: Pattern,
    brief: str,
    *,
    context: str | None = None,
    db: Database | None = None,
    provider: str | None = None,
    max_tokens: int | None = None,
    run_id: int | None = None,
) -> Generated:
    """Generate the deliverable body. Records the call in ``llm_call`` when `db`
    is given; works without one so a dry run needs no database."""
    prompt = build_prompt(pattern, brief, context=context)
    log.info("generating %s body (pattern v%d, %d chars of prompt)",
             pattern.name, pattern.version, len(prompt))
    completion: Completion = router.complete(
        db, settings,
        purpose=PURPOSE,
        prompt=prompt,
        system=SYSTEM,
        max_tokens=max_tokens,
        provider=provider,
        run_id=run_id,
    )
    if not completion.text.strip():
        raise ValueError(
            f"{completion.provider} returned an empty body "
            f"(stop_reason={completion.stop_reason}). If it is a token limit, the "
            f"model spent the budget on thinking — raise LLM_MAX_TOKENS."
        )
    return Generated(
        body=completion.text.strip(),
        pattern_name=pattern.name,
        pattern_version=pattern.version,
        provider=completion.provider,
        model=completion.model,
        input_tokens=completion.input_tokens,
        output_tokens=completion.output_tokens,
        latency_ms=completion.latency_ms,
    )
