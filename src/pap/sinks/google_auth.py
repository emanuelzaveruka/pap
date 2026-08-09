"""Google OAuth for a headless server, driven from environment variables.

Google's non-interactive refresh needs exactly three durable values —
``client_id``, ``client_secret`` and ``refresh_token`` — plus the token endpoint.
All four are ordinary strings, so they live in ``pap.env`` with every other
secret. **No token file has to be copied to the server**, which matters because
`.env` is the single place credentials are meant to exist and a headless box has
no browser to re-consent with.

What still cannot happen on the server is *obtaining* that refresh token: the
consent screen needs a browser. So:

1. On a machine with a browser: ``pap auth --login``. It runs the consent flow and
   **prints the three values** ready to paste into ``pap.env``.
2. Paste them into ``/etc/pap/pap.env`` on the server (mode 0600).
3. Every run from then on refreshes non-interactively.

The consent flow itself needs only ``GOOGLE_CLIENT_ID`` and
``GOOGLE_CLIENT_SECRET``: a Desktop client's downloaded JSON holds nothing else
that isn't a fixed Google endpoint, so that file need not be kept at all.
``GOOGLE_CLIENT_SECRETS_FILE`` is still accepted for anyone who prefers it.

The **access** token is different — it is short lived and disposable, so it is
cached in the state directory rather than in config. Losing that cache costs one
HTTP call; losing a refresh token costs a trip to a browser. The cache is keyed by
a fingerprint of client id + refresh token, so rotating either one invalidates it
automatically instead of handing back a token for the wrong account.

A ``GOOGLE_TOKEN_FILE`` is still honoured as a fallback for anyone who prefers the
file-based flow, but the environment path wins when both are present.

Google client libraries are imported lazily so the rest of the platform works
without them installed.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from ..config import GoogleSettings
from ..core.secrets import SecretStore, account_fingerprint, write_atomic

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/calendar.events",
]

ACCESS_TOKEN_STORE = "google_access_token"
# Refresh a little early rather than racing the expiry and failing a long upload
# partway through.
EXPIRY_SKEW = timedelta(minutes=5)


class GoogleAuthError(RuntimeError):
    """Credentials are missing, unusable, or need interactive consent."""


def _fingerprint(settings: GoogleSettings) -> str:
    return account_fingerprint(settings.client_id, settings.refresh_token)


def naive_utc_now() -> datetime:
    """Current UTC time without a tzinfo.

    google-auth stores and compares ``Credentials.expiry`` as naive UTC, so every
    expiry value we hand it or compare against has to be in the same form —
    mixing aware and naive datetimes raises TypeError inside the library rather
    than simply treating the token as expired.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def load_credentials(settings: GoogleSettings, *, state_dir: str | None = None):
    """Return usable credentials, refreshing them if needed.

    Prefers environment credentials; falls back to ``GOOGLE_TOKEN_FILE``. Raises
    ``GoogleAuthError`` when consent has never been given or the refresh token was
    revoked — both need a human at a browser, so failing loudly beats retrying.
    """
    if settings.uses_env_credentials:
        return _from_env(settings, state_dir=state_dir)
    return _from_file(settings)


# -- environment path (preferred) -------------------------------------------
def _from_env(settings: GoogleSettings, *, state_dir: str | None):
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError as exc:  # pragma: no cover - depends on optional install
        raise GoogleAuthError("google-auth is not installed") from exc

    store = _access_token_store(settings, state_dir)
    cached_token, cached_expiry = _cached_access_token(store)

    credentials = Credentials(
        token=cached_token,
        refresh_token=settings.refresh_token,
        client_id=settings.client_id,
        client_secret=settings.client_secret,
        token_uri=settings.token_uri,
        scopes=SCOPES,
        expiry=cached_expiry,
    )
    if credentials.valid:
        return credentials

    log.info("refreshing the Google access token")
    try:
        credentials.refresh(Request())
    except Exception as exc:  # noqa: BLE001 - surfaced as a config-level failure
        raise GoogleAuthError(
            f"could not refresh the Google access token ({type(exc).__name__}: {exc}). "
            f"If the refresh token was revoked or the consent was removed, run "
            f"`pap auth --login` again on a machine with a browser."
        ) from exc

    _cache_access_token(store, credentials)
    return credentials


def _access_token_store(settings: GoogleSettings, state_dir: str | None) -> SecretStore | None:
    if not state_dir:
        return None
    return SecretStore(state_dir, ACCESS_TOKEN_STORE, fingerprint=_fingerprint(settings))


def _cached_access_token(store: SecretStore | None) -> tuple[str | None, datetime | None]:
    if store is None:
        return None, None
    stored = store.load()
    if stored is None:
        return None, None
    token = stored.data.get("access_token")
    raw_expiry = stored.data.get("expiry")
    if not token or not raw_expiry:
        return None, None
    try:
        expiry = datetime.fromisoformat(raw_expiry)
    except ValueError:
        return None, None
    # google-auth compares expiry against naive UTC, so a tz-aware value here
    # raises TypeError deep inside the library rather than simply expiring.
    if expiry.tzinfo is not None:
        expiry = expiry.astimezone(timezone.utc).replace(tzinfo=None)
    if expiry - EXPIRY_SKEW <= naive_utc_now():
        return None, None
    return token, expiry


