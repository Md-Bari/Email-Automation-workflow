"""Gmail REST integration against mocked HTTP (respx). No network or credentials."""

from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from app.core.config import get_settings
from app.integrations import http as http_mod
from app.integrations.email_providers.base import (
    CredentialsRevokedError,
    CursorExpiredError,
    InsufficientScopeError,
    MessageNotFoundError,
    OutgoingReply,
    ProviderAuthError,
    ProviderRequestError,
    ProviderUnavailableError,
)
from app.integrations.email_providers.gmail import API, TOKEN_URL, GmailProvider


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def _sleep(_):
        return None
    monkeypatch.setattr(http_mod.asyncio, "sleep", _sleep)


@pytest.fixture
def gmail():
    return GmailProvider(get_settings())


def test_authorization_url_has_pkce_state_and_minimal_scopes(gmail):
    url = gmail.authorization_url(state="st", code_challenge="ch", redirect_uri="http://localhost/cb")
    q = parse_qs(urlparse(url).query)
    assert q["state"] == ["st"] and q["code_challenge"] == ["ch"] and q["code_challenge_method"] == ["S256"]
    assert q["access_type"] == ["offline"]
    assert set(q["scope"][0].split()) == {
        "https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/gmail.send"}


@respx.mock
async def test_exchange_code_sends_verifier(gmail):
    route = respx.post(TOKEN_URL).respond(200, json={
        "access_token": "a", "refresh_token": "r", "expires_in": 3600,
        "scope": "https://www.googleapis.com/auth/gmail.readonly"})
    tokens = await gmail.exchange_code(code="c", code_verifier="v", redirect_uri="http://cb")
    assert tokens.refresh_token == "r" and tokens.scopes == ["https://www.googleapis.com/auth/gmail.readonly"]
    assert b"code_verifier=v" in route.calls.last.request.content


@respx.mock
async def test_refresh_invalid_grant_means_revoked(gmail):
    respx.post(TOKEN_URL).respond(400, json={"error": "invalid_grant"})
    with pytest.raises(CredentialsRevokedError):
        await gmail.refresh("r")


@respx.mock
async def test_history_404_means_cursor_expired(gmail):
    respx.get(f"{API}/history").respond(404, json={"error": {"code": 404}})
    with pytest.raises(CursorExpiredError):
        await gmail.list_changes("tok", "123")


@respx.mock
async def test_history_paginates_and_dedupes(gmail):
    respx.get(f"{API}/history").mock(side_effect=[
        httpx.Response(200, json={"history": [{"messagesAdded": [{"message": {"id": "a"}}, {"message": {"id": "b"}}]}],
                                  "nextPageToken": "p2", "historyId": "200"}),
        httpx.Response(200, json={"history": [{"messagesAdded": [{"message": {"id": "a"}}],
                                               "messagesDeleted": [{"message": {"id": "b"}}]}], "historyId": "201"}),
    ])
    changes = await gmail.list_changes("tok", "100")
    assert changes.added_ids == ["a"] and changes.deleted_ids == ["b"] and changes.new_cursor == "201"


@respx.mock
async def test_transient_errors_are_retried_with_backoff(gmail):
    route = respx.get(f"{API}/profile").mock(side_effect=[
        httpx.Response(503), httpx.Response(429, headers={"Retry-After": "1"}),
        httpx.Response(200, json={"emailAddress": "Me@Gmail.com", "historyId": "42"}),
    ])
    profile = await gmail.get_profile("tok")
    assert profile.email_address == "me@gmail.com" and profile.cursor == "42"
    assert route.call_count == 3


@respx.mock
async def test_persistent_outage_raises_unavailable(gmail):
    respx.get(f"{API}/profile").respond(503)
    with pytest.raises(ProviderUnavailableError):
        await gmail.get_profile("tok")


@respx.mock
async def test_quota_403_is_retryable_but_scope_403_is_not(gmail):
    respx.get(f"{API}/profile").respond(403, json={"error": {"errors": [{"reason": "userRateLimitExceeded"}]}})
    with pytest.raises(ProviderUnavailableError):
        await gmail.get_profile("tok")
    respx.get(f"{API}/messages/x").respond(403, json={"error": {"errors": [{"reason": "insufficientPermissions"}]}})
    with pytest.raises(InsufficientScopeError):
        await gmail.get_message("tok", "x")


@respx.mock
async def test_401_and_404_mapping(gmail):
    respx.get(f"{API}/profile").respond(401)
    with pytest.raises(ProviderAuthError):
        await gmail.get_profile("tok")
    respx.get(f"{API}/messages/gone").respond(404)
    with pytest.raises(MessageNotFoundError):
        await gmail.get_message("tok", "gone")


@respx.mock
async def test_send_is_never_retried(gmail):
    route = respx.post(f"{API}/messages/send").respond(503)
    reply = OutgoingReply(to="dana@example.com", subject="Re: x", body="hi", from_address="me@gmail.com",
                          thread_id="t", in_reply_to="<a@b>", references=None, original_provider_message_id="m")
    with pytest.raises(ProviderRequestError):
        await gmail.send_reply("tok", reply)
    assert route.call_count == 1
