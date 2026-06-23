"""Resend email client wrapper."""

from __future__ import annotations

import json
import smtplib
import ssl
from asyncio import to_thread
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from sibyl.email.templates import EmailTemplate

log = structlog.get_logger()

_client: EmailClient | None = None


class EmailClient:
    """Wrapper around Resend for sending transactional emails."""

    def __init__(self) -> None:
        from sibyl.config import settings

        self._api_key = settings.resend_api_key.get_secret_value()
        self._from_address = settings.email_from
        self._outbox_path = settings.email_outbox_path.strip()
        self._smtp_host = settings.smtp_host.strip()
        self._smtp_port = settings.smtp_port
        self._smtp_username = settings.smtp_username.strip()
        self._smtp_password = settings.smtp_password.get_secret_value()
        self._smtp_starttls = settings.smtp_starttls
        self._smtp_ssl = settings.smtp_ssl
        self._smtp_timeout_seconds = settings.smtp_timeout_seconds
        self._resend: object | None = None

        if self._api_key:
            try:
                import resend

                resend.api_key = self._api_key
                self._resend = resend
            except ImportError:
                log.warning("resend package not installed, emails will be logged only")

    @property
    def configured(self) -> bool:
        """Check if email sending is configured."""
        return bool(self._smtp_host or (self._api_key and self._resend))

    async def send(
        self,
        *,
        to: str | list[str],
        subject: str,
        html: str,
        text: str | None = None,
        reply_to: str | None = None,
    ) -> str | None:
        """Send an email.

        Returns:
            Email ID if sent successfully, None if not configured.
        """
        recipients = [to] if isinstance(to, str) else to

        outbox_written = self._write_outbox(
            to=recipients,
            subject=subject,
            html=html,
            text=text,
            reply_to=reply_to,
        )

        if self._smtp_host:
            return await self._send_smtp(
                to=recipients,
                subject=subject,
                html=html,
                text=text,
                reply_to=reply_to,
                outbox_written=outbox_written,
            )

        if not self._resend:
            log.info(
                "email_skipped",
                reason="not_configured",
                to=recipients,
                subject=subject,
            )
            return "outbox" if outbox_written else None

        try:
            import resend

            params: dict[str, object] = {
                "from_": self._from_address,
                "to": recipients,
                "subject": subject,
                "html": html,
            }
            if text:
                params["text"] = text
            if reply_to:
                params["reply_to"] = reply_to

            result = resend.Emails.send(params)
            email_id = result.get("id") if isinstance(result, dict) else None

            log.info("email_sent", email_id=email_id, to=recipients, subject=subject)
            return email_id or "resend"

        except Exception:
            log.exception("email_failed", to=recipients, subject=subject)
            return "outbox" if outbox_written else None

    async def _send_smtp(
        self,
        *,
        to: list[str],
        subject: str,
        html: str,
        text: str | None,
        reply_to: str | None,
        outbox_written: bool,
    ) -> str | None:
        try:
            await to_thread(
                self._send_smtp_sync,
                to=to,
                subject=subject,
                html=html,
                text=text,
                reply_to=reply_to,
            )
            log.info("email_sent", provider="smtp", to=to, subject=subject)
            return "smtp"
        except Exception:
            log.exception("email_failed", provider="smtp", to=to, subject=subject)
            return "outbox" if outbox_written else None

    def _send_smtp_sync(
        self,
        *,
        to: list[str],
        subject: str,
        html: str,
        text: str | None,
        reply_to: str | None,
    ) -> None:
        message = EmailMessage()
        message["From"] = self._from_address
        message["To"] = ", ".join(to)
        message["Subject"] = subject
        if reply_to:
            message["Reply-To"] = reply_to
        message.set_content(text or "")
        message.add_alternative(html, subtype="html")

        context = ssl.create_default_context()
        if self._smtp_ssl:
            smtp = smtplib.SMTP_SSL(
                self._smtp_host,
                self._smtp_port,
                timeout=self._smtp_timeout_seconds,
                context=context,
            )
        else:
            smtp = smtplib.SMTP(
                self._smtp_host,
                self._smtp_port,
                timeout=self._smtp_timeout_seconds,
            )
        with smtp:
            if self._smtp_starttls and not self._smtp_ssl:
                smtp.starttls(context=context)
            if self._smtp_username:
                smtp.login(self._smtp_username, self._smtp_password)
            smtp.send_message(message)

    def _write_outbox(
        self,
        *,
        to: list[str],
        subject: str,
        html: str,
        text: str | None,
        reply_to: str | None,
    ) -> bool:
        if not self._outbox_path:
            return False
        path = Path(self._outbox_path).expanduser()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            record = {
                "created_at": datetime.now(UTC).isoformat(),
                "to": to,
                "subject": subject,
                "html": html,
                "text": text,
                "reply_to": reply_to,
            }
            with path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(record, sort_keys=True))
                file.write("\n")
            return True
        except Exception:
            log.exception("email_outbox_write_failed", path=str(path))
            return False

    async def send_template(
        self,
        template: EmailTemplate,
        *,
        to: str | list[str],
        reply_to: str | None = None,
    ) -> str | None:
        """Send an email using a template."""
        return await self.send(
            to=to,
            subject=template.subject,
            html=template.render_html(),
            text=template.render_text(),
            reply_to=reply_to,
        )


def get_email_client() -> EmailClient:
    """Get or create the global email client singleton."""
    global _client  # noqa: PLW0603
    if _client is None:
        _client = EmailClient()
    return _client
