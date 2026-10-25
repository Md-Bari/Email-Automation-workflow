from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


class AnalysisState(TypedDict, total=False):
    # Inputs
    email_id: str
    force: bool
    correlation_id: str | None
    # Working data (JSON-serializable so the state can be logged/checkpointed)
    signals: dict[str, Any]
    assessment: dict[str, Any]
    decision: dict[str, Any]
    analysis_id: str
    notification_id: str | None
    delivery_ids: list[str]
    draft_id: str | None
    draft_error: str | None
    # Control
    skip_reason: str | None
    status: str
    # Ordered list of visited nodes (appended by each node via the reducer).
    trace: Annotated[list[str], operator.add]
