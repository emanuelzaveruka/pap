"""``pap doctor --live`` — do the credentials actually work?

``pap doctor`` answers a cheaper question: *is the configuration present and
well-formed?* It reads names and file states and never touches the network. That
catches typos and missing values, but it cannot catch the failures that actually
bite: a rotated password, an expired refresh token, a Studeo account locked after
a semester break, an LLM key that was revoked.

This module answers the expensive question — **does each credential still
authenticate right now** — by performing the cheapest real call each service
offers:

===============  =========================================================
Postgres         connect and ``SELECT 1``
Studeo           log in; report the token's remaining lifetime
Google           refresh the access token, then one Drive ``about`` call
Telegram         ``getMe`` (identifies the bot, sends no message)
SMTP             connect, STARTTLS, log in, ``QUIT`` — **never sends mail**
LLM providers    the smallest possible completion, capped at a few tokens
===============  =========================================================

Two rules hold here exactly as they do in ``doctor``:

**No value is ever printed.** Results are names, states and durations. The output
stays safe to paste into a chat or an issue.

**A skipped check is not a pass.** Anything unconfigured reports ``disabled``
rather than silently counting as healthy — the whole point is to distinguish "this
works" from "this was never switched on".

Costs are deliberate: every LLM probe is a handful of tokens, and the SMTP probe
authenticates without delivering anything.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
import time
from datetime import datetime, timezone

import psycopg
import requests

from ..config import Settings
from ..core.secrets import human_duration
from ..db import conninfo, connection_hint
from .doctor import DISABLED, MISSING, OK, Check, render

log = logging.getLogger(__name__)

# Live probes are diagnostics, not work: they must fail fast rather than make the
# person wait on a hung service to be told it is hung.
PROBE_TIMEOUT = 15


def _timed(func) -> tuple[bool, str, float]:
    """Run a probe, returning ``(ok, detail, elapsed_ms)``.

    Every probe funnels through here so one misbehaving service cannot abort the
    rest of the report — a verification run that stops at the first failure is
    much less useful than one that tells you the state of everything.
    """
    started = time.monotonic()
    try:
        detail = func() or "ok"
        ok = True
    except Exception as exc:  # noqa: BLE001 - a failed probe is a result, not a crash
        detail = f"{type(exc).__name__}: {' '.join(str(exc).split())[:180]}"
        ok = False
    return ok, detail, (time.monotonic() - started) * 1000


def _check(area: str, name: str, func) -> Check:
    ok, detail, elapsed = _timed(func)
    return Check(area, name, OK if ok else MISSING, f"{detail} [{elapsed:.0f}ms]")


# -- individual probes ------------------------------------------------------
def _probe_postgres(settings: Settings) -> str:
    with psycopg.connect(conninfo(settings), connect_timeout=PROBE_TIMEOUT) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT version()")
            version = cur.fetchone()[0].split(",")[0]
    return f"authenticated — {version}"


def _probe_studeo(settings: Settings) -> str:
    """Log in and report how long the resulting token is good for.

    Reports the *lifetime* rather than the token, which is the operationally
    useful part: it tells you how often the platform must re-authenticate.
    """
    from ..sources.studeo import STUDEO_TZ, StudeoSource, jwt_expiry

    source = StudeoSource(settings)
    try:
        token = source.login()
    finally:
        source.close()

    expiry = jwt_expiry(token)
    if expiry is None:
        return "login succeeded (token carries no readable expiry)"
    remaining = expiry - datetime.now(timezone.utc)
    local = expiry.astimezone(STUDEO_TZ).strftime("%Y-%m-%d %H:%M:%S")
    return (f"login succeeded — token valid for "
            f"{human_duration(remaining.total_seconds())} (until {local})")


def _probe_google(settings: Settings) -> str:
    """Refresh the access token, then make the cheapest authenticated call.

    The refresh alone is not sufficient evidence: a token can refresh while the
    Drive scope has been revoked, so this also asks Drive who it thinks we are.
    """
    from googleapiclient.discovery import build

    from ..sinks.google_auth import load_credentials

    credentials = load_credentials(settings.google, state_dir=settings.state_dir)
    service = build("drive", "v3", credentials=credentials, cache_discovery=False)
    about = service.about().get(fields="user(emailAddress),storageQuota(limit,usage)").execute()

    email = (about.get("user") or {}).get("emailAddress", "")
    # Only the domain is reported: the account is confirmed without printing the
    # address itself, which is personal data even though it is not a secret.
    domain = email.split("@")[-1] if "@" in email else "unknown"
    quota = about.get("storageQuota") or {}
    used = int(quota.get("usage") or 0) / (1024 ** 3)
    limit = quota.get("limit")
    space = f"{used:.1f} GiB used"
    if limit:
        space += f" of {int(limit) / (1024 ** 3):.0f} GiB"
    return f"token refreshed, Drive reachable — account @{domain}, {space}"


def _probe_telegram(settings: Settings) -> str:
    """``getMe`` — proves the bot token works without messaging anyone."""
    response = requests.get(
        f"https://api.telegram.org/bot{settings.telegram.bot_token}/getMe",
        timeout=PROBE_TIMEOUT,
    )
    if response.status_code != 200:
        # The token is in the URL, so report the status only — never the URL.
        raise RuntimeError(f"getMe returned {response.status_code}: {response.text[:120]}")
    bot = (response.json() or {}).get("result") or {}
    return f"bot @{bot.get('username')} authenticated"


def _probe_smtp(settings: Settings) -> str:
    """Authenticate and hang up. Deliberately sends nothing."""
    email = settings.email
    context = ssl.create_default_context()
    if email.port == 465:
        with smtplib.SMTP_SSL(email.host, email.port, timeout=PROBE_TIMEOUT,
                              context=context) as smtp:
            smtp.login(email.user, email.password)
    else:
        with smtplib.SMTP(email.host, email.port, timeout=PROBE_TIMEOUT) as smtp:
            smtp.ehlo()
            if email.use_tls:
                smtp.starttls(context=context)
                smtp.ehlo()
            smtp.login(email.user, email.password)
    return f"authenticated to {email.host} (no mail sent)"


def _probe_healthchecks(settings: Settings) -> str:
    url = settings.observability.ping_url("verify")
    response = requests.get(f"{url}/start", timeout=PROBE_TIMEOUT)
    if response.status_code >= 400:
        raise RuntimeError(f"ping returned {response.status_code}")
    return "ping accepted"


# -- assembly ---------------------------------------------------------------
def collect_live_checks(settings: Settings) -> list[Check]:
    checks: list[Check] = [_check("database", "authentication", lambda: _probe_postgres(settings))]

    from ..sources.studeo import StudeoSource

    studeo = StudeoSource(settings)
    studeo_reason = studeo.disabled_reason
    studeo.close()
    if studeo_reason:
        checks.append(Check("studeo", "login", DISABLED, "no credentials configured"))
    else:
        checks.append(_check("studeo", "login", lambda: _probe_studeo(settings)))

    if settings.google.configured:
        checks.append(_check("google", "token refresh + Drive", lambda: _probe_google(settings)))
    else:
        checks.append(Check("google", "credentials", DISABLED, "GOOGLE_ENABLED is false"))

    if settings.telegram.configured:
        checks.append(_check("telegram", "getMe", lambda: _probe_telegram(settings)))
    else:
        checks.append(Check("telegram", "bot", DISABLED, "not configured"))

    if settings.email.configured:
        checks.append(_check("email", "smtp login", lambda: _probe_smtp(settings)))
    else:
        checks.append(Check("email", "smtp", DISABLED, "not configured"))

    if settings.observability.healthchecks_configured:
        checks.append(_check("observability", "healthchecks ping",
                             lambda: _probe_healthchecks(settings)))
    else:
        checks.append(Check("observability", "healthchecks", DISABLED, "no ping key"))

    checks.extend(_llm_checks(settings))
    return checks


def _llm_checks(settings: Settings) -> list[Check]:
    """Probe each configured LLM provider with the smallest possible request."""
    from ..llm import registry as llm_registry

    checks: list[Check] = []
    for name in llm_registry.available():
        provider_settings = settings.llm.for_provider(name)
        if not provider_settings.configured:
            checks.append(Check("llm", name, DISABLED, f"{provider_settings.key_var} is not set"))
            continue

        def probe(provider_name: str = name) -> str:
            provider = llm_registry.build(provider_name, settings)
            result = provider.ping()
            return (f"{result.model} answered — "
                    f"{result.input_tokens}+{result.output_tokens} tokens")

        checks.append(_check("llm", name, probe))
    return checks


def run_verify(settings: Settings) -> int:
    """Print the live report. Returns 1 if any configured credential failed."""
    print("Verifying credentials by using them. No value is ever printed.\n")
    checks = collect_live_checks(settings)
    print(render(checks))

    failed = [c for c in checks if c.state == MISSING]
    if failed:
        print()
        print("A failing credential here is a real outage: the job that needs it will "
              "fail the next time its timer fires.")
    return 1 if failed else 0
