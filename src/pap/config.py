"""Configuration loaded and validated from environment variables.

Dev values come from ``.env`` (python-dotenv); production from compose's
``env_file:`` pointing at ``/etc/pap/pap.env`` (mode 0600). Missing *required*
values fail fast; missing *optional* ones disable the feature they belong to
rather than crashing, so a half-configured platform still runs everything it can.

Nothing here ever prints a value — ``pap doctor`` reports names and
present/absent only. The set of secret values is registered with
``observability`` at startup so they can be scrubbed out of error reports.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"Required environment variable {name!r} is not set")
    return value


def _get(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None or value.strip() == "" else value.strip()


def _get_int(name: str, default: int) -> int:
    raw = _get(name, str(default))
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"Environment variable {name!r} must be an integer, got {raw!r}") from exc


def _get_float(name: str, default: float) -> float:
    raw = _get(name, str(default))
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"Environment variable {name!r} must be a number, got {raw!r}") from exc


def _get_bool(name: str, default: bool) -> bool:
    raw = _get(name, "true" if default else "false").lower()
    return raw in ("1", "true", "yes", "on")


def _state_dir() -> str:
    """Where tokens and cooldown markers live (mode 0600).

    ``PAP_STATE_DIR`` wins; otherwise systemd's ``StateDirectory=`` export (it may
    list several paths, first one wins); otherwise ``LOG_DIR``, which is always
    writable. Note systemd exports ``STATE_DIRECTORY`` only to processes it starts,
    so a host running the units should also set ``PAP_STATE_DIR`` explicitly —
    otherwise manual runs keep a second, separate token cache.
    """
    explicit = _get("PAP_STATE_DIR", "")
    if explicit:
        return explicit
    from_systemd = os.environ.get("STATE_DIRECTORY", "").split(":")[0].strip()
    return from_systemd or _get("LOG_DIR", "./logs")


@dataclass(frozen=True)
class TelegramSettings:
    enabled: bool
    bot_token: str
    chat_id: str

    @property
    def configured(self) -> bool:
        return self.enabled and bool(self.bot_token) and bool(self.chat_id)


@dataclass(frozen=True)
class EmailSettings:
    enabled: bool
    host: str
    port: int
    use_tls: bool
    user: str
    password: str
    sender: str
    recipients: tuple[str, ...]

    @property
    def configured(self) -> bool:
        return self.enabled and bool(self.host) and bool(self.sender) and bool(self.recipients)


@dataclass(frozen=True)
class GoogleSettings:
    """Google credentials, from environment variables or (legacy) a token file.

    The environment path is preferred and is what the server uses: a refresh token
    is a durable string, so it belongs in ``pap.env`` alongside every other secret
    rather than in a file that has to be copied to a headless machine and kept in
    sync. The access token is short-lived and is cached on disk instead — losing
    that cache costs one HTTP call, losing a refresh token costs a trip to a
    browser.
    """

    enabled: bool
    client_id: str
    client_secret: str
    refresh_token: str
    token_uri: str
    client_secrets_file: str
    token_file: str
    drive_root_folder: str
    drive_backup_folder: str
    calendar_id: str

    @property
    def uses_env_credentials(self) -> bool:
        return bool(self.client_id and self.client_secret and self.refresh_token)

    @property
    def configured(self) -> bool:
        return self.enabled and (self.uses_env_credentials or bool(self.token_file))


@dataclass(frozen=True)
class ObservabilitySettings:
    sentry_dsn: str
    environment: str
    release: str
    healthchecks_base_url: str
    healthchecks_ping_key: str
    healthchecks_slug_prefix: str

    @property
    def sentry_configured(self) -> bool:
        return bool(self.sentry_dsn)

    @property
    def healthchecks_configured(self) -> bool:
        return bool(self.healthchecks_base_url) and bool(self.healthchecks_ping_key)

    def ping_url(self, job: str) -> str | None:
        """Slug-based ping URL for a job, or None when Healthchecks is off.

        Slug pinging auto-creates the check on first ping, so adding a new job
        needs no clicking around in the Healthchecks UI.
        """
        if not self.healthchecks_configured:
            return None
        slug = f"{self.healthchecks_slug_prefix}-{job}" if self.healthchecks_slug_prefix else job
        return f"{self.healthchecks_base_url.rstrip('/')}/ping/{self.healthchecks_ping_key}/{slug}"


@dataclass(frozen=True)
class ArchiveSettings:
    """Where downloaded course material lives, and how large it may get.

    The size cap matters on this server specifically: its disk has already reported
    that it is failing, so an unbounded download is a real risk rather than a
    theoretical one.
    """

    books_dir: str
    materials_dir: str
    deliverables_dir: str
    max_book_mb: int


@dataclass(frozen=True)
class LLMProviderSettings:
    """Credentials and model for one vendor behind the LLM port."""

    name: str
    key_var: str
    api_key: str
    model: str

    @property
    def configured(self) -> bool:
        return bool(self.api_key) and bool(self.model)


@dataclass(frozen=True)
class LLMSettings:
    """Which vendor answers which task.

    Provider is resolved **per purpose**, not globally, because the tasks have
    genuinely different shapes: a MAPA deliverable is long and format-constrained,
    a book resume wants a large context window, a classification is short and
    high-volume. One global choice would force the same compromise on all three.

    Resolution order is: explicit CLI ``--provider`` > ``LLM_PROVIDER_<PURPOSE>``
    > ``LLM_PROVIDER_DEFAULT``.
    """

    default_provider: str
    purposes: dict[str, str]
    providers: dict[str, LLMProviderSettings]
    max_tokens: int
    thinking: str
    effort: str

    def for_provider(self, name: str) -> LLMProviderSettings:
        try:
            return self.providers[name]
        except KeyError:
            known = ", ".join(sorted(self.providers))
            raise ConfigError(f"unknown LLM provider {name!r} (known: {known})") from None

    def provider_for(self, purpose: str, override: str | None = None) -> str:
        if override:
            return override
        return self.purposes.get(purpose) or self.default_provider

    @property
    def any_configured(self) -> bool:
        return any(p.configured for p in self.providers.values())


@dataclass(frozen=True)
class HttpSettings:
    request_delay_seconds: float
    timeout_seconds: int
    user_agent: str
    retry_max_attempts: int
    retry_backoff_seconds: int


@dataclass(frozen=True)
class BackupSettings:
    enabled: bool
    directory: str
    keep_daily: int
    keep_weekly: int


@dataclass(frozen=True)
class Settings:
    # PostgreSQL
    pg_host: str
    pg_port: int
    pg_db: str
    pg_user: str
    pg_password: str

    # Runtime
    state_dir: str
    log_dir: str
    log_level: str

    # Grouped feature settings
    http: HttpSettings
    telegram: TelegramSettings
    email: EmailSettings
    google: GoogleSettings
    observability: ObservabilitySettings
    backup: BackupSettings
    archive: ArchiveSettings
    llm: LLMSettings

    # Values that must never reach logs or error reports.
    secret_values: tuple[str, ...] = field(default=(), repr=False)

    @property
    def conninfo_safe(self) -> str:
        """Connection details with the password omitted — safe to log and print.

        The real connection string is built by ``db.conninfo()``, which escapes
        properly; this one exists only to be displayed, so it must never carry a
        credential.
        """
        return (
            f"host={self.pg_host} port={self.pg_port} dbname={self.pg_db} "
            f"user={self.pg_user}"
        )


def load_settings(dotenv_path: str | None = None) -> Settings:
    load_dotenv(dotenv_path=dotenv_path, override=False)

    pg_password = _require("PG_PASSWORD")
    telegram = TelegramSettings(
        enabled=_get_bool("TELEGRAM_ENABLED", True),
        bot_token=_get("TELEGRAM_BOT_TOKEN", ""),
        chat_id=_get("TELEGRAM_CHAT_ID", ""),
    )
    email = EmailSettings(
        enabled=_get_bool("EMAIL_ENABLED", False),
        host=_get("SMTP_HOST", ""),
        port=_get_int("SMTP_PORT", 587),
        use_tls=_get_bool("SMTP_USE_TLS", True),
        user=_get("SMTP_USER", ""),
        password=_get("SMTP_PASSWORD", ""),
        sender=_get("EMAIL_FROM", ""),
        recipients=tuple(r.strip() for r in _get("EMAIL_TO", "").split(",") if r.strip()),
    )
    google = GoogleSettings(
        enabled=_get_bool("GOOGLE_ENABLED", False),
        client_id=_get("GOOGLE_CLIENT_ID", ""),
        client_secret=_get("GOOGLE_CLIENT_SECRET", ""),
        refresh_token=_get("GOOGLE_REFRESH_TOKEN", ""),
        token_uri=_get("GOOGLE_TOKEN_URI", "https://oauth2.googleapis.com/token"),
        client_secrets_file=_get("GOOGLE_CLIENT_SECRETS_FILE", ""),
        token_file=_get("GOOGLE_TOKEN_FILE", ""),
        drive_root_folder=_get("GOOGLE_DRIVE_ROOT_FOLDER", "Studeo"),
        drive_backup_folder=_get("GOOGLE_DRIVE_BACKUP_FOLDER", "_backups/pap"),
        calendar_id=_get("GOOGLE_CALENDAR_ID", "primary"),
    )
    observability = ObservabilitySettings(
        sentry_dsn=_get("SENTRY_DSN", ""),
        environment=_get("SENTRY_ENVIRONMENT", "production"),
        release=_get("PAP_RELEASE", "dev"),
        healthchecks_base_url=_get("HEALTHCHECKS_BASE_URL", ""),
        healthchecks_ping_key=_get("HEALTHCHECKS_PING_KEY", ""),
        healthchecks_slug_prefix=_get("HEALTHCHECKS_SLUG_PREFIX", "pap"),
    )

    llm = LLMSettings(
        default_provider=_get("LLM_PROVIDER_DEFAULT", "claude"),
        purposes={
            purpose: provider
            for purpose, provider in (
                ("deliverable", _get("LLM_PROVIDER_DELIVERABLE", "")),
                ("book_resume", _get("LLM_PROVIDER_BOOK_RESUME", "")),
                ("classify", _get("LLM_PROVIDER_CLASSIFY", "")),
            )
            if provider
        },
        providers={
            "claude": LLMProviderSettings(
                name="claude", key_var="ANTHROPIC_API_KEY",
                api_key=_get("ANTHROPIC_API_KEY", ""),
                model=_get("LLM_MODEL_CLAUDE", "claude-opus-5"),
            ),
            "openai": LLMProviderSettings(
                name="openai", key_var="OPENAI_API_KEY",
                api_key=_get("OPENAI_API_KEY", ""),
                model=_get("LLM_MODEL_OPENAI", "gpt-5"),
            ),
            "gemini": LLMProviderSettings(
                name="gemini", key_var="GEMINI_API_KEY",
                api_key=_get("GEMINI_API_KEY", ""),
                model=_get("LLM_MODEL_GEMINI", "gemini-2.5-pro"),
            ),
        },
        max_tokens=_get_int("LLM_MAX_TOKENS", 8000),
        thinking=_get("LLM_THINKING", "adaptive").lower(),
        effort=_get("LLM_EFFORT", "high").lower(),
    )

    # Registered for scrubbing. Short values are excluded: redacting a 3-character
    # string would corrupt unrelated text all over an error report.
    secrets = tuple(
        v for v in (
            pg_password,
            telegram.bot_token,
            email.password,
            observability.healthchecks_ping_key,
            google.client_secret,
            google.refresh_token,
            *(p.api_key for p in llm.providers.values()),
            _get("STUDEO_PASSWORD", ""),
            _get("STUDEO_TOKEN", ""),
        ) if len(v) >= 8
    )

    return Settings(
        pg_host=_get("PG_HOST", "localhost"),
        pg_port=_get_int("PG_PORT", 5432),
        pg_db=_require("PG_DB"),
        pg_user=_require("PG_USER"),
        pg_password=pg_password,
        state_dir=_state_dir(),
        log_dir=_get("LOG_DIR", "./logs"),
        log_level=_get("LOG_LEVEL", "INFO").upper(),
        http=HttpSettings(
            request_delay_seconds=_get_float("HTTP_REQUEST_DELAY_SECONDS", 0.5),
            timeout_seconds=_get_int("HTTP_TIMEOUT_SECONDS", 30),
            user_agent=_get("HTTP_USER_AGENT", "personal-automation-platform/0.1"),
            retry_max_attempts=_get_int("RETRY_MAX_ATTEMPTS", 4),
            retry_backoff_seconds=_get_int("RETRY_BACKOFF_SECONDS", 2),
        ),
        telegram=telegram,
        email=email,
        google=google,
        observability=observability,
        backup=BackupSettings(
            enabled=_get_bool("BACKUP_ENABLED", True),
            directory=_get("BACKUP_DIR", "/var/lib/pap/backups"),
            keep_daily=_get_int("BACKUP_KEEP_DAILY", 14),
            keep_weekly=_get_int("BACKUP_KEEP_WEEKLY", 8),
        ),
        archive=ArchiveSettings(
            books_dir=_get("ARCHIVE_BOOKS_DIR", "/var/lib/pap/livros"),
            materials_dir=_get("ARCHIVE_MATERIALS_DIR", "/var/lib/pap/materiais"),
            deliverables_dir=_get("ARCHIVE_DELIVERABLES_DIR", "/var/lib/pap/entregas"),
            max_book_mb=_get_int("ARCHIVE_MAX_BOOK_MB", 250),
        ),
        llm=llm,
        secret_values=secrets,
    )
