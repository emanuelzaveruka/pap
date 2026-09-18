# Personal Automation Platform

A containerized automation platform that watches the sources I actually depend on, keeps their state
in PostgreSQL, and turns *genuinely new* information into notifications and documents — without ever
telling me the same thing twice.

It runs unattended on a headless home server as oneshot Docker containers fired by systemd timers.

> **Why this repository exists.** It is a personal system, published as a portfolio piece. It is not a
> product and takes no contributions. The parts worth reading are the architectural constraints in
> [`CLAUDE.md`](CLAUDE.md) and the failure modes recorded in the tests — most of them are bugs that
> actually happened in production, kept as regressions.

---

## The problem

University portals, newsletters and job boards all have the same shape: information I need, published
on their schedule, buried in an interface that does not notify me, and impossible to tell apart from
what I already read yesterday. Polling them by hand is the failure mode — I stop, and then I miss a
deadline.

So the hard part is not scraping. It is **deciding what is new**, reliably enough that I trust the
notifications enough to stop checking manually. Everything in the design serves that.

## The pipeline

One path, always the same, wrapped in a run ledger:

```
collect → hash → upsert → enqueue → deliver
```

`runner.run_source()` is the only place that knows how those fit together. A source adapter never
sends anything and never decides what is new — it returns items and stops. That separation is what
makes `--dry-run` honest: it genuinely cannot write or notify.

**Idempotency lives in the schema, not the code.** `UNIQUE (source, external_id)` on items,
`UNIQUE (dedupe_key)` on notifications. The constraint violation *is* the dedupe. A
SELECT-then-INSERT would reintroduce exactly the race the constraint exists to prevent.

**The content hash covers only meaningful fields.** Fetch timestamps, session ids and tracking
parameters are excluded — include one and every run looks like a change, and the system re-notifies
you forever until you stop trusting it.

## Three ports

The whole design is three interfaces and a rule: adding a capability must cost one file.

| Port | Contract | Implementations |
|---|---|---|
| `core/registry.py` | `SourceAdapter.collect()` | Studeo (university portal), a fake source for tests |
| `sinks/base.py` | `Sink.send()` | Telegram, SMTP email, Google Calendar, Google Drive |
| `llm/base.py` | `LLMProvider.complete()` / `count_tokens()` | Claude, OpenAI, Gemini |

Adding a source costs one file: implement `collect()`, decorate with `@register`, import it. **If a
new source required changes anywhere else, the abstraction would be wrong.**

The LLM port matters most. The domain services — document generation, study resumes — depend only on
the interface and never learn which vendor answered. The provider resolves *per task*
(`LLM_PROVIDER_BOOK_RESUME`, `LLM_PROVIDER_DELIVERABLE`, …) with a CLI override, and every call is
recorded to `pap.llm_call` in a `finally` block, so **a failed call is still measured**. `pap llm
usage` reads that table: provider comparisons come from measurements rather than from guesses about
pricing.

## Observability is not notification

Deliberately three separate things, which is a distinction that keeps getting collapsed in systems
like this:

- **Notifications** (Telegram) — a product feature, for me as a user.
- **Error tracking** (GlitchTip) — *what broke, where, why*.
- **Liveness** (Healthchecks) — *did it run at all*.

The third is not redundant with the second: **a job that never fires raises no exception**, so error
tracking is structurally blind to it. Neither shares code or state with the notification dispatcher.

Secrets are scrubbed centrally rather than by trusting callers — every registered secret value is
replaced throughout the outgoing event payload, because a password reaches an error report through a
connection string in an exception message far more often than through any field you could allowlist.

## What is built

| Phase | Scope | State |
|---|---|---|
| 0 | Foundation: pipeline, schema, dispatcher, Telegram/email, backups, observability | ✅ verified end to end |
| 1 | University portal: agenda, deadlines → Google Calendar, book archive → Drive | ✅ verified against the live API |
| 2 | Vendor-neutral LLM port with per-task provider selection and usage accounting | ✅ three adapters |
| 3 | Deliverable generation into the institution's own `.docx` template | ✅ |
| 4 | Serial study-resume feed from archived books | ✅ |

