"""Account connection: OAuth state/PKCE validation, callback handling, isolation, disconnect."""

from urllib.parse import parse_qs, urlparse

from sqlalchemy import select

from app.core.encryption import get_cipher
from app.db.models import AuditLog, EmailAccount
from app.services.dispatcher import SYNC_ACCOUNT
from tests.conftest import register_and_login


async def start(client, headers):
    r = await client.post("/api/v1/accounts/gmail/connect", headers=headers)
    assert r.status_code == 200, r.text
    q = parse_qs(urlparse(r.json()["authorization_url"]).query)
    return q["state"][0], q["code_challenge"][0]


async def test_gmail_oauth_flow_links_account_with_encrypted_tokens(client, auth_headers, provider_factory,
                                                                   dispatcher, sessionmaker):
    state, challenge = await start(client, auth_headers)
    assert len(state) > 30 and challenge
    r = await client.get(f"/api/v1/accounts/gmail/callback?state={state}&code=auth-code")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "connected"
    exchange = provider_factory.gmail.exchanged[0]
    assert exchange["code"] == "auth-code" and exchange["redirect_uri"].endswith("/api/v1/accounts/gmail/callback")
    assert len(exchange["verifier"]) >= 43  # PKCE verifier kept server-side

    accounts = (await client.get("/api/v1/accounts", headers=auth_headers)).json()
    assert len(accounts) == 1
    acc = accounts[0]
    assert acc["email_address"] == "owner@gmail.com" and acc["can_send"] is True
    assert "encrypted_access_token" not in acc and "access-1" not in r.text
    assert SYNC_ACCOUNT in dispatcher.names()

    async with sessionmaker() as s:
        row = (await s.execute(select(EmailAccount))).scalar_one()
        assert row.encrypted_access_token != "access-1"
        assert get_cipher().decrypt(row.encrypted_refresh_token) == "refresh-1"
        audit = (await s.execute(select(AuditLog).where(AuditLog.action == "account.connect"))).scalar_one()
        assert audit.outcome.value == "success"


async def test_state_is_single_use(client, auth_headers):
    state, _ = await start(client, auth_headers)
    assert (await client.get(f"/api/v1/accounts/gmail/callback?state={state}&code=c")).json()["status"] == "connected"
    replay = await client.get(f"/api/v1/accounts/gmail/callback?state={state}&code=c")
    assert replay.status_code == 400 and replay.json()["error_code"] == "invalid_state"


async def test_callback_rejects_forged_or_missing_state(client, auth_headers, provider_factory):
    for query in ("state=forged-state-value&code=c", "code=c"):
        r = await client.get(f"/api/v1/accounts/gmail/callback?{query}")
        assert r.status_code == 400 and r.json()["error_code"] == "invalid_state"
    assert provider_factory.gmail.exchanged == []


async def test_callback_rejects_provider_mismatch(client, auth_headers):
    state, _ = await start(client, auth_headers)
    r = await client.get(f"/api/v1/accounts/microsoft/callback?state={state}&code=c")
    assert r.status_code == 400 and r.json()["error_code"] == "provider_mismatch"


async def test_user_denied_consent(client, auth_headers):
    state, _ = await start(client, auth_headers)
    r = await client.get(f"/api/v1/accounts/gmail/callback?state={state}&error=access_denied")
    assert r.status_code == 400 and r.json()["error_code"] == "access_denied"


async def test_insufficient_scope_is_rejected_and_revoked(client, auth_headers, provider_factory):
    provider_factory.gmail.granted = ["https://www.googleapis.com/auth/gmail.send"]
    state, _ = await start(client, auth_headers)
    r = await client.get(f"/api/v1/accounts/gmail/callback?state={state}&code=c")
    assert r.json()["error_code"] == "insufficient_scope"
    assert provider_factory.gmail.revoked == ["refresh-1"]


async def test_mailbox_cannot_be_linked_to_second_user(client, auth_headers, other_headers):
    state, _ = await start(client, auth_headers)
    await client.get(f"/api/v1/accounts/gmail/callback?state={state}&code=c")
    state2, _ = await start(client, other_headers)
    r = await client.get(f"/api/v1/accounts/gmail/callback?state={state2}&code=c")
    assert r.status_code == 400 and r.json()["error_code"] == "account_in_use"
    assert (await client.get("/api/v1/accounts", headers=other_headers)).json() == []


async def test_callback_redirects_to_frontend_when_configured(client, auth_headers, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "frontend_url", "https://app.example")
    state, _ = await start(client, auth_headers)
    r = await client.get(f"/api/v1/accounts/gmail/callback?state={state}&code=c")
    assert r.status_code == 303
    assert r.headers["location"].startswith("https://app.example/oauth/result?status=connected&account_id=")


async def test_accounts_are_isolated_between_users(client, mailbox, other_headers, auth_headers):
    account_id = mailbox["id"]
    for method, path in (("GET", f"/api/v1/accounts/{account_id}"), ("DELETE", f"/api/v1/accounts/{account_id}"),
                         ("POST", f"/api/v1/accounts/{account_id}/sync"),
                         ("GET", f"/api/v1/accounts/{account_id}/sync-status")):
        r = await client.request(method, path, headers=other_headers)
        assert r.status_code == 404, (method, path)
    assert (await client.get(f"/api/v1/accounts/{account_id}", headers=auth_headers)).status_code == 200


async def test_sync_endpoints(client, mailbox, auth_headers, dispatcher):
    account_id = mailbox["id"]
    status = (await client.get(f"/api/v1/accounts/{account_id}/sync-status", headers=auth_headers)).json()
    assert status["incremental_sync_ready"] and status["last_task"]["status"] == "succeeded"
    r = await client.post(f"/api/v1/accounts/{account_id}/sync", headers=auth_headers)
    assert r.status_code == 202
    body = r.json()
    assert body["task_type"] == "incremental_sync" and body["status_url"].endswith(body["task_id"])
    again = await client.post(f"/api/v1/accounts/{account_id}/sync", headers=auth_headers)
    assert again.status_code == 409 and again.json()["error"]["code"] == "sync_in_progress"


async def test_disconnect_deletes_account_and_data(client, mailbox, auth_headers):
    account_id = mailbox["id"]
    assert (await client.get("/api/v1/emails", headers=auth_headers)).json()["total"] == 7
    r = await client.delete(f"/api/v1/accounts/{account_id}", headers=auth_headers)
    assert r.status_code == 204
    assert (await client.get(f"/api/v1/accounts/{account_id}", headers=auth_headers)).status_code == 404
    assert (await client.get("/api/v1/emails", headers=auth_headers)).json()["total"] == 0


async def test_sandbox_connect_twice_conflicts(client, mailbox, auth_headers):
    r = await client.post("/api/v1/accounts/sandbox/connect", headers=auth_headers)
    assert r.status_code == 409


async def test_unknown_provider(client, auth_headers):
    r = await client.post("/api/v1/accounts/yahoo/connect", headers=auth_headers)
    assert r.status_code == 422 and r.json()["error"]["code"] == "unsupported_provider"


async def test_oauth_connect_is_rate_limited(client):
    headers = await register_and_login(client, "rl@example.com")
    codes = [(await client.post("/api/v1/accounts/gmail/connect", headers=headers)).status_code for _ in range(21)]
    assert codes[-1] == 429 and codes[0] == 200
