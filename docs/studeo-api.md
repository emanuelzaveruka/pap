# Studeo (Unicesumar) — API discovery

> **STATUS: PARTIALLY COMPLETE.** Everything under *Confirmed from the JS bundle* was read out of
> Studeo's own published JavaScript without logging in. Everything under *Still to confirm* needs one
> authenticated DevTools session — see the checklist at the end.
>
> `src/pap/sources/studeo.py` is still not written, deliberately: the endpoint **names** are known but
> their request and response **shapes** are not, and guessing those costs more than confirming them.

## Confirmed from the JS bundle (no login required)

Read from `https://studeo.unicesumar.edu.br/bundle/bundle.min.9fe71f77.js` (3.3 MB; the hash changes
on each deploy — find the current one in the page source).

| Fact | Detail |
|---|---|
| Front end | **AngularJS SPA** (`br/edu/unicesumar/web-angular-layout`). The HTML shell contains no data. |
| CDN / WAF | **Cloudflare**, plus Dynatrace RUM (`ruxitagentjs`). No bot-challenge seen on public paths. |
| Static assets | `studeostatic.unicesumar.edu.br` |
| API style | **JSON REST**, per-domain "controllers" — exactly the API the mobile app uses. |
| Auth | **JWT**. The bundle decodes the token client-side and reads `payload.dat.def.NEST` for the profile, so the token is a standard three-part JWT and its claims are readable without calling anything. |
| Base URL | `RestConfig.baseDomainURL`, assembled at runtime from injected globals (`localBaseApiAvaDomainURL`, `localBaseConteudoAvaURL`, …). **Not** `studeo.unicesumar.edu.br` — requests there return 404. Read the real host from any XHR in DevTools. |

### Endpoints found (names confirmed; semantics inferred from surrounding code)

**Auth**
```
POST /auth-api-controller/auth/token                 obtain the JWT
GET  /auth-api-controller/auth/token/time-info       token lifetime  <- answers "how long is it valid"
GET  /controle-acesso-api-controller/api/usuario/info-login
```

**Activities — the Phase 1 target**
```
GET /ambiente-api-controller/api/aluno/disciplina/afazer      "a fazer" = the TO-DO list
GET /objeto-ensino-api-controller/api/questionario/afazer/    questionnaires still to answer
GET /objeto-ensino-api-controller/api/agendamento             scheduling / dated items
GET /objeto-ensino-api-controller/api/acompanhamento          progress tracking
GET /objeto-ensino-api-controller/api/plano-estudo/disciplinas-usuario
```
The `afazer` response is summed via `e.data.somaTotal`, so it returns an object with a total plus a
collection — likely the per-discipline pending counts that drive the dashboard badge.

**Structure — the `54/2025` module codes**
```
GET /ambiente-api-controller/api/aluno/disciplina             disciplines
GET /ambiente-api-controller/api/aluno/disciplina/matriculados enrolled
GET /ambiente-api-controller/api/aluno/disciplina/curricular/
GET /ambiente-api-controller/api/disciplina/filtro/{term}
GET /ambiente-api-controller/api/disciplina-matricula/combo-modulo       <- module list
GET /ambiente-api-controller/api/disciplina-matricula/combo-modulo-full
GET /ambiente-api-controller/api/disciplina-matricula/combo-ano          <- year list
GET /ambiente-api-controller/api/curso
```
`combo-modulo` + `combo-ano` are almost certainly where `54/2025` comes from — check whether the code
arrives as one string or as separate module/year fields, since that decides the parser in
`core/models.py::parse_module_code`.

**Materials and downloads — gates Phase 4**
```
GET /central-anexo-api-controller/api-conteudo/download        RestConfig.baseDownloadURL
GET /central-anexo-api-controller/api-conteudo/display
GET /central-anexo-api-controller/api/anexo/findBy/hash/{hash}
GET /central-anexo-api-controller/api/anexo/findBy/id/{id}
```
Attachments are addressed **by hash** — which pairs naturally with `pap.attachment.sha256` and means
content-level dedupe is likely free.

