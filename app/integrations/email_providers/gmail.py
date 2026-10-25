"""Gmail API provider (OAuth 2.0 + PKCE, REST over async httpx)."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx

from app.core.config import Settings
from app.integrations.email_providers.base import (
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
from app.integrations.email_providers.parsing import (
    Address,
    build_reply_mime,
    decode_base64url,
    detect_automated,
    html_to_text,
    normalize_text,
    parse_address,
    parse_address_list,
    truncate,
)
from app.integrations.http import RETRYABLE_STATUS, request_with_backoff

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - URL, not a secret
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API = "https://gmail.googleapis.com/gmail/v1/users/me"

GMAIL_READONLY = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_SEND = "https://www.googleapis.com/auth/gmail.send"
_RATE_LIMIT_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "backendError"}


def _error_reason(response: httpx.Response) -> str | None:
    try:
        errors = response.json().get("error", {}).get("errors", [])
        return errors[0].get("reason") if errors else None
    except (ValueError, AttributeError):
        return None


def _is_retryable(response: httpx.Response) -> bool:
    return response.status_code == 403 and _error_reason(response) in _RATE_LIMIT_REASONS


class GmailProvider(EmailProviderClient):
    name = "gmail"
    read_scope = GMAIL_READONLY
    send_scope = GMAIL_SEND

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        if not settings.gmail_enabled:
            raise ProviderRequestError("Gmail OAuth is not configured (GOOGLE_CLIENT_ID/SECRET)")
        self.settings = settings
        self.client_id = settings.google_client_id or ""
        self.client_secret = settings.google_client_secret.get_secret_value() if settings.google_client_secret else ""
        self.scopes = settings.gmail_scopes
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self.settings.provider_timeout_seconds, transport=self._transport)

    # --- OAuth -------------------------------------------------------------------
    def authorization_url(self, *, state: str, code_challenge: str, redirect_uri: str) -> str:
        params = {
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(self.scopes),
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "false",
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        return f"{AUTH_URL}?{urlencode(params)}"

    async def _token_request(self, data: dict[str, str]) -> TokenSet:
        async with self._client() as client:
            try:
                response = await request_with_backoff(
                    client, "POST", TOKEN_URL, data=data, max_retries=self.settings.provider_max_retries
                )
            except httpx.HTTPError as exc:
                raise ProviderUnavailableError("Google token endpoint unreachable") from exc
        if response.status_code == 400 and response.json().get("error") == "invalid_grant":
            raise CredentialsRevokedError()
        if response.status_code in RETRYABLE_STATUS:
            raise ProviderUnavailableError()
        if response.status_code != 200:
            raise ProviderRequestError(f"Token request failed ({response.status_code})", response.status_code)
        body = response.json()
        expires_in = int(body.get("expires_in", 3600))
        return TokenSet(
            access_token=body["access_token"],
            refresh_token=body.get("refresh_token"),
            expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
            scopes=body.get("scope", "").split(),
        )

    async def exchange_code(self, *, code: str, code_verifier: str, redirect_uri: str) -> TokenSet:
        return await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": code_verifier,
                "redirect_uri": redirect_uri,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            }
        )

    async def refresh(self, refresh_token: str) -> TokenSet:
        return await self._token_request(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            }
        )

    async def revoke(self, token: str) -> None:
        async with self._client() as client:
            try:
                await client.post(REVOKE_URL, data={"token": token})
            except httpx.HTTPError:
                # Best effort: local credentials are deleted regardless.
                pass

    # --- API helpers -------------------------------------------------------------
    async def _api(self, access_token: str, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        async with self._client() as client:
            try:
                response = await request_with_backoff(
                    client,
                    method,
                    f"{API}{path}",
                    headers={"Authorization": f"Bearer {access_token}"},
                    max_retries=self.settings.provider_max_retries,
                    is_retryable=_is_retryable,
                    **kwargs,
                )
            except httpx.HTTPError as exc:
                raise ProviderUnavailableError("Gmail API unreachable") from exc
        if response.status_code == 401:
            raise ProviderAuthError()
        if response.status_code == 403:
            reason = _error_reason(response)
            if reason in _RATE_LIMIT_REASONS:
                raise ProviderUnavailableError("Gmail quota exceeded")
            raise InsufficientScopeError()
        if response.status_code in RETRYABLE_STATUS:
            raise ProviderUnavailableError()
        if response.status_code == 404:
            raise ProviderRequestError("Not found", 404)
        if response.status_code >= 400:
            raise ProviderRequestError(f"Gmail API error ({response.status_code})", response.status_code)
        return response.json() if response.content else {}

    # --- Mailbox -----------------------------------------------------------------
    async def get_profile(self, access_token: str) -> MailboxProfile:
        body = await self._api(access_token, "GET", "/profile")
        return MailboxProfile(email_address=body["emailAddress"].lower(), cursor=str(body.get("historyId")))

    async def list_recent_message_ids(
        self, access_token: str, *, newer_than_days: int, max_messages: int
    ) -> list[str]:
        ids: list[str] = []
        page_token: str | None = None
        while len(ids) < max_messages:
            params: dict[str, Any] = {
                "labelIds": "INBOX",
                "q": f"newer_than:{newer_than_days}d",
                "maxResults": min(self.settings.sync_page_size, max_messages - len(ids)),
            }
            if page_token:
                params["pageToken"] = page_token
            body = await self._api(access_token, "GET", "/messages", params=params)
            ids.extend(m["id"] for m in body.get("messages", []))
            page_token = body.get("nextPageToken")
            if not page_token:
                break
        return ids[:max_messages]

    async def list_changes(self, access_token: str, cursor: str) -> ChangeSet:
        added: list[str] = []
        deleted: list[str] = []
        page_token: str | None = None
        new_cursor = cursor
        while True:
            params: list[tuple[str, Any]] = [
                ("startHistoryId", cursor),
                ("historyTypes", "messageAdded"),
                ("historyTypes", "messageDeleted"),
                ("labelId", "INBOX"),
                ("maxResults", self.settings.sync_page_size),
            ]
            if page_token:
                params.append(("pageToken", page_token))
            try:
                body = await self._api(access_token, "GET", "/history", params=params)
            except ProviderRequestError as exc:
                if exc.status_code == 404:
                    raise CursorExpiredError() from exc
                raise
            for record in body.get("history", []):
                added.extend(m["message"]["id"] for m in record.get("messagesAdded", []))
                deleted.extend(m["message"]["id"] for m in record.get("messagesDeleted", []))
            new_cursor = str(body.get("historyId", new_cursor))
            page_token = body.get("nextPageToken")
            if not page_token:
                break
        deleted_set = set(deleted)
        unique_added = [mid for mid in dict.fromkeys(added) if mid not in deleted_set]
        return ChangeSet(added_ids=unique_added, deleted_ids=list(deleted_set), new_cursor=new_cursor)

    async def get_message(self, access_token: str, message_id: str) -> ProviderMessage:
        try:
            body = await self._api(access_token, "GET", f"/messages/{message_id}", params={"format": "full"})
        except ProviderRequestError as exc:
            if exc.status_code == 404:
                raise MessageNotFoundError(message_id) from exc
            raise
        return parse_gmail_message(body, max_body_chars=self.settings.max_body_chars)

    async def send_reply(self, access_token: str, reply: OutgoingReply) -> str | None:
        mime = build_reply_mime(
            from_address=reply.from_address,
            to=reply.to,
            subject=reply.subject,
            body=reply.body,
            in_reply_to=reply.in_reply_to,
            references=reply.references,
        )
        raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
        payload: dict[str, Any] = {"raw": raw}
        if reply.thread_id:
            payload["threadId"] = reply.thread_id
        # No automatic retries on send: a retried POST could deliver the message twice.
        async with self._client() as client:
            try:
                response = await client.post(
                    f"{API}/messages/send", json=payload, headers={"Authorization": f"Bearer {access_token}"}
                )
            except httpx.TimeoutException as exc:
                raise ProviderRequestError("Send timed out; delivery outcome unknown", None) from exc
            except httpx.HTTPError as exc:
                raise ProviderUnavailableError("Gmail API unreachable") from exc
        if response.status_code == 401:
            raise ProviderAuthError()
        if response.status_code == 403:
            raise InsufficientScopeError("The account did not grant permission to send email")
        if response.status_code >= 400:
            raise ProviderRequestError(f"Gmail send failed ({response.status_code})", response.status_code)
        return response.json().get("id")


def _headers(payload: dict[str, Any]) -> dict[str, str]:
    return {h["name"]: h["value"] for h in payload.get("headers", []) if "name" in h}


def _charset(part: dict[str, Any]) -> str:
    content_type = _headers(part).get("Content-Type", "")
    for piece in content_type.split(";"):
        piece = piece.strip()
        if piece.lower().startswith("charset="):
            return piece.split("=", 1)[1].strip('"') or "utf-8"
    return "utf-8"


def _decode_part(part: dict[str, Any]) -> str:
    data = part.get("body", {}).get("data")
    if not data:
        return ""
    raw = decode_base64url(data)
    try:
        return raw.decode(_charset(part), errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _walk(part: dict[str, Any], plain: list[str], html: list[str], attachments: list[dict[str, Any]]) -> None:
    mime = (part.get("mimeType") or "").lower()
    filename = part.get("filename") or ""
    body = part.get("body", {})
    disposition = _headers(part).get("Content-Disposition", "").lower()
    if filename or body.get("attachmentId") or disposition.startswith("attachment"):
        # Attachment contents are never downloaded here; only metadata is kept.
        attachments.append(
            {"filename": filename[:255], "mime_type": mime[:100], "size": int(body.get("size", 0))}
        )
        return
    if mime == "text/plain":
        plain.append(_decode_part(part))
    elif mime == "text/html":
        html.append(_decode_part(part))
    for child in part.get("parts", []) or []:
        _walk(child, plain, html, attachments)


def parse_gmail_message(body: dict[str, Any], *, max_body_chars: int) -> ProviderMessage:
    payload = body.get("payload", {})
    headers = _headers(payload)
    lower = {k.lower(): v for k, v in headers.items()}
    plain: list[str] = []
    html: list[str] = []
    attachments: list[dict[str, Any]] = []
    _walk(payload, plain, html, attachments)
    text = normalize_text("\n\n".join(p for p in plain if p.strip()))
    if not text and html:
        text = html_to_text("\n".join(html))
    text, _ = truncate(text, max_body_chars)

    sender = parse_address(lower.get("from")) or Address(email="unknown@invalid", name=None)
    reply_to = parse_address(lower.get("reply-to"))
    internal_ms = int(body.get("internalDate", 0) or 0)
    received = datetime.fromtimestamp(internal_ms / 1000, tz=UTC) if internal_ms else datetime.now(UTC)
    return ProviderMessage(
        provider_message_id=body["id"],
        thread_id=body.get("threadId"),
        sender=sender,
        to=parse_address_list(lower.get("to")),
        cc=parse_address_list(lower.get("cc")),
        reply_to=reply_to.email if reply_to else None,
        subject=(lower.get("subject") or "")[:998],
        body_text=text,
        received_at=received,
        internet_message_id=lower.get("message-id"),
        references=lower.get("references"),
        labels=list(body.get("labelIds", [])),
        attachments=attachments,
        snippet=(body.get("snippet") or "")[:500] or None,
        is_automated=detect_automated(headers, sender.email),
    )