def _cache_access_token(store: SecretStore | None, credentials) -> None:
    if store is None or not credentials.token or not credentials.expiry:
        return
    store.save({
        "access_token": credentials.token,
        "expiry": credentials.expiry.isoformat(),
    })


# -- file path (fallback) ----------------------------------------------------
def _from_file(settings: GoogleSettings):
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError as exc:  # pragma: no cover
        raise GoogleAuthError("google-auth is not installed") from exc

    token_file = settings.token_file
    if not token_file or not os.path.exists(token_file):
        raise GoogleAuthError(
            "no Google credentials. Set GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET and "
            "GOOGLE_REFRESH_TOKEN in your .env — run `pap auth --login` on a machine "
            "with a browser to obtain them."
        )

    credentials = Credentials.from_authorized_user_file(token_file, SCOPES)
    if credentials.valid:
        return credentials
    if credentials.expired and credentials.refresh_token:
        log.info("refreshing the Google access token")
        credentials.refresh(Request())
        write_atomic(token_file, _as_dict(credentials))
        return credentials

    raise GoogleAuthError(
        "the stored Google credentials cannot be refreshed (revoked, or consented "
        "without offline access). Run `pap auth --login` again."
    )


# -- interactive consent -----------------------------------------------------
def interactive_login(
    settings: GoogleSettings,
    *,
    write_file: bool = False,
    port: int = 0,
    open_browser: bool = True,
) -> dict[str, str]:
    """Run the consent flow. Returns the values to put in ``.env``.

    Needs a browser reachable from THIS machine, so it runs on your workstation,
    never on the server.

    Two knobs exist for WSL, where the default behaviour is unreliable:

    ``port``          A fixed port instead of a random one. WSL2 forwards
                      ``localhost`` from Windows into the distro, so the redirect
                      does come back — but a fixed port is far easier to
                      troubleshoot, and lets you allowlist it once in the Google
                      Cloud console if you ever switch to a Web client.
    ``open_browser``  Disable the automatic launch. From WSL, Python's
                      ``webbrowser`` often fails silently or opens nothing when
                      ``wslu`` is absent, leaving the flow apparently hung. With
                      this off it prints the URL for you to paste into Windows.
    """
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:  # pragma: no cover
        raise GoogleAuthError("google-auth-oauthlib is not installed") from exc

    flow = _build_flow(settings, InstalledAppFlow)
    # access_type=offline + prompt=consent is what actually returns a refresh
    # token. Without both, Google issues an access token only and the server would
    # be unable to renew anything once the first hour elapsed — the failure shows
    # up an hour later, not at login, which makes it easy to miss.
    credentials = flow.run_local_server(
        port=port,
        open_browser=open_browser,
        access_type="offline",
        prompt="consent",
        authorization_prompt_message=(
            "Open this URL in your browser to authorise:\n\n{url}\n"
        ),
    )

    if not credentials.refresh_token:
        raise GoogleAuthError(
            "Google returned no refresh token. Revoke this app's access at "
            "https://myaccount.google.com/permissions and run `pap auth --login` again."
        )

    if write_file and settings.token_file:
        write_atomic(settings.token_file, _as_dict(credentials))
        log.info("also stored a token file at %s (mode 0600)", settings.token_file)

    return {
        "GOOGLE_CLIENT_ID": credentials.client_id,
        "GOOGLE_CLIENT_SECRET": credentials.client_secret,
        "GOOGLE_REFRESH_TOKEN": credentials.refresh_token,
    }


def client_config(client_id: str, client_secret: str, token_uri: str) -> dict:
    """The ``client_secrets.json`` structure, built from two values.

    A Desktop client's JSON contains nothing that isn't already in
    ``GOOGLE_CLIENT_ID`` and ``GOOGLE_CLIENT_SECRET`` plus fixed Google endpoints,
    so downloading and storing that file is avoidable — which keeps every
    credential in ``.env`` where they belong instead of half in a file.
    """
    return {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": token_uri or "https://oauth2.googleapis.com/token",
            # Desktop clients redirect to a loopback address; the flow substitutes
            # the port it actually binds.
            "redirect_uris": ["http://localhost"],
        }
    }


def _build_flow(settings: GoogleSettings, flow_cls):
    """Prefer client id/secret from the environment; fall back to the JSON file."""
    if settings.client_id and settings.client_secret:
        log.debug("building the consent flow from GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET")
        return flow_cls.from_client_config(
            client_config(settings.client_id, settings.client_secret, settings.token_uri),
            SCOPES,
        )
    if settings.client_secrets_file and os.path.exists(settings.client_secrets_file):
        return flow_cls.from_client_secrets_file(settings.client_secrets_file, SCOPES)

    raise GoogleAuthError(
        "no OAuth client to authorise with. Either set GOOGLE_CLIENT_ID and "
        "GOOGLE_CLIENT_SECRET in your .env (copy them from the Desktop client in "
        "Google Cloud Console), or point GOOGLE_CLIENT_SECRETS_FILE at the "
        "downloaded JSON."
    )


def _as_dict(credentials) -> dict:
    import json

    # to_json() emits exactly the shape from_authorized_user_file() expects, so
    # round-tripping it keeps us compatible with the library's own format.
    return json.loads(credentials.to_json())
