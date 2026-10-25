"""Notification deduplication, delivery retries and failure isolation."""

import pytest
from sqlalchemy import select

from app.core.exceptions import IntegrationError, TransientError
from app.db.models import Email, NotificationDelivery
from app.db.models.enums import DeliveryStatus, NotificationType
from app.integrations.notifiers.base import Notifier
from app.services import analysis_service, notification_service, sync_service


class FakeNotifier(Notifier):
    channel = "telegram"

    def __init__(self, failures: list[Exception] | None = None):
        self.failures = list(failures or [])
        self.sent: list[tuple[str, str]] = []

    async def send(self, destination, notification):
        if self.failures:
            raise self.failures.pop(0)
        self.sent.append((destination, notification.text))


class FakeRegistry:
    def __init__(self, notifier):
        self.notifier = notifier

    def get(self, channel):
        return self.notifier


@pytest.fixture
async def emergency(session, store, sandbox_account, user):
    await notification_service.update_preferences(session, user.id, None,
                                                  {"telegram_enabled": True, "telegram_chat_id": "42"})
    await sync_service.sync_account(session, store, sandbox_account.id)
    email = (await session.execute(select(Email).where(Email.provider_message_id == "sbx-001"))).scalar_one()
    await analysis_service.run_analysis(session, email.id)
    delivery = (await session.execute(select(NotificationDelivery))).scalar_one()
    return email, delivery


async def test_notification_is_deduplicated(session, emergency):
    email, _ = emergency
    await session.refresh(email)
    again = await notification_service.create_email_notification(session, email, email.analysis,
                                                                 NotificationType.EMERGENCY)
    assert again is None


async def test_delivery_success_contains_minimal_content(session, emergency):
    _, delivery = emergency
    notifier = FakeNotifier()
    await notification_service.deliver(session, delivery.id, FakeRegistry(notifier))
    await session.refresh(delivery)
    assert delivery.status == DeliveryStatus.DELIVERED and delivery.attempts == 1
    destination, text = notifier.sent[0]
    assert destination == "42"
    assert "Summary:" in text and "$40k per hour" not in text.split("Summary:")[0]
    # Repeated delivery of a delivered record is a no-op (idempotent).
    await notification_service.deliver(session, delivery.id, FakeRegistry(notifier))
    assert len(notifier.sent) == 1


async def test_transient_failures_retry_until_limit(session, emergency, monkeypatch):
    _, delivery = emergency
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "notification_max_attempts", 2)
    notifier = FakeNotifier([TransientError("timeout"), TransientError("timeout")])
    with pytest.raises(TransientError):
        await notification_service.deliver(session, delivery.id, FakeRegistry(notifier))
    await session.refresh(delivery)
    assert delivery.status == DeliveryStatus.PENDING and delivery.attempts == 1
    finished = await notification_service.deliver(session, delivery.id, FakeRegistry(notifier))
    await session.refresh(delivery)
    assert finished and delivery.status == DeliveryStatus.FAILED and delivery.attempts == 2


async def test_rejected_delivery_fails_without_raising(session, emergency):
    _, delivery = emergency
    notifier = FakeNotifier([IntegrationError("chat not found", code="telegram_rejected")])
    assert await notification_service.deliver(session, delivery.id, FakeRegistry(notifier))
    await session.refresh(delivery)
    assert delivery.status == DeliveryStatus.FAILED


async def test_unconfigured_channel_is_skipped(session, emergency):
    _, delivery = emergency
    # Real registry: TELEGRAM_BOT_TOKEN is not configured in tests.
    assert await notification_service.deliver(session, delivery.id)
    await session.refresh(delivery)
    assert delivery.status == DeliveryStatus.SKIPPED


async def test_per_account_preferences_override_defaults(session, user, sandbox_account):
    await notification_service.update_preferences(session, user.id, None, {"auto_generate_drafts": False})
    await notification_service.update_preferences(session, user.id, sandbox_account.id,
                                                  {"auto_generate_drafts": True})
    assert (await notification_service.effective_preferences(session, user.id, sandbox_account.id)).auto_generate_drafts
    assert not (await notification_service.effective_preferences(session, user.id, None)).auto_generate_drafts
