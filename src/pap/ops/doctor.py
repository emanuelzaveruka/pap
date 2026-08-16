"""``pap doctor`` — is this install actually configured, and what is switched off?

**This command never prints a configuration value.** It reports names, and
present/absent, and what each gap disables. That rule is the whole point: it must
be safe to paste the output into a chat, a ticket, or this conversation while
`.env` itself stays unreadable to Claude Code.

It answers the question that otherwise costs an hour: *why did nothing happen?*
Usually because a feature is quietly disabled rather than broken.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass

import psycopg

from ..config import Settings
from ..db import SCHEMA, connect, connection_hint

OK = "ok"
MISSING = "missing"
DISABLED = "disabled"


@dataclass(frozen=True)
class Check:
    area: str
    name: str
    state: str
    detail: str = ""

    @property
    def symbol(self) -> str:
        return {OK: "+", MISSING: "!", DISABLED: "-"}.get(self.state, "?")


def _present(value: str) -> str:
    return OK if value else MISSING


def collect_checks(settings: Settings, *, check_db: bool = True) -> list[Check]:
    checks: list[Check] = []

    # -- database ----------------------------------------------------------
    checks.append(Check("database", "PG_HOST/PG_DB/PG_USER", OK, settings.conninfo_safe))
    checks.append(Check("database", "PG_PASSWORD", _present(settings.pg_password)))
    if check_db:
        checks.append(_check_database(settings))

    # -- the .env file itself ----------------------------------------------
    checks.append(_check_dotenv_line_endings())

    # -- state dir ---------------------------------------------------------
    checks.extend(_check_state_dir(settings.state_dir))

    # -- studeo -------------------------------------------------------------
    studeo_user = os.environ.get("STUDEO_USERNAME", "").strip()
    studeo_pass = os.environ.get("STUDEO_PASSWORD", "").strip()
    studeo_token = os.environ.get("STUDEO_TOKEN", "").strip()
    if not (studeo_user or studeo_pass or studeo_token):
        checks.append(Check("studeo", "credentials", DISABLED, "the studeo source cannot run"))
    else:
        checks.append(_check_studeo_username(studeo_user))
        checks.append(Check("studeo", "STUDEO_PASSWORD", _present(studeo_pass)))

    # -- notification channels --------------------------------------------
    tg = settings.telegram
    if not tg.enabled:
        checks.append(Check("telegram", "TELEGRAM_ENABLED", DISABLED, "no Telegram messages"))
    else:
        checks.append(Check("telegram", "TELEGRAM_BOT_TOKEN", _present(tg.bot_token)))
        checks.append(_check_telegram_chat_id(tg.chat_id))

    email = settings.email
    if not email.enabled:
        checks.append(Check("email", "EMAIL_ENABLED", DISABLED, "no email notifications"))
    else:
        checks.append(Check("email", "SMTP_HOST", _present(email.host)))
        checks.append(Check("email", "SMTP_USER", _present(email.user)))
        checks.append(Check("email", "SMTP_PASSWORD", _present(email.password)))
        checks.append(Check("email", "EMAIL_FROM", _present(email.sender)))
        checks.append(Check("email", "EMAIL_TO", _present(", ".join(email.recipients) if email.recipients else ""),
                            f"{len(email.recipients)} recipient(s)"))

    if not tg.configured and not email.configured:
        checks.append(Check(
            "notifications", "any channel", MISSING,
            "nothing can be delivered — items will be collected and queued but never sent",
        ))

    # -- observability -----------------------------------------------------
    obs = settings.observability
    checks.append(Check(
        "observability", "SENTRY_DSN",
        OK if obs.sentry_configured else DISABLED,
        f"release={obs.release} environment={obs.environment}" if obs.sentry_configured
        else "crashes will not be reported anywhere",
    ))
    checks.append(Check(
        "observability", "HEALTHCHECKS_PING_KEY",
        OK if obs.healthchecks_configured else DISABLED,
        f"base={obs.healthchecks_base_url}" if obs.healthchecks_configured
        else "a job that stops running will go unnoticed",
    ))

    # -- google ------------------------------------------------------------
    google = settings.google
    if not google.enabled:
        checks.append(Check("google", "GOOGLE_ENABLED", DISABLED,
                            "no Calendar sync, no Drive uploads, no off-machine backups"))
    elif google.uses_env_credentials:
        checks.append(Check("google", "GOOGLE_CLIENT_ID", _present(google.client_id)))
        checks.append(Check("google", "GOOGLE_CLIENT_SECRET", _present(google.client_secret)))
        checks.append(Check("google", "GOOGLE_REFRESH_TOKEN", _present(google.refresh_token),
                            "credentials come from the environment — no token file needed"))
    elif google.token_file:
        exists = os.path.exists(google.token_file)
        checks.append(Check(
            "google", "GOOGLE_TOKEN_FILE",
            OK if exists else MISSING,
            f"{google.token_file} (legacy file-based flow)" if exists
            else f"{google.token_file} — not found",
        ))
    else:
        checks.append(Check(
            "google", "credentials", MISSING,
            "set GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / GOOGLE_REFRESH_TOKEN — run "
            "`pap auth --login` on a machine with a browser to obtain them",
        ))

    # -- backups -----------------------------------------------------------
    backup = settings.backup
    if not backup.enabled:
        checks.append(Check("backup", "BACKUP_ENABLED", DISABLED,
                            "the database is not being backed up"))
    else:
        checks.append(Check(
            "backup", "BACKUP_DIR",
            OK if os.path.isdir(backup.directory) else MISSING,
            f"{backup.directory} (keep {backup.keep_daily} daily, {backup.keep_weekly} weekly)",
        ))
        if not google.configured:
            checks.append(Check(
                "backup", "off-machine copy", MISSING,
                "Google is not configured, so dumps stay on the same disk as the database — "
                "which defeats the point if that disk is the thing that fails",
            ))

    return checks


def _check_state_dir(state_dir: str) -> list[Check]:
    """Is the state directory writable, and does it actually honour 0600?

    The second question matters more than it looks. The credential store chmods
    every token file to 0600, but a filesystem is free to ignore that — notably
    WSL's DrvFs, which is what you get under ``/mnt/c``. There the call succeeds,
    the mode stays 0777, and a Google refresh token sits world-readable while the
    code and the tests both believe it is protected. Silence is the dangerous
    part, so it is probed rather than assumed.
    """
    if not os.path.isdir(state_dir):
        return [Check("runtime", "state dir", MISSING,
                      f"{state_dir} does not exist — tokens cannot be cached")]
    if not os.access(state_dir, os.W_OK):
        return [Check("runtime", "state dir", MISSING,
                      f"{state_dir} is not writable — tokens cannot be cached")]

    checks = [Check("runtime", "state dir", OK, state_dir)]

    probe = os.path.join(state_dir, ".pap-permission-probe")
    try:
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("")
        os.chmod(probe, 0o600)
        mode = stat.S_IMODE(os.stat(probe).st_mode)
    except OSError as exc:
        return checks + [Check("runtime", "state dir permissions", MISSING,
                               f"could not verify file permissions ({exc})")]
    finally:
        try:
            os.unlink(probe)
        except OSError:
            pass

    if mode == 0o600:
        return checks + [Check("runtime", "state dir permissions", OK, "0600 enforced")]
    return checks + [Check(
        "runtime", "state dir permissions", MISSING,
        f"this filesystem ignores chmod (a 0600 file reads back as {mode:04o}). "
        f"Stored credentials are NOT protected here — typical for /mnt/c under WSL. "
        f"Fine for local testing; on the server keep the state directory on ext4 "
        f"(a Docker volume), never a Windows mount.",
    )]


def _mask(value: str) -> str:
    """Digits become `#`. Lets a report describe a malformed value's *shape*
    without printing it — doctor never prints a configuration value."""
    return "".join("#" if ch.isdigit() else ch for ch in value)


def _check_studeo_username(username: str) -> Check:
    """Validate the *shape* of the RA without calling Studeo.

    Studeo answers a plain ``401`` to a correctly-numbered RA in the wrong
    format, which is indistinguishable from a wrong password — and sends you
    looking at the password, which is fine. This actually happened: `7654321-89`
    instead of `12345678-9`. Same nine digits, hyphen one position to the left,
    401 on every request for five days.
    """
    if not username:
        return Check("studeo", "STUDEO_USERNAME", MISSING)

    digits = "".join(ch for ch in username if ch.isdigit())
    if re.fullmatch(r"\d{8}-\d", username):
        return Check("studeo", "STUDEO_USERNAME", OK)

    if len(digits) == 9:
        return Check(
            "studeo", "STUDEO_USERNAME", MISSING,
            f"the nine digits are right but the format is not: expected "
            f"########-# (e.g. 12345678-9), got {_mask(username)}. Studeo answers "
            f"401 for this, which looks exactly like a wrong password.",
        )
    return Check(
        "studeo", "STUDEO_USERNAME", MISSING,
        f"expected an RA shaped ########-# (e.g. 12345678-9), got {_mask(username)}",
    )


def _check_telegram_chat_id(chat_id: str) -> Check:
    """Validate the *shape* of the chat id without calling Telegram.

    Telegram accepts a numeric id (negative for groups) or ``@publicchannel``.
    Anything else — most often the bot's own username, copied from BotFather —
    fails at send time with ``400: chat not found``, which names neither the
    variable nor the reason.
    """
    if not chat_id:
        return Check("telegram", "TELEGRAM_CHAT_ID", MISSING)
    if chat_id.lstrip("-").isdigit():
        return Check("telegram", "TELEGRAM_CHAT_ID", OK)
    if chat_id.startswith("@"):
        return Check("telegram", "TELEGRAM_CHAT_ID", OK, "public channel")

    extra = (
        " That looks like your bot's username — a bot cannot message itself."
        if chat_id.endswith("_bot") else ""
    )
    return Check(
        "telegram", "TELEGRAM_CHAT_ID", MISSING,
        f"must be a numeric chat id (or @publicchannel), not a username.{extra} "
        f"Message your bot first, then read the id from "
        f"https://api.telegram.org/bot<TOKEN>/getUpdates",
    )


def _check_dotenv_line_endings() -> Check:
    """Warn when .env has Windows line endings.

    The app itself is fine — python-dotenv strips the carriage return and
    ``config._get`` strips again. The damage is done by anything *else* that reads
    the file: `source .env` in a shell keeps the ``\\r`` and silently appends it to
    every value, so a password or API token differs from what the app uses by one
    invisible character and authentication fails with no visible cause.

    Only line endings are inspected — no value is read or reported.
    """
    try:
        from dotenv import find_dotenv
    except ImportError:  # pragma: no cover
        return Check("runtime", ".env line endings", OK, "python-dotenv not installed")

    path = find_dotenv(usecwd=True)
    if not path or not os.path.exists(path):
        return Check("runtime", ".env", DISABLED,
                     "no .env found — configuration comes from the environment")
    try:
        with open(path, "rb") as fh:
            crlf = b"\r\n" in fh.read()
    except OSError as exc:
        return Check("runtime", ".env", MISSING, f"{path} is unreadable ({exc})")

    if not crlf:
        return Check("runtime", ".env", OK, path)
    return Check(
        "runtime", ".env line endings", MISSING,
        f"{path} has Windows (CRLF) line endings. This app copes, but anything that "
        f"shell-sources the file will append a carriage return to every value and "
        f"authentication will fail for no visible reason. Fix with:  "
        f"sed -i 's/\\r$//' {path}",
    )


def _check_database(settings: Settings) -> Check:
    try:
        with connect(settings) as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT count(*) AS n FROM information_schema.tables WHERE table_schema = '{SCHEMA}'"
            )
            tables = cur.fetchone()["n"]
    except psycopg.Error as exc:
        detail = f"{type(exc).__name__}: {' '.join(str(exc).split())[:200]}"
        if hint := connection_hint(settings, exc):
            # Indented so the multi-line fix stays visually attached to its check
            # rather than reading as a separate section of the report.
            detail += "\n" + "\n".join(f"          {line}" for line in hint.splitlines())
        return Check("database", "connection", MISSING, detail)
    if tables == 0:
        return Check("database", "connection", MISSING,
                     f"connected, but schema '{SCHEMA}' is empty — run `pap migrate`")
    return Check("database", "connection", OK, f"connected, {tables} table(s) in '{SCHEMA}'")


def render(checks: list[Check]) -> str:
    lines = ["Configuration report (values are never printed — only names and status)", ""]
    area = None
    for check in checks:
        if check.area != area:
            area = check.area
            lines.append(f"  {area}")
        detail = f"  — {check.detail}" if check.detail else ""
        lines.append(f"    [{check.symbol}] {check.name}: {check.state}{detail}")

    missing = [c for c in checks if c.state == MISSING]
    disabled = [c for c in checks if c.state == DISABLED]
    lines.append("")
    lines.append(f"{len(missing)} problem(s), {len(disabled)} feature(s) switched off.")
    if missing:
        lines.append("Problems:")
        lines.extend(f"  - {c.area}/{c.name}" for c in missing)
    return "\n".join(lines)


def run_doctor(settings: Settings, *, check_db: bool = True) -> int:
    checks = collect_checks(settings, check_db=check_db)
    print(render(checks))
    return 1 if any(c.state == MISSING for c in checks) else 0