Proven in practice, not just in tests: a delivery failure loses nothing (retry delivered,
`attempts=2`); a second run is silent (`new=0`); a moved deadline **patches** its calendar event
rather than creating a second one, and a re-run makes *zero* API calls; a dump is restored into a
throwaway database nightly, because a backup nobody has restored is a hypothesis.

## Design constraints worth reading

The most useful thing in this repository is probably [`CLAUDE.md`](CLAUDE.md) — roughly forty
non-obvious constraints, each recording a failure that was expensive to diagnose. A sample:

- **A partial download must never be mistaken for a finished book.** The CDN answers HTTP 200 with a
  short error page. Bytes land in `*.part` and are renamed only after clearing a size floor —
  otherwise a corrupt PDF reaches Drive and fails much later, in the chunker, far from its cause.
- **Deadlines must be timezone-aware before reaching Calendar.** A naive value gets interpreted in the
  calendar's own zone, silently shifting every deadline when the server runs UTC and the college does
  not. `event_body` raises rather than guessing.
- **Docker publishes ports ahead of `ufw`.** A bare `5432:5432` exposes Postgres on every interface
  regardless of firewall rules, because Docker writes to the NAT chain first.
- **Telegram rejects messages over 4096 characters.** Splitting happens on paragraph → line → word
  boundaries, with a `.md` document as the fallback. HTML parse mode, not Markdown: course material
  contains unbalanced `*` and `_` constantly, and one parse failure rejects the entire message.
- **`pap doctor` must never print a configuration value** — only names and present/absent. That is
  what makes its output safe to paste into an issue.

## Running it

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt && pip install -e .   # editable install is required (src layout)

cp .env.example .env    # then fill it in
pap doctor              # reports names and present/absent — never values
pap migrate

pap run studeo --dry-run   # collect and report; writes nothing, sends nothing
pap run studeo
```

In Docker, which is how it actually runs:

```bash
docker compose up -d                                   # postgres + observability stack
docker compose --profile cli run --rm pap migrate
docker compose --profile cli run --rm pap run studeo
```

The `pap` service sits behind the `cli` profile deliberately, so `compose up -d` never daemonises it.

Exit codes: `0` completed · `1` the work failed · `2` configuration error, nothing attempted.

**Diagnosing a job that "stopped working" starts with `pap doctor --live`**, which authenticates for
real against every dependency. An expired credential looks exactly like a bug, and costs a day if you
assume it is one.

## Tests

```bash
pytest        # 348 tests, pure logic — no database, no network, no real .env
```

Two conventions are load-bearing:

- **`tests/test_archives.py` is a regression file.** Every test corresponds to a bug that actually
  happened against the live API: a path separator inside a filename field, an error page served with
  HTTP 200, a server ignoring `Range` and answering 200 instead of 206. New production failures earn a
  test here, named after the behaviour rather than the function.
- **`tests/test_llm.py` guards the abstraction, not the vendors.** It protects two properties: that
  provider choice is data rather than code, and that the Claude adapter cannot emit a request shape
  the current API rejects. It needs no API keys and makes no calls.

## Stack

Python 3.12 · PostgreSQL 17 · Docker Compose · systemd timers · GlitchTip · Healthchecks ·
Google Drive & Calendar APIs · Telegram Bot API · Anthropic / OpenAI / Google LLM SDKs

## A note on the portal integration

The university source reads **only my own enrolment, with my own credentials**, at a deliberately
low request rate, and identifies itself honestly in its user agent. It replaces me opening the same
pages by hand.

[`docs/studeo-api.md`](docs/studeo-api.md) records the *design consequences* of that integration —
the debugging that shaped the adapter — rather than a map of a third party's private API. The
endpoint inventory and capture procedure were removed before this repository was made public.

No credentials, tokens or personal identifiers are committed. `.env` is git-ignored, and the tooling
is built so that no command prints a configuration value.
