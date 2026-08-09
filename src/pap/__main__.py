"""Entrypoint: ``python -m pap`` (and the ``pap`` console script).

Exit codes, consistent across every subcommand:

``0``  completed
``1``  the work failed (a scrape raised, a restore did not verify, a check failed)
``2``  configuration error — nothing was attempted

Commands that only inspect configuration (``doctor``, ``auth``) deliberately do
not open the database, so they still work on a half-installed system, which is
exactly when they are needed.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from . import observability
from .config import ConfigError, Settings, load_settings
from .core import registry, secrets
from .core.models import Notification
from .db import Database, MigrationError, connect, connection_hint
from .logging_setup import setup_logging
from .ops import backup as backup_ops
from .ops import doctor as doctor_ops
from .ops import status as status_ops
from .runner import run_source
from .sinks import dispatcher, gcalendar
from .sinks.google_auth import GoogleAuthError, interactive_login

from . import sources  # noqa: F401  (imported for adapter registration)

log = logging.getLogger(__name__)

MIGRATIONS_DIR = os.environ.get(
    "PAP_MIGRATIONS_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "migrations"),
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pap",
        description="Personal automation platform: scrape sources, keep local state, "
                    "notify, and file artifacts into Google Drive and Calendar.",
    )
    parser.add_argument("--env-file", default=None, help="Path to a .env file.")
    parser.add_argument("--log-level", default=None, help="Override LOG_LEVEL for this run.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_doctor = sub.add_parser(
        "doctor", help="Report configuration status. Never prints any value.")
    p_doctor.add_argument("--no-db", action="store_true",
                          help="Skip the database connectivity check.")

    p_migrate = sub.add_parser("migrate", help="Apply pending SQL migrations.")
    p_migrate.add_argument("--dry-run", action="store_true",
                           help="List what would be applied and change nothing.")

    sub.add_parser("sources", help="List registered source adapters.")

    p_run = sub.add_parser("run", help="Run one source end to end.")
    p_run.add_argument("source", help="Adapter name (see `pap sources`).")
    p_run.add_argument("--dry-run", action="store_true",
                       help="Collect and report, but write nothing and send nothing.")
    p_run.add_argument("--no-dispatch", action="store_true",
                       help="Queue notifications but do not deliver them in this run.")
    p_run.add_argument("--boom", action="store_true",
                       help="Fake source only: raise on purpose to verify error reporting.")
    p_run.add_argument("--count", type=int, default=3,
                       help="Fake source only: how many items to invent (default 3).")

    p_dispatch = sub.add_parser("dispatch", help="Deliver queued notifications.")
    p_dispatch.add_argument("--dry-run", action="store_true",
                            help="Report what would be sent without sending.")
    p_dispatch.add_argument("--limit", type=int, default=50,
                            help="Maximum notifications to deliver (default 50).")

    p_sync = sub.add_parser("sync", help="Push stored state to an external service.")
    p_sync.add_argument("target", choices=["calendar"],
                        help="calendar = push deadlines to Google Calendar.")
    p_sync.add_argument("--dry-run", action="store_true",
                        help="Report what would be created or patched, and call nothing.")
    p_sync.add_argument("--limit", type=int, default=500)

    p_status = sub.add_parser("status", help="Recent runs and the notification queue.")
    p_status.add_argument("--days", type=int, default=7, help="Window in days (default 7).")

    p_notify = sub.add_parser("notify", help="Send a one-off message (channel smoke test).")
    p_notify.add_argument("--channel", default="telegram", help="telegram | email")
    p_notify.add_argument("--title", default="pap test")
    p_notify.add_argument("--body", default="If you are reading this, the channel works.")

    p_backup = sub.add_parser("backup", help="Database dump, upload and verification.")
    p_backup.add_argument("action", choices=["db", "verify"],
                          help="db = dump and upload; verify = restore the newest dump.")
    p_backup.add_argument("--dry-run", action="store_true")

    p_auth = sub.add_parser("auth", help="Google credentials.")
    p_auth.add_argument("--status", action="store_true",
                        help="Report stored credential state without calling Google.")
    p_auth.add_argument("--login", action="store_true",
                        help="Run the consent flow and print the .env lines to paste. "
                             "Requires a browser on THIS machine.")
    p_auth.add_argument("--write-token-file", action="store_true",
                        help="Also write GOOGLE_TOKEN_FILE, for the legacy file-based flow.")
    p_auth.add_argument("--port", type=int, default=8765,
                        help="Local port for the OAuth redirect (default 8765; 0 = random).")
    p_auth.add_argument("--no-browser", action="store_true",
                        help="Print the URL instead of launching a browser. Use this on WSL, "
                             "where the automatic launch often does nothing.")

    return parser


def _with_db(settings: Settings, func) -> int:
    try:
        conn = connect(settings)
    except Exception as exc:  # noqa: BLE001 - reported as a config-level failure
        # psycopg errors span several lines; collapse them so the useful part is
        # not pushed off screen by a hint block underneath.
        detail = " ".join(str(exc).split()) or type(exc).__name__
        print(f"Could not connect to PostgreSQL ({settings.conninfo_safe}): {detail}",
              file=sys.stderr)
        if hint := connection_hint(settings, exc):
            print(f"\n{hint}", file=sys.stderr)
        return 2
    try:
        return func(Database(conn))
    finally:
        conn.close()


def _cmd_run(settings: Settings, args: argparse.Namespace) -> int:
    adapter_kwargs: dict = {}
    if args.source == "fake":
        adapter_kwargs = {"boom": args.boom, "count": args.count}
    elif args.boom:
        print("--boom is only meaningful for the fake source", file=sys.stderr)
        return 2

    def go(db: Database) -> int:
        return run_source(
            db, settings, args.source,
            dry_run=args.dry_run,
            dispatch=not args.no_dispatch,
            adapter_kwargs=adapter_kwargs,
        )

    try:
        return _with_db(settings, go)
    except KeyError as exc:
        print(exc, file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - already recorded and reported by source_run
        print(f"Run failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


def _cmd_migrate(settings: Settings, args: argparse.Namespace) -> int:
    def go(db: Database) -> int:
        try:
            applied = db.apply_migrations(MIGRATIONS_DIR, dry_run=args.dry_run)
        except MigrationError as exc:
            print(f"Migration error: {exc}", file=sys.stderr)
            return 1
        if not applied:
            print("Schema is up to date.")
        else:
            verb = "Would apply" if args.dry_run else "Applied"
            print(f"{verb} {len(applied)} migration(s):")
            for name in applied:
                print(f"  {name}")
        return 0

    return _with_db(settings, go)


def _cmd_auth(settings: Settings, args: argparse.Namespace) -> int:
    if args.login:
        try:
            values = interactive_login(
                settings.google,
                write_file=args.write_token_file,
                port=args.port,
                open_browser=not args.no_browser,
            )
        except GoogleAuthError as exc:
            print(f"Login failed: {exc}", file=sys.stderr)
            return 2

        # Printed rather than written, because the server reads credentials from
        # pap.env and has no browser to run this flow itself. This is the one place
        # the platform deliberately puts a secret on screen — it is the only way to
        # get it into .env, and it is your own credential on your own machine.
        print("\nConsent granted. Add these three lines to your .env "
              "(/etc/pap/pap.env on the server, mode 0600):\n")
        for key, value in values.items():
            print(f"{key}={value}")
        print("\nAlso set GOOGLE_ENABLED=true. Nothing needs to be copied as a file.")
        print("Treat the refresh token as a password: it grants access until you revoke it")
        print("at https://myaccount.google.com/permissions")
        return 0

    # Default to --status: report without calling Google.
    google = settings.google
    print("Google credentials")
    print(f"  GOOGLE_ENABLED     : {'true' if google.enabled else 'false'}")
    if google.uses_env_credentials:
        print("  source             : environment (GOOGLE_CLIENT_ID / _SECRET / _REFRESH_TOKEN)")
    elif google.token_file:
        exists = os.path.exists(google.token_file)
        print(f"  source             : token file {google.token_file} "
              f"({'present' if exists else 'MISSING'})")
    else:
        print("  source             : none — run `pap auth --login` on a machine with a browser")
    print(f"  usable             : {'yes' if google.configured else 'no'}")
    print()
    print("Cached access token (short lived; losing it costs one HTTP call)")
    print(secrets.SecretStore(settings.state_dir, "google_access_token").describe())
    return 0


def _cmd_notify(settings: Settings, args: argparse.Namespace) -> int:
    result = dispatcher.send_now(settings, Notification(
        dedupe_key=f"manual-test:{args.channel}",
        channel=args.channel,
        title=args.title,
        body=args.body,
    ))
    print(f"{args.channel}: {'ok' if result.ok else 'FAILED'} — {result.detail}")
    return 0 if result.ok else 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        settings = load_settings(dotenv_path=args.env_file)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    setup_logging(settings.log_dir, args.log_level or settings.log_level)
    observability.init_observability(settings, job=args.command)

    if args.command == "doctor":
        return doctor_ops.run_doctor(settings, check_db=not args.no_db)
    if args.command == "sources":
        for name in registry.available():
            print(name)
        return 0
    if args.command == "migrate":
        return _cmd_migrate(settings, args)
    if args.command == "run":
        return _cmd_run(settings, args)
    if args.command == "dispatch":
        def go(db: Database) -> int:
            sent, failed = dispatcher.drain(db, settings, limit=args.limit,
                                            dry_run=args.dry_run)
            print(f"sent={sent} failed={failed}")
            return 0
        return _with_db(settings, go)
    if args.command == "sync":
        def go(db: Database) -> int:
            outcome = gcalendar.sync_deadlines(db, settings, dry_run=args.dry_run,
                                               limit=args.limit)
            print(outcome)
            return 1 if outcome.failed else 0
        return _with_db(settings, go)
    if args.command == "status":
        return _with_db(settings, lambda db: status_ops.run_status(db, days=args.days))
    if args.command == "notify":
        return _cmd_notify(settings, args)
    if args.command == "backup":
        try:
            if args.action == "db":
                return backup_ops.run_backup(settings, dry_run=args.dry_run)
            return backup_ops.run_verify(settings)
        except backup_ops.BackupError as exc:
            print(f"Backup error: {exc}", file=sys.stderr)
            return 1
    if args.command == "auth":
        return _cmd_auth(settings, args)

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
