# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A containerized personal automation platform. Sources are scraped into a local PostgreSQL base; a
dispatcher turns genuinely new items into notifications; domain services generate documents and study
resumes through a vendor-neutral LLM port. It runs on **`pap-server`** (Ubuntu 26.04, headless, reachable
over Tailscale at `<TAILSCALE_IP>`) as Docker containers fired by host systemd timers.

Source priority: **Studeo (Unicesumar) → Akita → LinkedIn → future.**

Full scope and phasing: `~/.claude/plans/we-will-go-create-starry-puppy.md`.
**Phase 0 (foundation) is implemented. Phases 1–6 are not yet built.**

Step-by-step local runbook (throwaway Postgres, Telegram bot, Google consent): **`docs/local-wsl-testing.md`**.

## Commands

```bash
# Setup (src layout — editable install REQUIRED or `python -m pap` won't resolve)
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt && pip install -e .

# Tests (pure logic; no DB, no network)
pytest
pytest tests/test_telegram.py::test_no_content_is_lost_when_splitting
pytest -k dedupe

# Configuration report — prints NO values, only names and present/absent
pap doctor
pap doctor --no-db            # skips the connectivity check

# Schema
pap migrate --dry-run
pap migrate

# Run a source
pap sources                   # list registered adapters
pap run fake --dry-run        # collect and report; writes nothing, sends nothing
pap run fake                  # store, queue, deliver
pap run fake --boom           # raise on purpose, to verify error reporting
pap run fake --no-dispatch    # queue only

# Notifications
pap dispatch --dry-run
pap dispatch
pap notify --channel telegram # one-off smoke test of a channel

# Ops
pap status --days 7
pap backup db
pap backup verify             # restore the newest dump into a throwaway DB
pap auth --status             # Google credential state; makes no API call
pap auth --login              # consent flow; prints the .env lines. Needs a browser ON THIS MACHINE
```

### In Docker (how it actually runs on the server)

```bash
docker compose up -d                                  # postgres + glitchtip + healthchecks
docker compose --profile cli run --rm pap doctor
docker compose --profile cli run --rm pap migrate
docker compose --profile cli run --rm pap run studeo
```

The `pap` service sits behind the `cli` profile deliberately, so `compose up -d` never daemonises it.
Jobs are oneshot containers fired by host systemd timers (`deploy/`).

## Architecture — three ports, one pipeline

The pipeline is always: **collect → hash → upsert → enqueue → deliver**, wrapped in a run ledger.
`runner.run_source()` is the only place that knows how the pieces fit together.

- **`core/registry.py`** — `SourceAdapter` port. Adding a source costs one file: implement `collect()`,
  decorate with `@register`, import it in `sources/__init__.py`. If a new source needs changes anywhere
  else, the abstraction is wrong.
- **`sinks/base.py`** — `Sink` port (`telegram`, `email`, later `gcalendar`, `gdrive`).
- **`llm/base.py`** — `LLMProvider` port (Phase 2: `claude`, `openai`, `gemini`). **`archives/` and
  `resumes/` depend only on this interface and never learn which vendor answered.** Provider resolves
  per *task* (`LLM_PROVIDER_BOOK_RESUME`, `LLM_PROVIDER_DELIVERABLE`, …) with a `--provider` override.
- **`sinks/dispatcher.py`** — Central Notifications. Scrapers never send; they write `pap.item` rows.
- **`observability.py`** — the ops domain, **deliberately separate from notifications**.

## Non-obvious constraints

- **`.env` is unreadable to Claude Code** (denied in `.claude/settings.json`, along with `secrets/`,
  `token.json` and `client_secret*.json`). Work from `.env.example`. Override variables on the command
  line for a run (dotenv uses `override=False`, so the real environment wins). Never `Write` `.env`.
- **`pap doctor` must never print a configuration value.** It reports names and present/absent only, so
  its output is safe to paste anywhere. Preserve that property when adding checks. It currently probes
  four things that otherwise fail silently: `.env` line endings, whether the state dir honours `chmod`,
  the *shape* of `TELEGRAM_CHAT_ID`, and connection errors (via `db.connection_hint`).
