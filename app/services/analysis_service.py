"""Runs the LangGraph analysis workflow and records structured error states."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.graph import get_graph
from app.agents.nodes.deps import GraphDeps
from app.core.config import get_settings
from app.core.exceptions import IntegrationError
from app.core.logging import correlation_id_var, get_logger
from app.db.models import Email
from app.db.models.enums import ProcessingStatus
from app.integrations.llm.base import LLMProvider
from app.integrations.llm.factory import get_llm_provider
from app.observability import metrics
from app.services.dispatcher import get_dispatcher

logger = get_logger(__name__)


@dataclass(slots=True)
class AnalysisOutcome:
    status: str  # analyzed | skipped | failed | pending_retry
    state: dict[str, Any]
    error_code: str | None = None
    retryable: bool = False


async def run_analysis(
    session: AsyncSession, email_id: uuid.UUID, *, force: bool = False, llm: LLMProvider | None = None
) -> AnalysisOutcome:
    settings = get_settings()
    deps = GraphDeps(session=session, llm=llm or get_llm_provider(), settings=settings, dispatcher=get_dispatcher())
    initial = {"email_id": str(email_id), "force": force, "correlation_id": correlation_id_var.get(), "trace": []}
    try:
        state = await get_graph().ainvoke(initial, config={"configurable": {"deps": deps}})
    except IntegrationError as exc:
        await session.rollback()
        return await _record_failure(session, email_id, exc.code, str(exc), exc.retryable)
    except LookupError:
        return AnalysisOutcome(status="skipped", state={"skip_reason": "not_found"})
    except Exception as exc:
        await session.rollback()
        logger.exception("analysis_unexpected_error", email_id=str(email_id))
        return await _record_failure(session, email_id, "internal_error", type(exc).__name__, True)
    return AnalysisOutcome(status=state.get("status", "analyzed"), state=dict(state))


async def _record_failure(
    session: AsyncSession, email_id: uuid.UUID, code: str, message: str, retryable: bool
) -> AnalysisOutcome:
    settings = get_settings()
    metrics.LLM_FAILURES.labels(code=code).inc()
    email = await session.get(Email, email_id)
    if email is None:
        return AnalysisOutcome(status="failed", state={}, error_code=code)
    can_retry = retryable and email.analysis_attempts < settings.analysis_max_attempts
    # No AI result is invented: the email stays pending (for retry) or failed, never "analyzed".
    email.processing_status = ProcessingStatus.PENDING if can_retry else ProcessingStatus.FAILED
    email.processing_error = f"{code}: {message}"[:500]
    await session.commit()
    metrics.ANALYSES.labels(outcome="pending_retry" if can_retry else "failed").inc()
    logger.warning("analysis_failed", email_id=str(email_id), code=code, retryable=can_retry)
    return AnalysisOutcome(
        status="pending_retry" if can_retry else "failed", state={}, error_code=code, retryable=can_retry
    )
