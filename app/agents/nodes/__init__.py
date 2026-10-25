"""LangGraph nodes. Each node is a small, separately testable async function.

Dependencies (DB session, LLM, dispatcher) are injected through ``config["configurable"]["deps"]``.
"""
