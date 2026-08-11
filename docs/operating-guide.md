# Operating guide

How to actually run this thing. Every command here has been run against the live Studeo API,
a real Google account and a real Telegram bot — none of it is aspirational.

Read it in order the first time. After that, the two sections you will keep coming back to are
[Daily operation](#daily-operation) and [When something breaks](#when-something-breaks).

---

## 0. The one command to run first, always

```bash
pap doctor --live
```

`pap doctor` on its own answers *is it configured*. `--live` answers *do the credentials
actually work* — it authenticates for real against every service:

| Area | What it proves | Cost |
|---|---|---|
| `database` | Postgres accepts the user/password and the `pap` schema exists | one connection |
| `studeo` | login with `STUDEO_USER` / `STUDEO_PASSWORD` returns a JWT | one auth call |
| `google` | the refresh token still mints an access token, and Drive answers | one refresh + `about.get` |
| `telegram` | `getMe` — the bot token is valid | one API call |
| `email` | SMTP accepts the login, then quits. **Sends nothing.** | one connection |
| `observability` | Healthchecks accepts a ping | one ping |
| `llm` | each configured provider answers a minimal completion | a few tokens per provider |

Anything not configured reports `disabled`, not `failed` — a missing optional service is not a
problem. Exit code is `0` when there are no problems, `1` otherwise.

It **never prints a configuration value.** The Google check reports only the account's *domain*,
so the output is safe to paste into a chat or an issue. Preserve that property if you add a check.

A credential that expired silently is the single most common cause of a job that "just stopped
working". Run this before blaming the code.

---

## 1. First-time setup

Local (WSL) walkthrough with a throwaway Postgres is in **`docs/local-wsl-testing.md`**. The short
version, in dependency order:

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt && pip install -e .   # editable install is REQUIRED

docker compose up -d postgres                          # or point PG_HOST at the server
pap doctor                                             # names only — fix everything it flags
pap migrate                                            # creates the pap schema
pap doctor --live                                      # now prove the credentials work
```

Never `source .env`. It is read by python-dotenv, not by a shell — see the note at the end of
this guide, which cost an evening to learn.

### Google, once

Consent needs a browser, so it cannot run on the server:

```bash
pap auth --login        # on your laptop; prints three lines to paste into pap.env
pap auth --status       # anywhere; no API call
```

The three values (`GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN`) are all
Google needs to refresh non-interactively — nothing has to be copied to the headless box.

---

## 2. Daily operation

Four commands. Each is safe to run twice; that is the whole point of the design.

```bash
pap run studeo          # collect activities + deadlines, queue what is new, deliver
pap sync calendar       # push deadlines to Google Calendar (patches, never duplicates)
pap archive books       # download the course books and file them in Drive
pap dispatch            # deliver anything still queued (retry path)
```

### `pap run studeo`

Logs in, discovers the disciplines you are actually enrolled in, reads the agenda, stores items,
and lets the dispatcher decide what is new. A second run reports `new=0` and sends nothing.

```bash
pap run studeo --dry-run       # collect and report; writes nothing, sends nothing
pap run studeo --no-dispatch   # store and queue, deliver later
```

`--dry-run` is honest because `collect()` has no side effects — no DB writes, no sending, no
deciding what is new. Keep it that way.

### `pap sync calendar`

Only touches deadlines whose `due_at` differs from `synced_due_at`, so a re-run costs **zero API
calls**. A moved deadline patches its existing event; an event you cancelled by hand in Calendar
stays cancelled rather than being resurrected.

```bash
pap sync calendar --dry-run    # reports what would be created/patched; calls nothing
```

### `pap archive books`

Discovers each discipline's *material de estudo*, resolves the download link, streams the PDF to
`ARCHIVE_BOOKS_DIR`, hashes it, and uploads to
`Studeo/<ano>/<módulo>/<disciplina>/livros/` in Drive.

```bash
pap archive books --dry-run                 # one listing call per discipline, nothing else
pap archive books --discipline 2026_26_...   # a single discipline by shortname
pap archive books --limit 1                  # useful when testing a change
```

Output is a counter line:

```
downloaded=4 reused=0 uploaded=4 skipped=1 failed=0
```

- **`skipped`** — already uploaded. This is what a healthy re-run looks like (`skipped=5 failed=0`).
- **`reused`** — the bytes were already on disk, or an identical sha256 was already in Drive, so no
  second upload. The same PDF is often the livro for several offerings of a discipline.
- **`failed`** — one book failed; the others still ran. Deliberate: an early version wrapped only
  the download, so one bad book took the whole run down with it.

Transfers are resumable (`Range`), land in `*.part`, and are renamed only after the size checks
out. A truncated artifact that looks finished is worse than an obvious failure — the same rule the
database dump follows. `ARCHIVE_MAX_BOOK_MB` (default 250) guards the server's failing disk.

### `pap dispatch`

Scrapers never send. They write `pap.item` rows; the dispatcher turns genuinely new ones into
notifications. This is what makes a failed delivery lose nothing — the row stays queued and the
next run retries it with `attempts` incremented.

```bash
pap dispatch --dry-run
pap dispatch --limit 50
```

---

## 3. The LLM port

Three real adapters — `claude`, `openai`, `gemini` — behind one interface. `archives/` and
`resumes/` depend only on `llm/base.py` and never learn which vendor answered.

```bash
pap llm ping                      # every configured provider
pap llm ping --provider gemini    # one of them
pap llm usage --days 30           # calls, tokens and latency per provider, from the database
```

Set at least one key and it works:

```env
ANTHROPIC_API_KEY=sk-ant-...
LLM_PROVIDER_DEFAULT=claude
```

With no key at all, `pap llm ping` exits **2** (configuration error, nothing attempted) and names
the three variables it would accept.

### Choosing a provider per task

Resolution is `--provider` → per-purpose variable → default. Provider choice is *data*, not code:

```env
LLM_PROVIDER_DEFAULT=claude
LLM_PROVIDER_BOOK_RESUME=gemini     # long context, cheap per page
LLM_PROVIDER_DELIVERABLE=claude     # a MAPA has to follow the college's pattern
LLM_PROVIDER_CLASSIFY=openai
```

That is the abstraction you asked for: to A/B two vendors on the same task, change one line, or
pass `--provider` for a single run. Nothing in the domain code changes.

### What is tuned, and why

| Variable | Default | Note |
|---|---|---|
| `LLM_MAX_TOKENS` | `8000` | Requests above 16 000 stream automatically, to avoid request timeouts. |
| `LLM_THINKING` | `adaptive` | On Opus 5 `budget_tokens` is a 400; `adaptive` replaces it. |
| `LLM_EFFORT` | `high` | Lives under `output_config`. `xhigh`/`max` require thinking — with `thinking=disabled` it is clamped to `high` rather than failing on a nightly job nobody is watching. |

No sampling parameters are ever sent (`temperature` / `top_p` / `top_k` are 400s on Opus 5).
Refusal fallbacks are on, so a declined request re-runs on another model instead of failing the
job. The system prompt carries a cache breakpoint — the pattern spec is identical across every
chunk of a book, which turns a per-chunk cost into a one-off.

Every call is recorded in `pap.llm_call` (provider, model, tokens, latency, purpose) inside a
`finally`, so failures are recorded too. `pap llm usage` reads that table — cost comparisons come
from measurements, not guesses.

---

## 4. Ops

```bash
pap status --days 7      # recent runs, their status, and the notification queue
pap backup db            # pg_dump → gzip → Google Drive
pap backup verify        # restore the newest dump into a throwaway database
```

`pap backup verify` exists because a dump nobody has restored is a hypothesis. On this box that is
not pedantry: the single 223.6 G SSD carries boot, LVM, root and Postgres, and SMART already
reported *"likely to fail soon"*. The nightly backup is a precondition, not a nicety.

**Monitoring is a separate domain from notifications, on purpose.** GlitchTip answers *what broke,
where, why*; Healthchecks answers *did it run at all* — a job that never fires raises no exception,
so error tracking is structurally blind to it. Neither shares code or state with the Telegram
dispatcher, which is a product feature with a different audience.

Exit codes: **0** completed · **1** the work failed · **2** configuration error, nothing attempted.
That distinction is what lets systemd and Healthchecks tell "broken" apart from "misconfigured".

---

## 5. On the server

```bash
docker compose up -d                                  # postgres + glitchtip + healthchecks
docker compose --profile cli run --rm pap doctor --live
docker compose --profile cli run --rm pap migrate
docker compose --profile cli run --rm pap archive books
```

The `pap` service sits behind the `cli` profile so `compose up -d` never daemonises it. Jobs are
oneshot containers fired by host systemd timers:

```bash
sudo cp deploy/*.service deploy/*.timer /etc/systemd/system/
sudo mkdir -p /etc/systemd/system/docker.service.d
sudo cp deploy/docker-after-tailscaled.conf /etc/systemd/system/docker.service.d/
sudo systemctl daemon-reload
sudo systemctl enable --now pap-dispatch.timer pap-backup.timer pap-backup-verify.timer
sudo systemctl enable --now pap-run@studeo.timer
```

Two server-only rules that fail in confusing ways if ignored:

- `PAP_STATE_DIR` must be a Docker volume on ext4, never a Windows mount. `/mnt/c` silently ignores
  `chmod`, so a token written 0600 stays 0777 while the code believes otherwise. `pap doctor`
  probes for this.
- `PG_BIND_ADDRESS` must be the Tailscale IP (`<TAILSCALE_IP>`), never `0.0.0.0` — Docker publishes
  ports *ahead of* `ufw`, so a bare `5432:5432` exposes Postgres regardless of firewall rules.

---

## 6. When something breaks

Work down this list. It is ordered by how often each cause is the real one.

| Symptom | First thing to check |
|---|---|
| A job "stopped working" with no code change | `pap doctor --live`. A Studeo password change or a revoked Google grant looks exactly like a bug. |
| `password authentication failed` right after editing `.env` | CRLF line endings. `pap doctor` flags them. python-dotenv strips `\r`; a shell does not. |
| `400: chat not found` from Telegram | `TELEGRAM_CHAT_ID` must be numeric (or `@publicchannel`), never a username — and you must message the bot first, or the chat does not exist. |
| `Could not connect to PostgreSQL (host=postgres ...)` | `PG_HOST=postgres` resolves inside the compose network only. From the host use `localhost`; from another machine, the Tailscale IP. |
| Studeo returns 401 on every call | The token goes in `Authorization` **raw** — no `Bearer` prefix. Tokens last ~4 hours; `ensure_token` re-logs in. |
| A book fails with HTTP 500 | The download endpoint rejects `Accept: application/json` with `RESTEASY003635`. It needs `*/*`. Already handled — but this is the shape of that failure. |
| Every book after the first one fails | A failed statement aborts the transaction. `archive_books` rolls back per book for exactly this reason; if you add a handler, roll back there too. |
| The whole run exits 0 but nothing happened | Check `pap status`. `core/runs.py` always re-raises, so a genuinely broken run cannot exit 0 — "nothing happened" usually means `new=0`, which is correct. |
| `pg_dump: server version mismatch` | The Postgres server major and `postgresql-client` must match (17 today). Bump both together. On a WSL host you need `postgresql-client-17` from the PGDG repo; in the container they already match. |

Reading the ledger directly, when the CLI is not enough:

```sql
-- the run ledger. The monitor reads only this table, so it cannot drift from reality.
SELECT source, status, started_at, items_found, items_new, error_text
  FROM pap.source_run ORDER BY started_at DESC LIMIT 10;

-- anything still queued, and why it has not been delivered
SELECT channel, title, attempts, last_error
  FROM pap.notification WHERE state = 'pending' ORDER BY created_at;

-- which books made it to Drive
SELECT title, bytes, uploaded_at IS NOT NULL AS in_drive
  FROM pap.book ORDER BY id DESC;
```

---

## 7. `.env` lines this phase added

Add these to `pap.env` / `.env` yourself — `.env` and `.env.example` are unreadable to Claude Code
by design, so they have to be handed over rather than written:

```env
# --- LLM port (Phase 2) -----------------------------------------------------
# At least one key. Everything else has a working default.
ANTHROPIC_API_KEY=
OPENAI_API_KEY=
GEMINI_API_KEY=

LLM_PROVIDER_DEFAULT=claude
# Optional per-task overrides. Empty means "use the default".
LLM_PROVIDER_DELIVERABLE=
LLM_PROVIDER_BOOK_RESUME=
LLM_PROVIDER_CLASSIFY=

LLM_MODEL_CLAUDE=claude-opus-5
LLM_MODEL_OPENAI=gpt-5
LLM_MODEL_GEMINI=gemini-2.5-pro

LLM_MAX_TOKENS=8000
LLM_THINKING=adaptive
LLM_EFFORT=high

# --- Archive (Task 1) -------------------------------------------------------
# On the server these must live on the Docker volume, not /mnt/c.
ARCHIVE_BOOKS_DIR=/var/lib/pap/livros
ARCHIVE_MATERIALS_DIR=/var/lib/pap/materiais
ARCHIVE_DELIVERABLES_DIR=/var/lib/pap/entregas
ARCHIVE_MAX_BOOK_MB=250
```

`pap doctor` will tell you if any of them is missing or malformed — it prints names and
present/absent only, never a value.

---

## 8. Two rules worth repeating

**Never `source .env`.** It is read by python-dotenv, never by a shell. Unquoted values containing
spaces or parentheses are a shell syntax error, and on a CRLF file the shell appends `\r` to every
value while dotenv strips it. The two then disagree by one invisible character and authentication
fails with no visible cause. To inspect a value, use `dotenv_values`.

**Override on the command line, not by editing the file.** dotenv uses `override=False`, so the
real environment wins:

```bash
PG_HOST=localhost pap doctor --live
LLM_PROVIDER_BOOK_RESUME=gemini pap llm ping
```

---

## What is built, and what is next

| | Status |
|---|---|
| Phase 0 — foundation, dispatcher, backups, observability | complete, verified end to end |
| Phase 1 — Studeo: auto-login, discovery, agenda, deadlines → Calendar | complete (37 events created, patch-not-duplicate proven) |
| Task 1 — book download → Drive | complete (4 books, 2.1–17.9 MiB, re-run `skipped=5 failed=0`) |
| Phase 2 — the LLM port | complete (3 adapters, per-purpose resolution, usage ledger) |
| Phase 3 — MAPA deliverables (needs the book as context + the college's pattern) | next |
| Phase 4 — book resume feed to Telegram | next |
| Akita — full backfill of 2024/2025/2026 posts | after Studeo |
| LinkedIn / job search | backlog, deferred by decision |

Phases 3 and 4 were both blocked on the same thing, which is why the book pipeline came first: the
book is the *source* for the resume feed and the *context* for a MAPA. Both can start now.
