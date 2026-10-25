"""FastAPI application factory."""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app import __version__
from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging, correlation_id_var, get_logger
from app.observability import metrics
from app.observability.tracing import setup_tracing

logger = get_logger(__name__)
_REQUEST_ID = re.compile(r"^[A-Za-z0-9\-_.]{8,64}$")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logger.info("startup", environment=settings.environment, llm_provider=settings.llm_provider,
                sandbox_mode=settings.sandbox_mode, gmail_enabled=settings.gmail_enabled)
    yield
    from app.db.session import get_engine

    await get_engine().dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    docs_enabled = settings.environment != "production" or settings.debug
    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        description=(
            "Backend API for an agentic AI email assistant: Gmail OAuth, synchronization, LangGraph "
            "analysis, emergency notifications and human-approved reply drafts."
        ),
        openapi_url=f"{settings.api_v1_prefix}/openapi.json" if docs_enabled else None,
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-ID"],
        expose_headers=["X-Request-ID", "Retry-After"],
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        incoming = request.headers.get("x-request-id", "")
        cid = incoming if _REQUEST_ID.match(incoming) else uuid.uuid4().hex
        token = correlation_id_var.set(cid)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            correlation_id_var.reset(token)
        route = request.scope.get("route")
        route_path = getattr(route, "path", "unmatched")
        metrics.HTTP_REQUESTS.labels(request.method, route_path, response.status_code).inc()
        metrics.HTTP_LATENCY.labels(request.method, route_path).observe(time.perf_counter() - start)
        response.headers["X-Request-ID"] = cid
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = response.headers.get("Cache-Control", "no-store")
        if settings.environment == "production":
            response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        return response

    register_exception_handlers(app)
    app.include_router(api_router, prefix=settings.api_v1_prefix)

    if settings.metrics_enabled:
        @app.get("/metrics", include_in_schema=False)
        async def prometheus_metrics() -> Response:
            return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    setup_tracing(app, settings)
    return app


app = create_app()
