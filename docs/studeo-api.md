# Studeo (Unicesumar) — API discovery

> **STATUS: NOT YET FILLED IN.** This is the deliverable of Phase 1, Task 1, and it must be completed
> *before* any code is written in `src/pap/sources/studeo.py`. Guessing at the shape and correcting
> later costs more than an hour with DevTools open.

## Why this comes first

Studeo is a JavaScript single-page app — fetching the login page returns an empty shell — and it ships
an **official mobile app on iOS and Android**. A mobile app implies a JSON backend. Finding that backend
is far better than parsing rendered HTML: it is stable across UI redesigns, cheaper to request, and
usually returns exactly the fields needed.

Preference order, and the reason for it:

1. **iCal / `.ics` export** — if the Calendário section offers one, deadline sync becomes a feed
   subscription and most of Phase 1 disappears. **Check this first.**
2. **JSON API** — what the SPA and the mobile app call. The target.
3. **Authenticated HTML scraping** — workable, breaks on redesigns.
4. **Playwright** — last resort. Adds ~1.5 GB to the image and is the most likely way to OOM a 7.1 GiB
   box that is also running Postgres. Only if 1–3 are genuinely impossible.

## How to capture

1. Open Studeo in a browser with DevTools → Network → Fetch/XHR recording.
2. Log in. Visit **Atividades / MAPAs**, **Calendário**, **Disciplinas**, and the **biblioteca**.
3. Save the HAR (right-click → *Save all as HAR with content*) somewhere outside the repo — it contains
   live session tokens. `.har` is gitignored, but keep it off the repo tree anyway.
4. Fill in the sections below from the captured traffic.

## To fill in

### Authentication
- [ ] Login endpoint (method, URL, request body shape)
- [ ] Credential field: RA or CPF?
- [ ] Token type: JWT bearer, or session cookie?
- [ ] Where is it sent on subsequent calls (`Authorization` header, cookie)?
- [ ] Lifetime, and how expiry manifests (401? redirect to an HTML login page?)
- [ ] Is there a refresh endpoint, or does expiry require a full re-login?

### Structure
- [ ] Endpoint listing **modules** — and how the code `54/2025` appears in the response
      (single string? separate year and sequence fields? something else entirely?)
- [ ] Endpoint listing **disciplines** for a module; their stable id
- [ ] Which id is durable across semesters and safe to use as `external_id`

### Activities
- [ ] Endpoint listing activities/MAPAs
- [ ] Field carrying the **prazo**, and its format + timezone (this drives Calendar sync — an offset
      error puts every deadline on the wrong day)
- [ ] How status is represented (`Entregue` / `Pendente` / `Não entregue`)
- [ ] A stable per-activity id for `external_id`
- [ ] How attachments/templates attached to an activity are listed

### Calendar
- [ ] **Does an iCal / `.ics` export exist?** ← check before anything else
- [ ] If yes: its URL and how it is authenticated

### Library / books  ← gates Phase 4 entirely
- [ ] How a book is located for a discipline
- [ ] Whether the PDF can be downloaded with normal session credentials
- [ ] Whether downloads are watermarked, DRM'd, or rate limited
- [ ] Whether the mobile app uses a different (often simpler) host than the web app

### Decision
- [ ] Chosen tier: iCal / JSON API / HTML / Playwright
- [ ] Rate limit observed, and the `HTTP_REQUEST_DELAY_SECONDS` it justifies
- [ ] Anything that behaves differently between the web and mobile backends

## Rules for the adapter that follows

- **Read-only. The platform never writes to Studeo** — no submissions, no state changes. Deliverables go
  to Drive for a human to submit.
- The session token is cached via `core/secrets.py` (`studeo_session`). A fresh login on every run would
  be a login every few hours against the college's auth endpoint, which is the kind of pattern that gets
  an account flagged.
- Store `module.code` **exactly as Studeo reports it**. Parse `year`/`seq` out for sorting only, and let
  an unrecognised format degrade to "unsorted but still filed" rather than dropping the record.
- Never commit the HAR, and never paste tokens into this file.
