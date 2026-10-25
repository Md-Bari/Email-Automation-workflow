from __future__ import annotations

import pytest

from tests.helpers import drain


@pytest.fixture
async def mailbox(client, auth_headers, dispatcher, sessionmaker, store):
    """Alice with a connected sandbox account, synced and fully analyzed (worker run inline)."""
    r = await client.post("/api/v1/accounts/sandbox/connect", headers=auth_headers)
    assert r.status_code == 201, r.text
    await drain(dispatcher, sessionmaker, store)
    return r.json()


async def emails_by_subject(client, headers) -> dict[str, dict]:
    r = await client.get("/api/v1/emails?page_size=100", headers=headers)
    assert r.status_code == 200, r.text
    return {e["subject"]: e for e in r.json()["items"]}
