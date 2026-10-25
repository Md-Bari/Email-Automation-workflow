"""Shared fixtures. Tests run against in-memory SQLite, an in-memory KV store, a recording task
dispatcher, the sandbox mailbox and the deterministic mock LLM - no real credentials needed."""

from __future__ import annotations

import os

from cryptography.fernet import Fernet

# Must be set before the application settings are first loaded.
os.environ.update(
    {
        "ENVIRONMENT": "test",
        "LOG_JSON": "false",
        "LOG_LEVEL": "WARNING",
        "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
        "REDIS_URL": "redis://localhost:6399/0",
        "LLM_PROVIDER": "mock",
        "SANDBOX_MODE": "true",
        "FRONTEND_URL": "",
        "TOKEN_ENCRYPTION_KEYS": Fernet.generate_key().decode(),
        "JWT_SECRET_KEY": "test-secret-key-that-is-long-enough-for-hs256-usage",
        "GOOGLE_CLIENT_ID": "test-client-id",
        "GOOGLE_CLIENT_SECRET": "test-client-secret",
        "METRICS_ENABLED": "true",
    }
)

from collections.abc import AsyncIterator  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402
from sqlalchemy import event  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from app.core.config import get_settings  # noqa: E402

get_settings.cache_clear()

from app.core.kvstore import MemoryStore, get_kv_store  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.session import get_db  # noqa: E402
from app.integrations.email_providers.factory import set_provider_factory  # noqa: E402
from app.integrations.email_providers.sandbox import SandboxProvider  # noqa: E402
from app.integrations.llm.factory import set_llm_provider  # noqa: E402
from app.integrations.llm.mock_provider import MockLLMProvider  # noqa: E402
from app.main import create_app  # noqa: E402
from app.services.dispatcher import RecordingDispatcher, set_dispatcher  # noqa: E402
from tests.fixtures.providers import TestProviderFactory  # noqa: E402


@pytest.fixture
async def engine(tmp_path):
    # A file database (one per test) gives every session its own connection, like PostgreSQL;
    # WAL lets readers and writers in different sessions proceed concurrently.
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}", poolclass=NullPool)

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _):  # pragma: no cover - driver hook
        dbapi_connection.isolation_level = None  # let SQLAlchemy manage BEGIN (needed for SAVEPOINT)
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    @event.listens_for(engine.sync_engine, "begin")
    def _begin(conn):  # pragma: no cover - driver hook
        conn.exec_driver_sql("BEGIN")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
def sessionmaker(engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


@pytest.fixture
async def session(sessionmaker) -> AsyncIterator[AsyncSession]:
    async with sessionmaker() as s:
        yield s


@pytest.fixture
def store() -> MemoryStore:
    return MemoryStore()


@pytest.fixture
def dispatcher() -> AsyncIterator[RecordingDispatcher]:
    d = RecordingDispatcher()
    set_dispatcher(d)
    yield d
    set_dispatcher(None)


@pytest.fixture(autouse=True)
def _test_doubles():
    factory = TestProviderFactory(get_settings())
    set_provider_factory(factory)
    set_llm_provider(MockLLMProvider())
    SandboxProvider.incoming.clear()
    SandboxProvider.sent.clear()
    yield factory
    set_provider_factory(None)
    set_llm_provider(None)


@pytest.fixture
def provider_factory(_test_doubles) -> TestProviderFactory:
    return _test_doubles


@pytest.fixture
def app(sessionmaker, store, dispatcher):
    application = create_app()

    async def _db() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as s:
            yield s

    application.dependency_overrides[get_db] = _db
    application.dependency_overrides[get_kv_store] = lambda: store
    return application


@pytest.fixture
async def client(app) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def register_and_login(client: httpx.AsyncClient, email: str, password: str = "correct-horse-battery") -> dict:
    r = await client.post("/api/v1/auth/register", json={"email": email, "password": password})
    assert r.status_code == 201, r.text
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
async def auth_headers(client) -> dict:
    return await register_and_login(client, "alice@example.com")


@pytest.fixture
async def other_headers(client) -> dict:
    return await register_and_login(client, "mallory@example.com")
