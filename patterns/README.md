# Pattern documents

A **pattern document** is a versioned spec describing what a generated deliverable must contain:
required sections, formatting, citation style, length, tone. It is combined with the activity brief to
form the prompt in Phase 3 (`archives/deliverable.py`).

One file per activity type, e.g. `mapa.md`, `estudo-de-caso.md`, `fichamento.md`.

## Front matter

Every pattern starts with:

```markdown
---
name: mapa
version: 1
formats: [docx, pdf]
---
```

`version` is recorded on the `pap.deliverable` row as `pattern_version`. **Bump it whenever the spec
changes.** That is what lets a regenerated document be traced back to the exact spec that produced it —
without it, two files generated months apart look identical in the database and there is no way to tell
which rules each one followed.

## Writing a pattern

Describe the *requirements*, not the content. The activity brief supplies the subject; the pattern
supplies the shape. If Studeo provides its own template for an activity type, the pattern should mirror
that template's structure rather than invent one — the deliverable has to be acceptable to the people
grading it.

Nothing here is submitted automatically. Generated files land in Drive under
`.../<disciplina>/entregas/` for you to review and submit yourself.