**Library**
```
GET /ambiente-api-controller/api/acesso-biblioteca-zbra/busca-acesso
```
Returns `{ linkAcesso }`, which the app opens in a new window — so the virtual library is a
**third-party platform (Zbra) reached by a handoff link**, not files served by Studeo. Phase 4's
"download the book PDF" assumption may not survive this; confirm before building the resume feed.

## Routes and identifiers (from real observed URLs)

Studeo uses AngularJS hash routes, so these are **client-side routes, not endpoints** — but the ids in
them are the ids the API uses.

```
#!/app/studeo/aluno/ambiente/disciplina                              discipline list
#!/app/studeo/aluno/ambiente/disciplina/{disciplinaId}               one discipline
#!/app/studeo/aluno/ambiente/disciplina/{disciplinaId}/questionario/{questionarioId}
```

Real examples:

```
disciplinaId    2026_26_CURSO14NA-52_EGRAD_DISC200_026
questionarioId  354805, 360269      (a plain integer, stable per activity)
```

### Decomposing `disciplinaId` — INFERRED, worth confirming

```
2026_26_CURSO14NA-52_EGRAD_DISC200_026
 |    |      |     |    |     |      |
 |    |      |     |    |     |      +-- ? sequence / offering
 |    |      |     |    |     +--------- discipline code (DISC200)
 |    |      |     |    +--------------- EGRAD = graduação
 |    |      |     +-------------------- 52  <-- looks like the MODULE number
 |    |      +-------------------------- course/class code (CURSO14NA)
 |    +--------------------------------- ? intake
 +-------------------------------------- 2026  <-- year
```

If `2026` + `-52` really are year and module, then **`module.code = "52/2026"` is derivable from the
discipline id alone**, and matches the codes you listed (`51/2026`, `52/2026`, `53/2026`, `54/2026`).
Confirm against `combo-modulo` before relying on it — but if it holds, no extra request is needed to
build the Drive folder tree.

### Activity naming

Activities are questionnaires, conventionally named **AE1, AE2, AE3 and MAPA**. Each is one
`questionarioId`. The adapter should use that integer as `Item.external_id` (prefixed with the
discipline, since ids may not be globally unique) and keep the AE/MAPA label as the title.

## Studeo already publishes a Google Calendar per class

The biggest find, and it may reshape Phase 1.

```
GET /ambiente-api-controller/api/turma-sincronizacao-classroom/id-calendario-turma/{turma}
```

The screen `aluno-ambiente-calendario-google-disciplina` calls this, receives a **Google Calendar id**,
and opens `calendar.google.com/calendar/embed?src=<id>`. So Unicesumar syncs classes to **Google
Classroom**, and Classroom creates a real Google Calendar per class.

