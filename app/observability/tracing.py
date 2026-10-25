"""Optional OpenTelemetry tracing.

Enabled when OTEL_EXPORTER_OTLP_ENDPOINT is set and the ``otel`` extra is installed
(``pip install .[otel]``). Otherwise this is a no-op, so tracing never blocks startup.
"""

from __future__ import annotations

from fastapi import FastAPI

from app.core.config import Settings
from app.core.logging import get_logger

logger = get_logger(__name__)


def setup_tracing(app: FastAPI, settings: Settings) -> bool:
    if not settings.otel_exporter_otlp_endpoint:
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        logger.warning("otel_packages_missing", hint="pip install .[otel]")
        return False
    provider = TracerProvider(resource=Resource.create({"service.name": settings.otel_service_name}))
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{settings.otel_exporter_otlp_endpoint}/v1/traces"))
    )
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app, excluded_urls="health,metrics")
    logger.info("otel_tracing_enabled")
    return True
