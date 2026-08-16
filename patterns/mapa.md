---
name: mapa
version: 2
formats: [docx]
template: patterns/templates/mapa-unicesumar.docx
---

# MAPA — Material de Avaliação Prática da Aprendizagem

Derived from **six real documents**: one blank template supplied by Studeo and five
completed submissions across five disciplines (Tópicos em Computação II, Tópicos
Especiais em Engenharia de Software I, Design e Interação, Gerenciamento de
Software, Projeto/Implementação e Teste de Software).

Version 2 replaces the guessed content section of version 1 with what the evidence
actually shows. Where the sample is thin or contradictory, this file says so rather
than inventing a rule.

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

## 4. What the samples do NOT contain

Stated explicitly because their absence is evidence, and because a generator will
otherwise add them by default:

- **No references section.** Not one of the five has `REFERÊNCIAS`.
- **No in-text citations.** No `(AUTOR, ano)` anywhere.
- **No conclusion in most.** Only the ABNT-style one ends with a closing section;
  the others simply stop after the last answer.
- **No cover page.** The identity table is the whole front matter.
- **No bullet lists.** Enumeration is done with numbered headings or tables.

Do not add any of these unless the brief asks for it.

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

- **Never invent a citation.** Fabricated authors are undetectable to a skim and
  fatal on a check. The samples cite nothing, so there is no pressure to.
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