- **`TELEGRAM_CHAT_ID` must be numeric (or `@publicchannel`), never a username.** A bot cannot message
  itself and cannot open a conversation — you must message it first or the chat does not exist. The
  send-time error is `400: chat not found`, which names neither the variable nor the cause.
- **Idempotency is in the schema, not the code.** `UNIQUE (source, external_id)` on `item` and
  `UNIQUE (dedupe_key)` on `notification` are what make re-runs safe. The `UNIQUE` violation *is* the
  dedupe — never replace it with a SELECT-then-INSERT, which reintroduces the race it exists to prevent.
- **Scraping never notifies.** An adapter's `collect()` must be side-effect free: no DB writes, no
  sending, no deciding what is new. That is what makes `--dry-run` honest.
- **`content_hash` covers only meaningful fields** (`core/dedupe.py`). Anything volatile — fetch
  timestamps, session ids, tracking parameters — must stay out, or every run looks like a change and
  re-notifies. `discipline_name` is deliberately excluded: renaming a discipline must not mark every
  activity in it as changed.
- **Telegram rejects messages over 4096 characters.** `sinks/telegram.py` splits on paragraph → line →
  word boundaries and falls back to sending a `.md` document past `MAX_MESSAGES` chunks. This is handled
  in Phase 0 rather than Phase 4 because book resumes will exceed it routinely. HTML parse mode is used
  because Telegram's legacy Markdown breaks on unbalanced `*`/`_`, which course material contains
  constantly, and a parse failure rejects the whole message.
- **Observability is not notification.** GlitchTip answers *what broke, where, why*; Healthchecks answers
  *did it run at all* — a job that never fires raises no exception, so error tracking is structurally
  blind to it. Neither shares code or state with the Telegram dispatcher, which is a product feature with
  a different audience. Do not merge them.
- **`core/runs.py` always re-raises.** Swallowing an exception there would let a broken run exit 0 and
  look healthy to systemd — exactly the silent failure this design exists to prevent.
- **Short-lived processes must `flush()` Sentry.** The SDK reports on a background thread; a container
  exiting immediately after an exception would otherwise discard the event. `source_run` does this.
- **Secrets are scrubbed centrally** (`observability._scrub`), not by trusting callers. Every value in
  `Settings.secret_values` is replaced throughout the event payload — a password reaches an error report
  via a connection string in an exception message far more often than through any field we could
  allowlist. Values under 8 characters are not registered: redacting them would corrupt unrelated text.
- **Google scopes are minimal and should stay that way.** `drive.file` grants access **only to files this
  app created** — it cannot see the rest of your Drive. Widening that is a decision to make explicitly.
- **`/mnt/c` silently ignores `chmod`.** `core/secrets.py` writes tokens 0600, but WSL's DrvFs keeps
  them 0777 — the call succeeds and the file stays world-readable while the code and its tests both
  believe otherwise. `pap doctor` probes for this. Local testing only; on the server `PAP_STATE_DIR`
  must be a Docker volume on ext4, never a Windows mount.
- **`.env` is read by python-dotenv, never by a shell.** Do not `source` it: unquoted values with
  spaces or parentheses are a shell syntax error, and on a CRLF file the shell appends `\r` to every
  value while dotenv strips it — the two then disagree by one invisible character and authentication
  fails with no visible cause. `doctor` flags CRLF for this reason.
- **Build connection strings with `db.conninfo()`**, which uses `psycopg.conninfo.make_conninfo`.
  Hand-formatted `key=value` breaks on a password containing a space or quote, and fails as a libpq
  parse error that looks nothing like a credential problem. `Settings.conninfo` was removed so nothing
  can reach for the unescaped version; `conninfo_safe` is display-only.
- **Google credentials live in `.env`, not in a file.** `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` /
  `GOOGLE_REFRESH_TOKEN` are all Google needs to refresh non-interactively, so nothing has to be copied
  to the headless server. `GOOGLE_TOKEN_FILE` is still honoured as a fallback, but env wins when both
  are set. The **access** token is the exception: short lived and disposable, so it is cached in
  `PAP_STATE_DIR` keyed by a fingerprint of client id + refresh token — rotating either invalidates it
  automatically rather than handing back a token for the wrong account.
