"""Email delivery. Two interchangeable back-ends, chosen from .env:

- SMTP  : works with any mailbox (Gmail, Outlook, Zoho, your own domain). Easiest to start with.
- SendGrid: for higher volume and open/click tracking.

Both return {"status": "sent" | "failed", ...} and never raise, so one bad
address cannot stop a campaign.
"""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from typing import Any

from ..config import Settings


class SmtpMailer:
    def __init__(self, settings: Settings, from_name: str = ""):
        self._s = settings
        self._from = formataddr((from_name, settings.from_email)) if from_name else settings.from_email

    def send(self, to: str, subject: str, body: str) -> dict[str, Any]:
        message = EmailMessage()
        message["From"], message["To"], message["Subject"] = self._from, to, subject
        message["Message-ID"] = make_msgid(domain=self._s.from_email.split("@")[-1])  # not this PC's hostname
        message.set_content(body)
        s = self._s
        try:
            if s.smtp_port == 465:  # implicit TLS
                server = smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, timeout=30, context=ssl.create_default_context())
            else:
                server = smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=30)
            with server:
                server.ehlo()
                if s.smtp_port != 465 and server.has_extn("starttls"):
                    server.starttls(context=ssl.create_default_context())
                    server.ehlo()
                if s.smtp_user:
                    server.login(s.smtp_user, s.smtp_password)
                refused = server.send_message(message)
        except Exception as exc:  # noqa: BLE001 - report, don't crash the campaign
            return {"status": "failed", "error": f"{type(exc).__name__}: {str(exc)[:250]}"}
        if refused:
            return {"status": "failed", "error": f"Recipient refused: {refused}"}
        return {"status": "sent", "message_id": message["Message-ID"]}


class SendGridMailer:
    def __init__(self, settings: Settings, from_name: str = ""):
        from sendgrid import SendGridAPIClient

        self._client = SendGridAPIClient(settings.sendgrid_api_key)
        self._from = (settings.from_email, from_name) if from_name else settings.from_email

    def send(self, to: str, subject: str, body: str) -> dict[str, Any]:
        from sendgrid.helpers.mail import Mail

        message = Mail(from_email=self._from, to_emails=to, subject=subject, plain_text_content=body)
        try:
            response = self._client.send(message)
        except Exception as exc:  # noqa: BLE001
            return {"status": "failed", "error": f"{type(exc).__name__}: {str(exc)[:250]}"}
        return {"status": "sent", "message_id": response.headers.get("X-Message-Id", "")}


def build_mailer(settings: Settings, from_name: str = ""):
    """Return the configured mailer, or None when live sending is not possible."""
    if not settings.can_send_live:
        return None
    return SmtpMailer(settings, from_name) if settings.email_provider == "smtp" else SendGridMailer(settings, from_name)
