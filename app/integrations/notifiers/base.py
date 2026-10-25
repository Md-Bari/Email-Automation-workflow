"""External notification channels. Payloads carry minimal content (never full bodies or tokens)."""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from abc import ABC, abstractmethod
from dataclasses import dataclass
from email.message import EmailMessage

import httpx

from app.core.config import Settings
from app.core.exceptions import IntegrationError, TransientError


@dataclass(slots=True)
class OutboundNotification:
    title: str
    text: str
    link: str | None


class NotifierNotConfigured(IntegrationError):
    def __init__(self, channel: str) -> None:
        super().__init__(f"{channel} channel is not configured", code="channel_not_configured", retryable=False)


class Notifier(ABC):
    channel: str

    @abstractmethod
    async def send(self, destination: str, notification: OutboundNotification) -> None: ...


class TelegramNotifier(Notifier):
    channel = "telegram"

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        if not settings.telegram_bot_token:
            raise NotifierNotConfigured("telegram")
        self._token = settings.telegram_bot_token.get_secret_value()
        self._timeout = settings.notification_timeout_seconds
        self._transport = transport

    async def send(self, destination: str, notification: OutboundNotification) -> None:
        text = f"{notification.title}\n\n{notification.text}"
        if notification.link:
            text += f"\n\nOpen: {notification.link}"
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            try:
                response = await client.post(
                    f"https://api.telegram.org/bot{self._token}/sendMessage",
                    json={"chat_id": destination, "text": text[:4000], "disable_web_page_preview": True},
                )
            except httpx.HTTPError as exc:
                raise TransientError("Telegram unreachable", code="telegram_unavailable") from exc
        if response.status_code == 429 or response.status_code >= 500:
            raise TransientError(f"Telegram error {response.status_code}", code="telegram_unavailable")
        if response.status_code >= 400:
            raise IntegrationError(f"Telegram rejected message ({response.status_code})", code="telegram_rejected")


class SmtpNotifier(Notifier):
    channel = "email"

    def __init__(self, settings: Settings) -> None:
        if not (settings.smtp_host and settings.smtp_from):
            raise NotifierNotConfigured("email")
        self.settings = settings

    def _send_sync(self, destination: str, notification: OutboundNotification) -> None:
        s = self.settings
        msg = EmailMessage()
        msg["From"] = s.smtp_from
        msg["To"] = destination
        msg["Subject"] = notification.title[:200]
        body = notification.text + (f"\n\nOpen in the app: {notification.link}" if notification.link else "")
        msg.set_content(body)
        with smtplib.SMTP(s.smtp_host or "", s.smtp_port, timeout=s.notification_timeout_seconds) as smtp:
            if s.smtp_use_tls:
                smtp.starttls(context=ssl.create_default_context())
            if s.smtp_username and s.smtp_password:
                smtp.login(s.smtp_username, s.smtp_password.get_secret_value())
            smtp.send_message(msg)

    async def send(self, destination: str, notification: OutboundNotification) -> None:
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self._send_sync, destination, notification),
                timeout=self.settings.notification_timeout_seconds + 5,
            )
        except (TimeoutError, OSError, smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError) as exc:
            raise TransientError("SMTP delivery failed", code="smtp_unavailable") from exc
        except smtplib.SMTPException as exc:
            raise IntegrationError("SMTP rejected message", code="smtp_rejected") from exc


class NotifierRegistry:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def get(self, channel: str) -> Notifier:
        if channel == "telegram":
            return TelegramNotifier(self.settings)
        if channel == "email":
            return SmtpNotifier(self.settings)
        raise NotifierNotConfigured(channel)
