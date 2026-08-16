---
name: atividade
version: 1
formats: [docx]
template: patterns/templates/atividade-unicesumar.docx
---

# ATIVIDADE — the short graded activity

The small counterpart to a MAPA. Derived from one completed submission
(`ATIVIDADE 01`, Tópicos Especiais em Engenharia de Software I, 0,5 points) plus
the shell it shares with the MAPA family.

**One sample.** Everything below marked *(single sample)* is a description of that
document, not a confirmed rule. Widen it as more activities arrive — and bump
`version` when you do.

## 1. How it differs from a MAPA

This is the whole reason it is a separate pattern rather than a branch inside
`mapa.md`:

| | ATIVIDADE | MAPA |
|---|---|---|
| Title line | `ATIVIDADE 01` | `MAPA – Material de Avaliação Prática da Aprendizagem` |
| Worth | 0,5 | 3,5 |
| Length | ~550 words *(single sample)* | 600–1200 words |
| Work title | none | sometimes (ABNT family) |
| Introduction | none | in the ABNT family |
| Structure | numbered questions only | two families — see `mapa.md` §3 |

Everything else — A4, 1.0 in margins, Arial 11, header logo, the 3-column merged
identity table — is identical. The shell is shared; only the title line and the
body shape differ.

## 2. Identity labels

This sample uses `Acadêmico:` and `R.A.:` — a different spelling from the blank
MAPA template's `Nome:` and `R.A`. Both are handled by
`deliverable.IDENTITY_PATTERNS`, which matches the field and writes the label back
as the template spelled it. See `mapa.md` §2 for the full variance table; do not
normalise the labels.

## 3. Body structure

```
1. <question restated as a heading, sentence case, no trailing period>
<two to five prose paragraphs answering it>

2. <next question>
...
```

Observed in the sample:

- **Three questions, ~185 words each.** Each heading restates the question in
  sentence case — `1. Principais componentes para estruturar uma Fábrica de
  Software orientada a processos`. Note it is the *topic* of the question, not the
  interrogative form.
- **Numbered with a dot** (`1.`), unlike the MAPA ABNT family's dotless `1`.
- Continuous prose under each heading. Multiple paragraphs are normal; the sample
  averages four.
- No sub-headings. Where a MAPA restates sub-questions (`Qual é o risco?`), this
  one does not.

## 4. What the sample does not contain

Absence is evidence, and a generator adds all of these unprompted:

- No introduction and no conclusion — it opens on question 1 and stops after the
  last answer.
- No references section and no in-text citations.
- No bullet lists, no tables, no figures.
- No cover page. The identity table is the entire front matter.

Add none of it unless the brief asks.

## 5. Register

Same as the MAPA samples: pt-BR, third person, impersonal — `é necessário`,
`recomenda-se`, `destaca-se`. Explanatory rather than argumentative: the sample
states what a thing is and why it matters, without taking a contrarian position.

## 6. Rules the generator must follow

Identical to `mapa.md` §7 and repeated here so this file stands alone:

- **Never invent a citation.** The sample cites nothing, so there is no pressure to.
- **Never invent data** — no measurements, results or company names as fact.
- **Use the disciplina's livro as the source.**
- **Restate the question as the heading.** Every question in the sample does.
- **Answer the question asked.** At ~185 words per question, padding is visible.
- **The platform never submits.** The file lands in Drive; the student submits it.

## 7. Output

- `…/Studeo/<ano>/<módulo>/<disciplina>/entregas/`
- `deliverable` row records `drive_file_id` and `pattern_version`
- Telegram notification with the direct Drive link
