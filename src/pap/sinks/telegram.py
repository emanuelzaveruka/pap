"""Telegram sink — the primary notification channel.

Plain ``requests`` against the Bot API; no SDK, matching how the rest of your
projects talk to HTTP services.

**The 4096-character limit is the design constraint here.** Telegram rejects any
`sendMessage` longer than that, and the book resume feed (Phase 4) routinely
produces text well past it. So splitting is built in from the start rather than
discovered later: text is broken on paragraph, then line, then word boundaries,
and only a single unbroken run of characters is ever hard-cut. Anything that
would take more than ``MAX_MESSAGES`` chunks is sent as a ``.md`` document
instead, which is both easier to read and a single notification rather than
fifteen.

HTML parse mode is used rather than Markdown: Telegram's legacy Markdown breaks
on unbalanced ``*`` or ``_``, which appear constantly in course material, and a
parse failure would reject the whole message.
"""

from __future__ import annotations

import html
import io
import logging

import requests

from ..config import TelegramSettings
from ..core.models import Notification, SendResult

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
TELEGRAM_MAX_CHARS = 4096
# Leave room for the header/footer wrapped around each chunk.
CHUNK_LIMIT = 3800
MAX_MESSAGES = 4


def split_message(text: str, limit: int = CHUNK_LIMIT) -> list[str]:
    """Split text into chunks no longer than ``limit``, preferring clean breaks.

    Tries paragraph breaks first, then single newlines, then spaces, and only
    hard-cuts a run of characters with no break in it at all (a long URL, say).
    Pure function with no I/O so the boundary logic is directly testable.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    text = text or ""
    if len(text) <= limit:
        return [text] if text else []

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = -1
        for separator in ("\n\n", "\n", " "):
            found = window.rfind(separator)
            # Ignore a break so early in the window that it would produce a tiny
            # chunk and push nearly everything into the next one.
            if found > limit // 4:
                cut = found + (len(separator) if separator != " " else 1)
                break
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return [c for c in chunks if c]


def render(notification: Notification) -> str:
    """Notification -> Telegram HTML. Everything interpolated is escaped."""
    parts = [f"<b>{html.escape(notification.title)}</b>"]
    if notification.body:
        parts.append(html.escape(notification.body))
    if notification.url:
        parts.append(f'<a href="{html.escape(notification.url, quote=True)}">Abrir</a>')
    return "\n\n".join(parts)


class TelegramSink:
    name = "telegram"

    def __init__(self, settings: TelegramSettings, *, timeout: int = 30) -> None:
        self.settings = settings
        self.timeout = timeout
        self.session = requests.Session()

    @property
    def configured(self) -> bool:
        return self.settings.configured

    def _url(self, method: str) -> str:
        return f"{API_BASE}/bot{self.settings.bot_token}/{method}"

    def _post(self, method: str, **kwargs) -> tuple[bool, str]:
        try:
            response = self.session.post(self._url(method), timeout=self.timeout, **kwargs)
        except requests.RequestException as exc:
            return False, f"request failed: {exc}"
        if response.status_code == 200:
            return True, ""
        # The bot token is in the URL, so never echo the URL into an error string.
        detail = response.text[:500]
        return False, f"telegram {method} returned {response.status_code}: {detail}"

    def send(self, notification: Notification) -> SendResult:
        if not self.configured:
            return SendResult(ok=False, detail="telegram is not configured")

        text = render(notification)
        chunks = split_message(text)
        if not chunks:
            return SendResult(ok=True, detail="nothing to send")

        if len(chunks) > MAX_MESSAGES:
            return self._send_document(notification, text)

        for index, chunk in enumerate(chunks, start=1):
            suffix = f"\n\n<i>({index}/{len(chunks)})</i>" if len(chunks) > 1 else ""
            ok, detail = self._post(
                "sendMessage",
                data={
                    "chat_id": self.settings.chat_id,
                    "text": chunk + suffix,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": "true",
                },
            )
            if not ok:
                return SendResult(ok=False, detail=f"part {index}/{len(chunks)}: {detail}")
        return SendResult(ok=True, detail=f"sent in {len(chunks)} message(s)")

    def _send_document(self, notification: Notification, text: str) -> SendResult:
        """Too long to read as chat messages — send it as a file instead."""
        filename = _safe_filename(notification.title) + ".md"
        body = f"# {notification.title}\n\n{notification.body}\n"
        if notification.url:
            body += f"\n{notification.url}\n"
        ok, detail = self._post(
            "sendDocument",
            data={
                "chat_id": self.settings.chat_id,
                "caption": _truncate(notification.title, 1000),
            },
            files={"document": (filename, io.BytesIO(body.encode("utf-8")), "text/markdown")},
        )
        return SendResult(ok=ok, detail=detail or f"sent as {filename}")


def _safe_filename(title: str, *, limit: int = 60) -> str:
    keep = [c if (c.isalnum() or c in "-_ ") else "-" for c in (title or "resume")]
    return ("".join(keep).strip().replace(" ", "-") or "resume")[:limit]


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
