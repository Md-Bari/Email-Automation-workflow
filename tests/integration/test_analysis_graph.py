"""The LangGraph analysis workflow end to end (mock LLM, sandbox emails)."""

import pytest
from sqlalchemy import select

from app.db.models import Email, EmailDraft, Notification
from app.db.models.enums import NotificationType, Priority, ProcessingStatus
from app.integrations.llm.base import LLMError, LLMProvider, LLMTransientError
from app.integrations.llm.factory import set_llm_provider
from app.services import analysis_service, sync_service
from app.services.dispatcher import DELIVER_NOTIFICATION


@pytest.fixture
async def synced(session, store, sandbox_account):
    await sync_service.sync_account(session, store, sandbox_account.id)
    rows = (await session.execute(select(Email).where(Email.email_account_id == sandbox_account.id))).scalars().all()
    return {e.provider_message_id: e for e in rows}


async def test_production_outage_is_emergency_with_notification_and_draft(session, synced):
    email = synced["sbx-001"]
    outcome = await analysis_service.run_analysis(session, email.id)
    assert outcome.status == "analyzed"
    assert outcome.state["trace"][:5] == ["load_email", "screen_email", "analyze_email", "apply_policy",
                                          "persist_analysis"]
    # Notification is created before the draft is generated.
    trace = outcome.state["trace"]
    assert trace.index("create_notification:emergency") < trace.index("generate_reply_draft")
    await session.refresh(email)
    assert email.processing_status == ProcessingStatus.ANALYZED
    assert email.analysis.is_emergency and email.analysis.priority == Priority.URGENT
    assert email.analysis.emergency_evidence
    notes = (await session.execute(select(Notification).where(Notification.email_id == email.id))).scalars().all()
    assert len(notes) == 1 and notes[0].notification_type == NotificationType.EMERGENCY
    # Subject, sender, reason, summary and action only - never the full body.
    assert "Reason:" in notes[0].message and "Summary:" in notes[0].message
    assert "decision on rolling back" not in notes[0].message
    drafts = (await session.execute(select(EmailDraft).where(EmailDraft.email_id == email.id))).scalars().all()
    assert len(drafts) == 1 and drafts[0].to_address == "oncall@acme-corp.example"


async def test_marketing_urgent_language_is_not_an_emergency(session, synced):
    email = synced["sbx-002"]
    await analysis_service.run_analysis(session, email.id)
    await session.refresh(email)
    assert not email.analysis.is_emergency
    assert email.analysis.priority == Priority.LOW
    assert not email.analysis.reply_recommended
    assert (await session.execute(select(Notification))).scalars().all() == []


async def test_prompt_injection_email_is_contained(session, synced):
    email = synced["sbx-005"]
    outcome = await analysis_service.run_analysis(session, email.id)
    await session.refresh(email)
    a = email.analysis
    assert not a.is_emergency and a.needs_review and not a.reply_recommended
    assert a.decision_trace["prompt_injection_suspected"]
    assert outcome.state.get("draft_id") is None
    assert (await session.execute(select(EmailDraft))).scalars().all() == []


async def test_meeting_request_gets_draft_and_no_notification(session, synced):
    email = synced["sbx-004"]
    outcome = await analysis_service.run_analysis(session, email.id)
    assert outcome.state["draft_id"]
    assert "create_notification:emergency" not in outcome.state["trace"]


async def test_reanalysis_is_idempotent(session, synced):
    email = synced["sbx-001"]
    await analysis_service.run_analysis(session, email.id)
    second = await analysis_service.run_analysis(session, email.id)
    assert second.state["skip_reason"] == "already_analyzed"
    forced = await analysis_service.run_analysis(session, email.id, force=True)
    assert forced.status == "analyzed"
    # Forced re-analysis does not duplicate the notification or the draft.
    assert len((await session.execute(select(Notification))).scalars().all()) == 1
    assert len((await session.execute(select(EmailDraft))).scalars().all()) == 1


async def test_emergency_notification_dispatches_external_deliveries(session, synced, user, dispatcher):
    from app.services import notification_service

    await notification_service.update_preferences(session, user.id, None,
                                                  {"telegram_enabled": True, "telegram_chat_id": "12345"})
    dispatcher.clear()
    await analysis_service.run_analysis(session, synced["sbx-003"].id)
    assert dispatcher.names().count(DELIVER_NOTIFICATION) == 1


class FailingLLM(LLMProvider):
    model_name = "failing"

    def __init__(self, exc):
        self.exc, self.calls = exc, 0

    async def structured(self, **kwargs):
        self.calls += 1
        raise self.exc


async def test_llm_outage_keeps_email_pending_without_inventing_results(session, synced, monkeypatch):
    from app.agents import graph as graph_mod

    monkeypatch.setattr(graph_mod, "LLM_RETRY", graph_mod.LLM_RETRY._replace(initial_interval=0.01))
    graph_mod.get_graph.cache_clear()
    llm = FailingLLM(LLMTransientError("down"))
    email = synced["sbx-004"]
    outcome = await analysis_service.run_analysis(session, email.id, llm=llm)
    assert outcome.status == "pending_retry" and outcome.retryable
    assert llm.calls == 3  # LangGraph RetryPolicy attempts
    await session.refresh(email)
    assert email.processing_status == ProcessingStatus.PENDING
    assert email.analysis is None
    assert "llm_unavailable" in email.processing_error
    graph_mod.get_graph.cache_clear()


async def test_non_retryable_llm_error_marks_failed(session, synced):
    set_llm_provider(None)
    email = synced["sbx-006"]
    outcome = await analysis_service.run_analysis(session, email.id, llm=FailingLLM(LLMError("bad key")))
    assert outcome.status == "failed" and not outcome.retryable
    await session.refresh(email)
    assert email.processing_status == ProcessingStatus.FAILED


async def test_empty_email_is_skipped(session, synced):
    email = synced["sbx-006"]
    email.body_text, email.snippet, email.subject = "", None, ""
    await session.commit()
    outcome = await analysis_service.run_analysis(session, email.id)
    assert outcome.status == "skipped"
    await session.refresh(email)
    assert email.processing_status == ProcessingStatus.SKIPPED
