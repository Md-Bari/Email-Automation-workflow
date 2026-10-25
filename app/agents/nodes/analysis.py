"""Nodes: load_email -> screen_email -> analyze_email (LLM) -> apply_policy -> persist_analysis."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import asdict

from langchain_core.runnables import RunnableConfig

from app.agents.context import build_context
from app.agents.nodes.deps import deps
from app.agents.prompts.templates import ANALYSIS_SYSTEM, analysis_user_prompt
from app.agents.rules import RuleSignals, apply_policy, compute_signals
from app.agents.state import AnalysisState
from app.db.models import Email, EmailAnalysis
from app.db.models.enums import ProcessingStatus
from app.integrations.llm.base import LLMTransientError
from app.integrations.llm.schemas import EmailAssessment
from app.observability import metrics


async def _email(config: RunnableConfig, state: AnalysisState) -> Email:
    email = await deps(config).session.get(Email, uuid.UUID(state["email_id"]))
    if email is None:
        raise LookupError("email disappeared during processing")
    return email


async def load_email(state: AnalysisState, config: RunnableConfig) -> AnalysisState:
    d = deps(config)
    email = await d.session.get(Email, uuid.UUID(state["email_id"]))
    if email is None:
        return {"skip_reason": "not_found", "trace": ["load_email:not_found"]}
    if email.provider_deleted_at is not None:
        return {"skip_reason": "deleted_at_provider", "trace": ["load_email:deleted"]}
    analysis = email.analysis
    if (
        analysis is not None
        and analysis.analysis_version == d.settings.analysis_version
        and not state.get("force")
        and email.processing_status == ProcessingStatus.ANALYZED
    ):
        # Idempotency: an up-to-date analysis already exists.
        return {"skip_reason": "already_analyzed", "analysis_id": str(analysis.id), "trace": ["load_email:cached"]}
    email.processing_status = ProcessingStatus.PROCESSING
    email.analysis_attempts += 1
    email.processing_error = None
    await d.session.commit()
    return {"skip_reason": None, "trace": ["load_email"]}


async def screen_email(state: AnalysisState, config: RunnableConfig) -> AnalysisState:
    d = deps(config)
    email = await _email(config, state)
    body = email.body_text or email.snippet or ""
    if not body.strip() and not email.subject.strip():
        email.processing_status = ProcessingStatus.SKIPPED
        email.processing_error = "Empty email"
        await d.session.commit()
        return {"skip_reason": "empty_email", "trace": ["screen_email:empty"]}
    signals = compute_signals(
        subject=email.subject, body=body, sender_email=email.sender_email,
        is_automated=email.is_automated, trusted_alert_domains=d.settings.trusted_alert_domains,
    )
    return {"signals": signals.as_dict(), "trace": ["screen_email"]}


async def analyze_email(state: AnalysisState, config: RunnableConfig) -> AnalysisState:
    """Single structured LLM call. Transient failures are retried by the node's RetryPolicy."""
    d = deps(config)
    email = await _email(config, state)
    ctx = await build_context(d.session, email, d.settings.llm_max_input_chars)
    prompt = analysis_user_prompt(ctx.data, ctx.body, ctx.truncated, ctx.thread)
    try:
        assessment = await asyncio.wait_for(
            d.llm.structured(system=ANALYSIS_SYSTEM, user=prompt, schema=EmailAssessment,
                             context=ctx.mock_context()),
            timeout=d.settings.llm_timeout_seconds * (d.settings.llm_max_retries + 1) + 5,
        )
    except TimeoutError as exc:
        raise LLMTransientError("LLM call timed out", code="llm_timeout") from exc
    data = assessment.model_dump(mode="json")
    data["_input_truncated"] = ctx.truncated
    return {"assessment": data, "trace": ["analyze_email"]}


async def apply_policy_node(state: AnalysisState, config: RunnableConfig) -> AnalysisState:
    d = deps(config)
    email = await _email(config, state)
    assessment = EmailAssessment.model_validate({k: v for k, v in state["assessment"].items() if not k.startswith("_")})
    raw = state["signals"]
    signals = RuleSignals(**{k: v for k, v in raw.items() if k != "urgency_language_only"})
    decision = apply_policy(
        assessment, signals, subject=email.subject, body=email.body_text or email.snippet or "",
        emergency_threshold=d.settings.emergency_confidence_threshold,
        review_threshold=d.settings.review_confidence_threshold,
    )
    data = asdict(decision)
    data["priority"] = decision.priority.value
    return {"decision": data, "trace": ["apply_policy"]}


async def persist_analysis(state: AnalysisState, config: RunnableConfig) -> AnalysisState:
    """Upsert the analysis (one row per email) - safe to repeat on retry."""
    d = deps(config)
    email = await _email(config, state)
    a, dec = state["assessment"], state["decision"]
    analysis = email.analysis or EmailAnalysis(email_id=email.id)
    analysis.summary = a["summary"]
    analysis.intent = a["intent"][:255]
    analysis.category = a["category"]
    analysis.priority = dec["priority"]
    analysis.is_emergency = dec["is_emergency"]
    analysis.emergency_reason = dec["emergency_reason"]
    analysis.emergency_evidence = dec["emergency_evidence"]
    analysis.suggested_action = a["suggested_action"]
    analysis.reply_recommended = dec["reply_recommended"]
    analysis.confidence_score = dec["confidence"]
    analysis.needs_review = dec["needs_review"]
    analysis.review_reason = dec["review_reason"]
    analysis.input_truncated = bool(a.get("_input_truncated"))
    analysis.decision_trace = {
        "signals": state["signals"],
        "llm_emergency_claim": a["emergency"]["is_emergency"],
        "llm_priority": a["priority"],
        "prompt_injection_suspected": a["prompt_injection_suspected"],
        "adjustments": dec["adjustments"],
    }
    analysis.model_name = d.llm.model_name
    analysis.analysis_version = d.settings.analysis_version
    if email.analysis is None:
        d.session.add(analysis)
        email.analysis = analysis
    email.processing_status = ProcessingStatus.ANALYZED
    email.processing_error = None
    await d.session.commit()
    metrics.ANALYSES.labels(outcome="analyzed").inc()
    if dec["is_emergency"]:
        metrics.EMERGENCIES.inc()
    return {"analysis_id": str(analysis.id), "trace": ["persist_analysis"]}