- **OAuth consent cannot run on the server** (headless). `pap auth --login` runs on a machine with a
  browser and *prints* the three values to paste into `pap.env`. `access_type=offline` +
  `prompt=consent` is what actually returns a refresh token — without both, Google issues an access
  token only and the failure surfaces an hour later rather than at login.
- **google-auth stores `Credentials.expiry` as naive UTC.** Handing it a timezone-aware datetime raises
  `TypeError` inside the library instead of treating the token as expired. Use
  `google_auth.naive_utc_now()` and normalise anything read back from the cache.
- **Editing an applied migration is refused.** `db.apply_migrations` checksums each file; a changed file
  raises `MigrationError`. Add a new numbered file instead.
- **The Postgres server major version must match `postgresql-client` in the app image** (both 17 today).
  `pg_dump` refuses outright to dump a server newer than itself, and `pap backup verify` replays the dump
  through `psql`, so a mismatch either way fails during a restore — the worst moment to find out. Bump
  both together. Note Ubuntu 24.04's `postgresql-client` is **16**, so running backups on a WSL host
  against the 17 container needs `postgresql-client-17` from the PGDG repo; inside the container the
  versions already match. `_dump` prints both fixes when it hits this.
- **Docker publishes ports ahead of `ufw`.** A bare `5432:5432` would expose Postgres on every interface
  regardless of firewall rules. `docker-compose.yml` binds to `${PG_BIND_ADDRESS}` — set it to
  `<TAILSCALE_IP>` (Tailscale) and never `0.0.0.0`.
- **Docker must start after `tailscaled`**, or publishing to the Tailscale IP fails with *"cannot assign
  requested address"*. Install `deploy/docker-after-tailscaled.conf`.
- **`glitchtip` and `healthchecks` databases are created by `deploy/postgres-init/`, which runs ONLY on
  first init of an empty `pgdata` volume.** Adding the stack to an existing volume needs them created by
  hand — see the script header.
- **Playwright is not in the base image** (~1.5 GB). Phase 1 discovery decides whether Studeo needs a
  browser at all. If it does, it goes in `Dockerfile.browser` with `mem_limit` — concurrent Chromium is
  the most likely way to OOM a 7.1 GiB box also running Postgres.
- **The server's disk reported "likely to fail soon"** and carries boot, LVM, root and Postgres. The
  nightly `pap backup db` → Google Drive is a precondition, not a nicety, and `pap backup verify` exists
  because a dump nobody has restored is a hypothesis. `_dump` deletes a partial file on failure: a
  truncated dump that looks like a backup is worse than none.

## Status

**Phase 0 is complete and verified end to end** against a local Postgres 17 container and real
Telegram + Google accounts:

| Guarantee | How it was proven |
|---|---|
| collect → store → queue → deliver | 3 fake items became 3 Telegram messages |
| a delivery failure loses nothing | first attempt failed (bad chat id), retry delivered — `attempts=2` |
| re-running is silent | second run: `new=0`, nothing queued, nothing sent |
| `--dry-run` writes nothing | 0 rows in `pap.item` afterwards |
| failures are visible | `--boom` → exit 1, `status='failed'`, traceback stored |
| Google auth chain | refresh token → access token → Drive API → folder created; token cached |
| backup + restore | dump, restore into a throwaway DB, 10 rows across 12 tables (run in-container) |

Not yet exercised: GlitchTip and Healthchecks (no DSN/ping key configured yet), and the email sink.

## Testing conventions

Tests are **pure logic — no DB, no network, no real `.env`** (`test_config.py` passes a nonexistent
dotenv path so a developer's real file can never leak into a run). Test data is built from a `BASE` dict
plus an `_item(**over)` / `_settings(env, **extra)` override helper, matching `etl_metas`.

## Deploy

```bash
sudo cp deploy/*.service deploy/*.timer /etc/systemd/system/
sudo mkdir -p /etc/systemd/system/docker.service.d
sudo cp deploy/docker-after-tailscaled.conf /etc/systemd/system/docker.service.d/
sudo systemctl daemon-reload
sudo systemctl enable --now pap-dispatch.timer pap-backup.timer pap-backup-verify.timer
sudo systemctl enable --now pap-run@studeo.timer      # once Phase 1 exists
```

Exit codes: `0` completed, `1` the work failed, `2` configuration error (nothing attempted).