Why this matters: if those calendars already carry activity deadlines, the correct design is not
"scrape activities and create events" but **subscribe to the university's calendar** — or read it with
the Calendar API and mirror only what is missing. That would be more accurate than anything scraped
(it is the registrar's own data) and dramatically less code.

**Check this before writing `sources/studeo.py`.** Open that screen, get the calendar id, and look at
what the calendar actually contains — deadlines for AE1/AE2/AE3/MAPA, or only live-class sessions. The
answer decides whether Phase 1 is a scraper or a subscription.

## Confirmed screen endpoints

Feature code is lazy-loaded per screen; each screen's bundle names its own calls.

```
aluno-ambiente-disciplina.min.js       (discipline screen)
  /ambiente-api-controller/api/aluno/disciplina
  /ambiente-api-controller/api/disciplina-matricula
  /ambiente-api-controller/api/disciplina-provas-online
  /central-anexo-api-controller/api-conteudo/display
  references: livro / livros, arquivo.linkDownload, Cronograma, PRAZO

aluno-ambiente-questionario.min.js     (activity screen)
  /objeto-ensino-api-controller/api/questionario
  fields seen: descricao, descricaoHtml, nota, situacao, tentativa
```

**The book download is a `linkDownload` field on a file object** in the discipline payload — the
"baixar" span you described. That is a direct link, which contradicts the earlier worry that books
only live behind the third-party Zbra library. Zbra appears to be the *general* virtual library;
course books look directly downloadable. Good news for Phase 4, still worth confirming.

## Still to confirm (one authenticated DevTools session)

Ordered by how much each answer changes the design.

- [ ] **Does the class Google Calendar already contain activity deadlines?** Open
      *Calendário Google* for a discipline, copy the calendar id, inspect it. If yes, Phase 1 becomes
      a subscription rather than a scraper — answer this before writing any adapter code.
- [ ] The **base API host** — read it off any XHR request URL (it is not `studeo.unicesumar.edu.br`).
- [ ] `POST /auth-api-controller/auth/token`: request body (RA or CPF? grant_type?) and response shape.
- [ ] Is the JWT sent as `Authorization: Bearer …` or a custom header? (The bundle sets an
      `Authorization` flag per request, so some calls deliberately go unauthenticated.)
- [ ] Token lifetime from `/auth/token/time-info`, and whether a refresh endpoint exists.
- [ ] Open one activity (`.../questionario/354805`) and capture the call it makes. **Does the response
      carry the prazo?** That single field decides whether the Calendar sync can work at all.
- [ ] How `Entregue / Pendente / Não entregue` appears — probably `situacao`.
- [ ] Does the discipline response list its questionnaires (so activities come from ONE call per
      discipline), or must each be fetched by id?
- [ ] Confirm the `disciplinaId` decomposition above — is `-52` really the module?
- [ ] The book `linkDownload`: is it a direct file URL, or does it need the download controller?

### How to capture

1. Open Studeo with DevTools → Network → Fetch/XHR recording, then log in.
2. Visit **Atividades/MAPAs**, **Calendário**, **Disciplinas**, and the **biblioteca**.
3. Right-click → *Save all as HAR with content*, somewhere **outside this repo** — it contains a live
   token. `.har` is gitignored, but keep it off the tree anyway.
4. Fill in the checklist above. Do not paste tokens into this file.

## Why this comes first

Studeo is a JavaScript single-page app — fetching the login page returns an empty shell — and it ships
an **official mobile app on iOS and Android**. A mobile app implies a JSON backend. Finding that backend
is far better than parsing rendered HTML: it is stable across UI redesigns, cheaper to request, and
usually returns exactly the fields needed.

Preference order, and the reason for it:

1. **iCal / `.ics` export** — if the Calendário section offers one, deadline sync becomes a feed
   subscription and most of Phase 1 disappears. Nothing in the bundle hints at one, so this now looks
   unlikely — but it is cheap to check while you are in there.
2. **JSON API** — what the SPA and the mobile app call. **This is confirmed to exist**, so it is the
   target.
3. **Authenticated HTML scraping** — workable, breaks on redesigns.
4. **Playwright** — last resort. Adds ~1.5 GB to the image and is the most likely way to OOM a 7.1 GiB
   box that is also running Postgres. Only if 1–3 are genuinely impossible.

## Rules for the adapter that follows

- **Read-only. The platform never writes to Studeo** — no submissions, no state changes. Deliverables go
  to Drive for a human to submit.
- The session token is cached via `core/secrets.py` (`studeo_session`). A fresh login on every run would
  be a login every few hours against the college's auth endpoint, which is the kind of pattern that gets
  an account flagged.
- Store `module.code` **exactly as Studeo reports it**. Parse `year`/`seq` out for sorting only, and let
  an unrecognised format degrade to "unsorted but still filed" rather than dropping the record.
- Never commit the HAR, and never paste tokens into this file.
