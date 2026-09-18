# Studeo (Unicesumar) — integration notes

> **Scope of this file.** It records the *design consequences* of integrating with the college's
> student portal, not a map of it. The endpoint inventory, the exact request shapes and the capture
> procedure were removed before this repository was made public: they are a third party's private
> API, and publishing a step-by-step guide to it serves nobody. The adapter itself
> (`src/pap/sources/studeo.py`) remains complete and readable — the endpoints it uses are constants
> at the top of the file.
>
> The integration reads **only the author's own enrolment, with the author's own credentials**, at a
> deliberately low request rate (`HTTP_REQUEST_DELAY_SECONDS`).

## What the adapter actually does

Plain JSON over HTTPS against the portal's API host. No browser, no Playwright — Phase 1 settled
this, and it is why the base image stays ~1.5 GB smaller than it would otherwise be.

The flow is: authenticate with `STUDEO_USERNAME` + `STUDEO_PASSWORD` → cache the JWT → read the
study-plan feed → derive the enrolled disciplines from that same response → emit one `Item` per
agenda entry. Login is automated; nothing is pasted by hand.

The token is cached in `PAP_STATE_DIR` (0600), keyed by a fingerprint of the credentials, so
rotating either invalidates the cache automatically. **Only the token and its expiry are stored —
never the password.**

## The findings worth keeping

These are the things that cost real debugging time, and they are the reason the adapter looks the
way it does.

**Authentication failures are indistinguishable from each other.** The API answers a plain `401` for
a correctly-numbered RA in the wrong *format*, for a wrong password, and for a token sent with the
wrong header convention. Three different causes, one identical symptom, and the error text points at
none of them. `pap doctor` exists largely because of this: it validates the *shape* of
`STUDEO_USERNAME` locally, so the most common cause is ruled out before anyone goes looking at the
password. `_check_studeo_username` reports the shape and never the value.

**Timestamps are epoch milliseconds, and the academic calendar is not UTC.** Converting in the wrong
zone shifts every deadline by hours while still looking entirely plausible. Conversion therefore
happens in exactly one place (`epoch_ms_to_datetime`) and always yields a timezone-aware value;
`sinks/gcalendar.py` refuses a naive datetime for the same reason.

**The feed repeats entries.** One real response carried the same live class four times. `external_id`
is derived from the identifying fields only, so `UNIQUE (source, external_id)` absorbs the repeats —
the constraint *is* the dedupe, not a SELECT-then-INSERT around it.

**Enrolment is discovered, never configured.** Every agenda entry names its own discipline, so
`disciplines_from_plano()` derives the enrolled set from a call the adapter already makes. There is
no discipline list to maintain and no extra request to make. `STUDEO_DISCIPLINAS` remains only as an
override.

**A discipline id encodes its module**, and the activity description carries the same module code
independently. The adapter derives it from both and logs a disagreement rather than silently
trusting either — if one of the two patterns drifts, that is how it becomes visible.

**The download endpoint rejects `Accept: application/json`.** It answers HTTP 500 with a message
naming nothing about the header, which reads as a server fault rather than a client one.
`_headers(accept="*/*")` exists for this.

**A download link is signed and short-lived**, so it is resolved immediately before the transfer and
never during discovery. That is also what keeps `pap archive books --dry-run` to one listing call per
discipline.

## Known gap

The agenda feed carries no questionnaire ids, so AE1/AE2/MAPA deadlines cannot be *enumerated* —
only opened individually once the id is known. `parse_questionario` is confirmed against a real
payload; only the listing is missing.

## Rules for any adapter that follows

1. `collect()` is side-effect free — no writes, no sends, no deciding what is new. That is what makes
   `--dry-run` honest.
2. Never let a volatile field into `content_hash`. A fetch timestamp or a session id makes every run
   look like a change and re-notifies.
3. One bad item must not end the run. Wrap discovery and per-item work separately, and roll back on
   failure or the connection stays in an aborted transaction.
4. Rate-limit deliberately, and identify the client honestly in `HTTP_USER_AGENT`.
