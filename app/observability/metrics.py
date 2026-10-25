"""Prometheus metrics (exposed at /metrics when METRICS_ENABLED=true)."""

from __future__ import annotations

from prometheus_client import Counter, Histogram

HTTP_REQUESTS = Counter("http_requests_total", "HTTP requests", ["method", "route", "status"])
HTTP_LATENCY = Histogram("http_request_duration_seconds", "HTTP request latency", ["method", "route"])
EMAILS_SYNCED = Counter("emails_synced_total", "New emails stored by synchronization", ["provider"])
ANALYSES = Counter("email_analyses_total", "Email analyses by outcome", ["outcome"])
EMERGENCIES = Counter("emergencies_detected_total", "Emails confirmed as emergencies")
LLM_FAILURES = Counter("llm_failures_total", "LLM call failures", ["code"])
DRAFTS_SENT = Counter("drafts_sent_total", "Approved replies sent", ["outcome"])
NOTIFICATION_DELIVERIES = Counter(
    "notification_deliveries_total", "External notification deliveries", ["channel", "outcome"]
)
