"""Redis store semantics (via fakeredis) plus optional real PostgreSQL/Redis checks.

Real-infrastructure tests run only when TEST_DATABASE_URL / TEST_REDIS_URL are set, e.g. against
``docker compose up -d postgres redis``.
"""

import os
import uuid

import pytest
from fakeredis import FakeAsyncRedis

from app.core.kvstore import RedisStore


@pytest.fixture
def redis_store():
    return RedisStore(FakeAsyncRedis())


async def test_redis_store_pop_is_single_use(redis_store):
    await redis_store.set("k", "v", 60)
    assert await redis_store.pop("k") == "v"
    assert await redis_store.pop("k") is None


async def test_redis_store_counter_and_lock(redis_store):
    assert (await redis_store.incr("c", 60))[0] == 1
    count, ttl = await redis_store.incr("c", 60)
    assert count == 2 and 0 < ttl <= 60
    assert await redis_store.acquire_lock("l", 60)
    assert not await redis_store.acquire_lock("l", 60)
    await redis_store.delete("l")
    assert await redis_store.acquire_lock("l", 60)
    assert await redis_store.ping()


@pytest.mark.integration
@pytest.mark.skipif(not os.getenv("TEST_REDIS_URL"), reason="TEST_REDIS_URL not set")
async def test_real_redis_store():
    from redis.asyncio import Redis

    store = RedisStore(Redis.from_url(os.environ["TEST_REDIS_URL"]))
    key = f"test:{uuid.uuid4().hex}"
    await store.set(key, "1", 30)
    assert await store.pop(key) == "1" and await store.pop(key) is None
    assert await store.ping()


@pytest.mark.integration
@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set")
async def test_postgres_migrations_and_unique_message_constraint():
    from alembic.config import Config
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from alembic import command
    from app.db.base import utcnow
    from app.db.models import Email, EmailAccount, User
    from app.db.models.enums import EmailProvider

    url = os.environ["TEST_DATABASE_URL"]
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    engine = create_async_engine(url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        user = User(email=f"{uuid.uuid4().hex}@example.com")
        s.add(user)
        await s.flush()
        account = EmailAccount(user_id=user.id, provider=EmailProvider.SANDBOX,
                               email_address=f"{uuid.uuid4().hex}@sandbox.local", granted_scopes=[])
        s.add(account)
        await s.flush()
        common = dict(user_id=user.id, email_account_id=account.id, provider_message_id="m1",
                      sender_email="a@b.example", received_at=utcnow(), recipients={}, provider_labels=[],
                      attachment_metadata=[])
        s.add(Email(**common))
        await s.commit()
        s.add(Email(**common))
        with pytest.raises(IntegrityError):
            await s.commit()
        await s.rollback()
        await s.delete(user)
        await s.commit()
    await engine.dispose()
