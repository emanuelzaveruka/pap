"""SMTP sink — the secondary notification channel, stdlib only.

Two connection styles, chosen by port rather than by a separate setting because
getting them mismatched is the usual reason SMTP silently fails: port 465 is
implicit TLS (``SMTP_SSL`` from the first byte), everything else connects in the
clear and upgrades with STARTTLS when ``SMTP_USE_TLS`` is on.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage

from ..config import EmailSettings
from ..core.models import Notification, SendResult

log = logging.getLogger(__name__)

IMPLICIT_TLS_PORT = 465


class EmailSink:
    name = "email"

    def __init__(self, settings: EmailSettings, *, timeout: int = 30) -> None:
        self.settings = settings
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return self.settings.configured

    def _build(self, notification: Notification) -> EmailMessage:
        message = EmailMessage()
        message["Subject"] = notification.title
        message["From"] = self.settings.sender
        message["To"] = ", ".join(self.settings.recipients)
        body = notification.body or ""
        if notification.url:
            body = f"{body}\n\n{notification.url}" if body else notification.url
        message.set_content(body or notification.title)
        return message

    def send(self, notification: Notification) -> SendResult:
        if not self.configured:
            return SendResult(ok=False, detail="email is not configured")

        message = self._build(notification)
        context = ssl.create_default_context()
        try:
            if self.settings.port == IMPLICIT_TLS_PORT:
                with smtplib.SMTP_SSL(self.settings.host, self.settings.port,
                                      timeout=self.timeout, context=context) as smtp:
                    self._authenticate(smtp)
                    smtp.send_message(message)
            else:
                with smtplib.SMTP(self.settings.host, self.settings.port,
                                  timeout=self.timeout) as smtp:
                    smtp.ehlo()
                    if self.settings.use_tls:
                        smtp.starttls(context=context)
                        smtp.ehlo()
                    self._authenticate(smtp)
                    smtp.send_message(message)
        except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
            return SendResult(ok=False, detail=f"smtp failed: {type(exc).__name__}: {exc}")
        return SendResult(ok=True, detail=f"sent to {len(self.settings.recipients)} recipient(s)")

    def _authenticate(self, smtp: smtplib.SMTP) -> None:
        if self.settings.user and self.settings.password:
            smtp.login(self.settings.user, self.settings.password)
