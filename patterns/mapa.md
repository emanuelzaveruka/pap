---
name: mapa
version: 3
formats: [docx]
template: patterns/templates/mapa-unicesumar.docx
---

# MAPA — Material de Avaliação Prática da Aprendizagem

Derived from **seven real documents**: one blank template supplied by Studeo and
six completed submissions across six disciplines (Tópicos em Computação II ×2 —
including a Scrum activity — Tópicos Especiais em Engenharia de Software I, Design
e Interação, Gerenciamento de Software, Projeto/Implementação e Teste de Software).

v1 guessed the content rules. v2 replaced them with what five documents showed.
v3 corrects v2, which turned a five-sample into an absolute and got the
`REFERÊNCIAS` rule wrong — see §4. Where the evidence is thin, this file now says
how thin.

## 1. Document shell — identical across all six

| Property | Value |
|---|---|
| Page | A4 — 8.27 × 11.69 in |
| Margins | 1.0 in on all four sides |
| Default font | Arial 11 (`docDefaults`, 22 half-points) |
| Header | University logo — one image in `word/media/` in every document |
| Title line | `MAPA – Material de Avaliação Prática da Aprendizagem` (en-dash) |
| Identity | 3-column table, rows merged, six fields |
| List styles | **None defined** — `List Bullet` / `List Number` do not exist |

## 2. Identity labels vary — match by meaning, never by string

The single most important finding. The same two fields are spelled six ways across
six documents:

| Field | Spellings seen |
|---|---|
| name | `Nome:` · `Acadêmico:` |
| RA | `R.A` · `R.A.` · `R.A:` · `R.A.:` |
| curso / disciplina / valor da atividade / prazo | stable in all six |

`deliverable.IDENTITY_PATTERNS` matches on the field, tolerating punctuation and
either name spelling, and **writes the label back exactly as the template spelled
it** so a filled document is indistinguishable from the blank one.

A literal-prefix matcher passed against the blank template and silently filled
nothing on four of the other five. The failure is silent — the label just stays
bare — which is why `test_every_committed_template_can_be_filled` is parametrized
over the directory rather than a fixed list.

## 3. Body structure — driven by the brief, not by a fixed skeleton

There is no single section skeleton. Every sample mirrors the structure of its own
activity brief. Two families appear:

**a. Question-numbered** (Gerenciamento de Software, Tópicos Especiais ATV-1)

```
1. <question restated, sentence case>
<prose answer>
2. <next question>
```

Where the brief asks sub-questions, each is restated verbatim as its own line and
answered under it — e.g. `Qual é o risco?` / `Como prevenir?` / `O que fazer se
acontecer?` / `Qual é a probabilidade?`, repeated for all five risks.

**b. ABNT-numbered dissertation** (Tópicos Especiais em Eng. de Software MAPA)

```
<WORK TITLE IN CAPITALS>
1 INTRODUÇÃO
2 <SECTION IN CAPITALS>
3 <SECTION IN CAPITALS>
```

Section number with **no dot**, heading in capitals — ABNT style. Only this sample
carries a work title and an `INTRODUÇÃO`.

**Rule for the generator: follow the brief.** If the brief enumerates questions,
restate and answer them in order. If it asks for a dissertation, use family (b).
Do not impose the other shape.

## 4. What the samples usually do NOT contain

Their absence is evidence, and a generator adds all of them by default. But
"usually" is doing real work here — see the correction below.

Across **six** completed submissions:

| | count |
|---|---|
| `REFERÊNCIAS` section | **1 of 6** |
| conclusion (`Conclusão` / `Considerações Finais`) | **1 of 6** — the same one |
| in-text citations `(AUTOR, ano)` | 0 of 6 |
| cover page | 0 of 6 |
| bullet lists | 0 of 6 |

**Version 2 of this file said "not one of the five has REFERÊNCIAS" and told the
generator never to add one.** That was drawn from five documents; the sixth — a
Scrum MAPA for Tópicos em Computação II — has both a `Referências` section and
`Considerações Finais`, and cites the disciplina's own livro. An absolute rule
from a five-sample was simply wrong.

The real rule: **the brief decides.** A brief that says *"fundamente no livro da
disciplina e apresente as referências"* gets a `REFERÊNCIAS` section; one that
says nothing gets none. Never add one unprompted, and never omit one that was
asked for.

Cover pages and bullet lists remain absent in all six — treat those as settled.

## 5. Length

Body word counts, excluding the identity block:

| Document | Words |
|---|---|
| MAPA – Design e Interação | 235 |
| ATV-1 – Tópicos Especiais | 560 |
| MAPA – Gerenciamento de Software | 698 |
| MAPA – Tópicos Especiais | 786 |
| MAPA – Projeto, Implementação e Teste | 1185 |

Roughly **600–1200 words for a MAPA** (3,5 points) and **~550 for an ATV** (0,5).
The 235-word outlier is a prototype-based activity where the deliverable is largely
images and structured lists rather than prose. Length follows the brief's demands,
not a fixed target.

## 6. Tables and figures

Content tables are normal and carry real weight — one sample has five (functional
requirements, non-functional requirements, business rules). They use a title row
followed by a header row (`Código` / `Descrição`), and codes are cross-referenced
between tables (`RN01 → RF1`).

Two documents embed a second image beyond the header logo (a use-case diagram, a
prototype screenshot), referenced from the text as `Abaixo diagrama de …`.
**The generator cannot produce these.** Where the brief requires a diagram or a
prototype, the deliverable must leave a clearly marked placeholder for the student
rather than describing a diagram that is not there.

## 7. Rules the generator must follow

- **Never invent a citation — including the bibliographic data.** This failed in
  a real generation: given the book's pages but not its front matter, the model
  produced `UNICESUMAR. Tópicos em Computação II. 2019` when the book's own ficha
  catalográfica reads `JOSÉ, Maria Isabel Jacob … Unicesumar, 2018`. The source
  was genuine and the citation was fabricated, which is the worst combination
  available: it looks checkable and is wrong. `archives/context.py` therefore
  always carries the front matter, and a missing datum must be written as
  `[INSERIR: …]` rather than guessed.
- **Never invent data.** No measurements, survey results, or company names
  presented as real. Where a sample uses a scenario, it comes from the brief.
- **Use the disciplina's livro as the source.** It is already downloaded and in
  Drive; grounding in it is what makes an answer specific rather than generic.
- **Write in pt-BR**, third person, impersonal — the register the samples use
  (`recomenda-se`, `é necessário`, `destaca-se`).
- **Restate the question as the heading** in family (a). Every sample does.
- **The platform never submits.** The file lands in Drive; the student submits it.

## 8. Output

- `…/Studeo/<ano>/<módulo>/<disciplina>/entregas/`
- `deliverable` row records `drive_file_id` and `pattern_version`
- Telegram notification with the direct Drive link
