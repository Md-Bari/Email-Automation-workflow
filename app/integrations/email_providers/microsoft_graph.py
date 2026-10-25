"""Microsoft Graph (Outlook / Microsoft 365) provider.

Implemented against the documented Graph v1.0 REST API, exercised by mocked tests only;
live behaviour is unverified until real Azure app credentials are configured.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx

from app.core.config import Settings
from app.integrations.email_providers.base import (
    Address,
    ChangeSet,
    CredentialsRevokedError,
    CursorExpiredError,
    EmailProviderClient,
    InsufficientScopeError,
    MailboxProfile,
    MessageNotFoundError,
    OutgoingReply,
    ProviderAuthError,
    ProviderMessage,
    ProviderRequestError,
    ProviderUnavailableError,
    TokenSet,
)
from app.integrations.email_providers.parsing import detect_automated, html_to_text, normalize_text, truncate
from app.integrations.http import RETRYABLE_STATUS, request_with_backoff

GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = ["offline_access", "User.Read", "Mail.Read", "Mail.Send"]
_SELECT = (
    "id,conversationId,internetMessageId,subject,from,replyTo,toRecipients,ccRecipients,"
    "receivedDateTime,body,bodyPreview,hasAttachments,categories,internetMessageHeaders"
)


def _addr(obj: dict[str, Any] | None) -> Address | None:
    if not obj:
        return None
    ea = obj.get("emailAddress", {})
    email = (ea.get("address") or "").lower()
    return Address(email=email, name=ea.get("name")) if "@" in email else None


class MicrosoftGraphProvider(EmailProviderClient):
    name = "microsoft"
    read_scope = "Mail.Read"
    send_scope = "Mail.Send"

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        if not settings.microsoft_enabled:
            raise ProviderRequestError("Microsoft OAuth is not configured (MICROSOFT_CLIENT_ID/SECRET)")
        self.settings = settings
        self.client_id = settings.microsoft_client_id or ""
        self.client_secret = (
            settings.microsoft_client_secret.get_secret_value() if settings.microsoft_client_secret else ""
        )
        self.authority = f"https://login.microsoftonline.com/{settings.microsoft_tenant}/oauth2/v2.0"
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self.settings.provider_timeout_seconds, transport=self._transport)

    def authorization_url(self, *, state: str, code_challenge: str, redirect_uri: str) -> str:
        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "response_mode": "query",
            "scope": " ".join(SCOPES),
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "prompt": "select_account",
        }
        return f"{self.authority}/authorize?{urlencode(params)}"

    async def _token(self, data: dict[str, str]) -> TokenSet:
        data = {**data, "client_id": self.client_id, "client_secret": self.client_secret, "scope": " ".join(SCOPES)}
        async with self._client() as client:
            try:
                response = await request_with_backoff(
                    client, "POST", f"{self.authority}/token", data=data,
                    max_retries=self.settings.provider_max_retries,
                )
            except httpx.HTTPError as exc:
                raise ProviderUnavailableError("Microsoft identity platform unreachable") from exc
        if response.status_code == 400 and response.json().get("error") in {"invalid_grant", "interaction_required"}:
            raise CredentialsRevokedError()
        if response.status_code in RETRYABLE_STATUS:
            raise ProviderUnavailableError()
        if response.status_code != 200:
            raise ProviderRequestError(f"Token request failed ({response.status_code})", response.status_code)
        body = response.json()
        return TokenSet(
            access_token=body["access_token"],
            refresh_token=body.get("refresh_token"),
            expires_at=datetime.now(UTC) + timedelta(seconds=int(body.get("expires_in", 3600))),
            scopes=body.get("scope", "").split(),
        )

    async def exchange_code(self, *, code: str, code_verifier: str, redirect_uri: str) -> TokenSet:
        return await self._token(
            {"grant_type": "authorization_code", "code": code, "code_verifier": code_verifier,
             "redirect_uri": redirect_uri}
        )

    async def refresh(self, refresh_token: str) -> TokenSet:
        return await self._token({"grant_type": "refresh_token", "refresh_token": refresh_token})

    async def revoke(self, token: str) -> None:
        # Graph has no per-token revocation endpoint for delegated tokens; local deletion suffices.
        return None

    async def _api(self, access_token: str, method: str, url: str, **kwargs: Any) -> httpx.Response:
        if not url.startswith("https://"):
            url = f"{GRAPH}{url}"
        async with self._client() as client:
            try:
                response = await request_with_backoff(
                    client, method, url, headers={"Authorization": f"Bearer {access_token}"},
                    max_retries=self.settings.provider_max_retries, **kwargs,
                )
            except httpx.HTTPError as exc:
                raise ProviderUnavailableError("Microsoft Graph unreachable") from exc
        if response.status_code == 401:
            raise ProviderAuthError()
        if response.status_code == 403:
            raise InsufficientScopeError()
        if response.status_code in RETRYABLE_STATUS:
            raise ProviderUnavailableError()
        return response

    async def get_profile(self, access_token: str) -> MailboxProfile:
        response = await self._api(access_token, "GET", "/me?$select=mail,userPrincipalName")
        body = response.json()
        return MailboxProfile(email_address=(body.get("mail") or body["userPrincipalName"]).lower(), cursor=None)

    async def list_recent_message_ids(
        self, access_token: str, *, newer_than_days: int, max_messages: int
    ) -> list[str]:
        since = (datetime.now(UTC) - timedelta(days=newer_than_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        url: str | None = (
            f"/me/mailFolders/inbox/messages?$select=id&$orderby=receivedDateTime desc"
            f"&$filter=receivedDateTime ge {since}&$top={min(self.settings.sync_page_size, max_messages)}"
        )
        ids: list[str] = []
        while url and len(ids) < max_messages:
            body = (await self._api(access_token, "GET", url)).json()
            ids.extend(m["id"] for m in body.get("value", []))
            url = body.get("@odata.nextLink")
        return ids[:max_messages]

    async def initial_cursor(self, access_token: str) -> str | None:
        """Walk the inbox delta once to obtain a deltaLink for future incremental syncs."""
        url: str | None = "/me/mailFolders/inbox/messages/delta?$select=id"
        while url:
            body = (await self._api(access_token, "GET", url)).json()
            if "@odata.deltaLink" in body:
                return body["@odata.deltaLink"]
            url = body.get("@odata.nextLink")
        return None

    async def list_changes(self, access_token: str, cursor: str) -> ChangeSet:
        added: list[str] = []
        deleted: list[str] = []
        url: str | None = cursor
        new_cursor = cursor
        while url:
            response = await self._api(access_token, "GET", url)
            if response.status_code == 410:
                raise CursorExpiredError()
            if response.status_code >= 400:
                raise ProviderRequestError(f"Graph delta failed ({response.status_code})", response.status_code)
            body = response.json()
            for item in body.get("value", []):
                (deleted if "@removed" in item else added).append(item["id"])
            url = body.get("@odata.nextLink")
            new_cursor = body.get("@odata.deltaLink", new_cursor)
        return ChangeSet(added_ids=list(dict.fromkeys(added)), deleted_ids=deleted, new_cursor=new_cursor)

    async def get_message(self, access_token: str, message_id: str) -> ProviderMessage:
        response = await self._api(access_token, "GET", f"/me/messages/{message_id}?$select={_SELECT}")
        if response.status_code == 404:
            raise MessageNotFoundError(message_id)
        if response.status_code >= 400:
            raise ProviderRequestError(f"Graph error ({response.status_code})", response.status_code)
        m = response.json()
        body = m.get("body", {})
        content = body.get("content", "")
        text = html_to_text(content) if body.get("contentType") == "html" else normalize_text(content)
        text, _ = truncate(text, self.settings.max_body_chars)
        headers = {h["name"]: h["value"] for h in m.get("internetMessageHeaders", []) or []}
        sender = _addr(m.get("from")) or Address(email="unknown@invalid")
        reply_to = next((a for a in (_addr(r) for r in m.get("replyTo", [])) if a), None)
        return ProviderMessage(
            provider_message_id=m["id"],
            thread_id=m.get("conversationId"),
            sender=sender,
            to=[a for a in (_addr(r) for r in m.get("toRecipients", [])) if a],
            cc=[a for a in (_addr(r) for r in m.get("ccRecipients", [])) if a],
            reply_to=reply_to.email if reply_to else None,
            subject=(m.get("subject") or "")[:998],
            body_text=text,
            received_at=datetime.fromisoformat(m["receivedDateTime"].replace("Z", "+00:00")),
            internet_message_id=m.get("internetMessageId"),
            references=headers.get("References"),
            labels=list(m.get("categories", [])),
            attachments=[{"has_attachments": True}] if m.get("hasAttachments") else [],
            snippet=(m.get("bodyPreview") or "")[:500] or None,
            is_automated=detect_automated(headers, sender.email),
        )

    async def send_reply(self, access_token: str, reply: OutgoingReply) -> str | None:
        payload = {
            "message": {"toRecipients": [{"emailAddress": {"address": reply.to}}], "subject": reply.subject},
            "comment": reply.body,
        }
        async with self._client() as client:
            try:
                response = await client.post(
                    f"{GRAPH}/me/messages/{reply.original_provider_message_id}/reply",
                    json=payload, headers={"Authorization": f"Bearer {access_token}"},
                )
            except httpx.TimeoutException as exc:
                raise ProviderRequestError("Send timed out; delivery outcome unknown", None) from exc
            except httpx.HTTPError as exc:
                raise ProviderUnavailableError("Microsoft Graph unreachable") from exc
        if response.status_code == 401:
            raise ProviderAuthError()
        if response.status_code == 403:
            raise InsufficientScopeError("The account did not grant permission to send email")
        if response.status_code >= 400:
            raise ProviderRequestError(f"Graph send failed ({response.status_code})", response.status_code)
        return None  # Graph's reply endpoint returns 202 without the sent message id.
