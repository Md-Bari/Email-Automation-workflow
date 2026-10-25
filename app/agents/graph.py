"""The email analysis workflow as a LangGraph state graph.

    load_email ─► screen_email ─► analyze_email ─► apply_policy ─► persist_analysis
         │              │                                               │
         └── skip ──────┴────────────────► finalize ◄───────────────────┤
                                              ▲              emergency / rejected claim
                                              │                         ▼
                                 generate_reply_draft ◄── dispatch ◄── create_notification
                                   (reply recommended)

Design notes:
* One structured LLM call produces summary, intent, priority and the emergency assessment;
  deterministic policy (``rules.apply_policy``) then verifies and calibrates it.
* Emergency notifications are created *before* draft generation so alerts are never delayed.
* Fetch/normalize/deduplicate happen in the sync job, which stores the email before this graph runs.
* Recovery: every node is idempotent and progress is persisted on the email row
  (``processing_status``), so a crashed run is safely re-executed by the Celery retry.
"""

from __future__ import annotations

from functools import lru_cache

from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy

from app.agents.nodes.actions import (
    create_notification,
    dispatch_notifications,
    finalize,
    generate_reply_draft,
)
from app.agents.nodes.analysis import (
    analyze_email,
    apply_policy_node,
    load_email,
    persist_analysis,
    screen_email,
)
from app.agents.state import AnalysisState
from app.integrations.llm.base import LLMOutputError, LLMTransientError

LLM_RETRY = RetryPolicy(
    initial_interval=1.0, backoff_factor=2.0, max_interval=20.0, max_attempts=3,
    retry_on=(LLMTransientError, LLMOutputError),
)


def _after_load(state: AnalysisState) -> str:
    return "finalize" if state.get("skip_reason") else "screen_email"


def _after_screen(state: AnalysisState) -> str:
    return "finalize" if state.get("skip_reason") else "analyze_email"


def _after_persist(state: AnalysisState) -> str:
    decision = state["decision"]
    if decision["is_emergency"] or decision["emergency_claim_rejected"]:
        return "create_notification"
    return "generate_reply_draft" if decision["reply_recommended"] else "finalize"


def _after_dispatch(state: AnalysisState) -> str:
    return "generate_reply_draft" if state["decision"]["reply_recommended"] else "finalize"


def build_graph():
    graph = StateGraph(AnalysisState)
    graph.add_node("load_email", load_email)
    graph.add_node("screen_email", screen_email)
    graph.add_node("analyze_email", analyze_email, retry_policy=LLM_RETRY)
    graph.add_node("apply_policy", apply_policy_node)
    graph.add_node("persist_analysis", persist_analysis)
    graph.add_node("create_notification", create_notification)
    graph.add_node("dispatch_notifications", dispatch_notifications)
    graph.add_node("generate_reply_draft", generate_reply_draft)
    graph.add_node("finalize", finalize)

    graph.add_edge(START, "load_email")
    graph.add_conditional_edges("load_email", _after_load, ["screen_email", "finalize"])
    graph.add_conditional_edges("screen_email", _after_screen, ["analyze_email", "finalize"])
    graph.add_edge("analyze_email", "apply_policy")
    graph.add_edge("apply_policy", "persist_analysis")
    graph.add_conditional_edges(
        "persist_analysis", _after_persist, ["create_notification", "generate_reply_draft", "finalize"]
    )
    graph.add_edge("create_notification", "dispatch_notifications")
    graph.add_conditional_edges("dispatch_notifications", _after_dispatch, ["generate_reply_draft", "finalize"])
    graph.add_edge("generate_reply_draft", "finalize")
    graph.add_edge("finalize", END)
    return graph.compile()


@lru_cache
def get_graph():
    return build_graph()
