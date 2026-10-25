"""Draft review workflow and the safety of sending: approval, confirmation, idempotency, isolation."""

import pytest

from app.integrations.email_providers.sandbox import SandboxProvider
from tests.api.conftest import emails_by_subject
from tests.helpers import drain

CLEAN_BODY = "Hi Dana,\n\nThursday at 3pm works for me. Please invite the design team.\n\nBest,\nAlice"


@pytest.fixture
async def meeting_draft(client, mailbox, auth_headers):
    email = (await emails_by_subject(client, auth_headers))["Q3 planning review"]
    r = await client.get(f"/api/v1/drafts?email_id={email['id']}", headers=auth_headers)
    assert r.json()["total"] == 1, "a draft is generated automatically for reply-worthy emails"
    return r.json()["items"][0]


async def approve_clean(client, draft, headers):
    r = await client.patch(f"/api/v1/drafts/{draft['id']}", json={"draft_body": CLEAN_BODY}, headers=headers)
    assert r.status_code == 200
    r = await client.post(f"/api/v1/drafts/{draft['id']}/approve", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def send(client, draft_id, headers, key="send-key-0001", body=None):
    return client.post(f"/api/v1/drafts/{draft_id}/send", json={"confirm": True} if body is None else body,
                       headers={**headers, "Idempotency-Key": key})


async def test_generated_draft_is_pending_review_and_never_auto_sent(meeting_draft):
    assert meeting_draft["status"] == "pending_review"
    assert meeting_draft["to_address"] == "dana@acme-corp.example"
    assert meeting_draft["draft_subject"] == "Re: Q3 planning review"
    assert SandboxProvider.sent == []


async def test_placeholders_block_approval(client, meeting_draft, auth_headers):
    r = await client.post(f"/api/v1/drafts/{meeting_draft['id']}/approve", headers=auth_headers)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "unresolved_placeholders"
    assert "[Your name]" in r.json()["error"]["details"]["placeholders"]


async def test_full_send_flow_with_idempotent_replay(client, meeting_draft, auth_headers):
    approved = await approve_clean(client, meeting_draft, auth_headers)
    assert approved["status"] == "approved" and approved["approved_at"]
    assert SandboxProvider.sent == []  # approval alone never sends

    r = await send(client, meeting_draft["id"], auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["replayed"] is False and r.json()["draft"]["status"] == "sent"
    assert len(SandboxProvider.sent) == 1
    sent = SandboxProvider.sent[0]
    assert sent.to == "dana@acme-corp.example" and sent.in_reply_to == "<sbx-004@sandbox.local>"
    assert sent.body == CLEAN_BODY

    replay = await send(client, meeting_draft["id"], auth_headers)
    assert replay.status_code == 200 and replay.json()["replayed"] is True
    assert len(SandboxProvider.sent) == 1  # no duplicate delivery

    other_key = await send(client, meeting_draft["id"], auth_headers, key="send-key-0002")
    assert other_key.status_code == 409 and other_key.json()["error"]["code"] == "already_sent"
    assert len(SandboxProvider.sent) == 1

    edit = await client.patch(f"/api/v1/drafts/{meeting_draft['id']}", json={"draft_body": "x"}, headers=auth_headers)
    assert edit.status_code == 409
    delete = await client.delete(f"/api/v1/drafts/{meeting_draft['id']}", headers=auth_headers)
    assert delete.status_code == 409


async def test_send_requires_explicit_confirmation_and_key(client, meeting_draft, auth_headers):
    await approve_clean(client, meeting_draft, auth_headers)
    for body in ({}, {"confirm": False}, {"confirm": "yes"}):
        r = await send(client, meeting_draft["id"], auth_headers, body=body)
        assert r.status_code == 422, body
    r = await client.post(f"/api/v1/drafts/{meeting_draft['id']}/send", json={"confirm": True}, headers=auth_headers)
    assert r.status_code == 422  # missing Idempotency-Key
    assert SandboxProvider.sent == []


async def test_unapproved_draft_cannot_be_sent(client, meeting_draft, auth_headers):
    r = await send(client, meeting_draft["id"], auth_headers)
    assert r.status_code == 409 and r.json()["error"]["code"] == "invalid_state_transition"
    assert SandboxProvider.sent == []


async def test_editing_after_approval_requires_reapproval(client, meeting_draft, auth_headers):
    await approve_clean(client, meeting_draft, auth_headers)
    r = await client.patch(f"/api/v1/drafts/{meeting_draft['id']}", json={"draft_body": CLEAN_BODY + " P.S."},
                           headers=auth_headers)
    assert r.json()["status"] == "pending_review" and r.json()["approved_at"] is None
    r = await send(client, meeting_draft["id"], auth_headers)
    assert r.status_code == 409
    assert SandboxProvider.sent == []


async def test_other_user_cannot_view_edit_approve_or_send(client, meeting_draft, auth_headers, other_headers):
    await approve_clean(client, meeting_draft, auth_headers)
    draft_id = meeting_draft["id"]
    assert (await client.get(f"/api/v1/drafts/{draft_id}", headers=other_headers)).status_code == 404
    assert (await client.patch(f"/api/v1/drafts/{draft_id}", json={"draft_body": "hi"},
                               headers=other_headers)).status_code == 404
    for action in ("approve", "reject", "regenerate"):
        r = await client.post(f"/api/v1/drafts/{draft_id}/{action}", json={}, headers=other_headers)
        assert r.status_code == 404, action
    assert (await send(client, draft_id, other_headers)).status_code == 404
    assert SandboxProvider.sent == []
    assert (await client.get("/api/v1/drafts", headers=other_headers)).json()["total"] == 0


async def test_reject_and_state_transitions(client, meeting_draft, auth_headers):
    r = await client.post(f"/api/v1/drafts/{meeting_draft['id']}/reject", headers=auth_headers)
    assert r.json()["status"] == "rejected"
    r = await client.post(f"/api/v1/drafts/{meeting_draft['id']}/reject", headers=auth_headers)
    assert r.status_code == 409 and r.json()["error"]["details"] == {"from": "rejected", "to": "rejected"}
    # Editing a rejected draft brings it back for review.
    r = await client.patch(f"/api/v1/drafts/{meeting_draft['id']}", json={"draft_body": CLEAN_BODY},
                           headers=auth_headers)
    assert r.json()["status"] == "pending_review"


async def test_regenerate_and_delete(client, meeting_draft, auth_headers, dispatcher, sessionmaker, store):
    r = await client.post(f"/api/v1/drafts/{meeting_draft['id']}/regenerate", json={"tone": "concise"},
                          headers=auth_headers)
    assert r.status_code == 202
    await drain(dispatcher, sessionmaker, store)
    draft = (await client.get(f"/api/v1/drafts/{meeting_draft['id']}", headers=auth_headers)).json()
    assert draft["tone"] == "concise" and draft["status"] == "pending_review"
    assert (await client.delete(f"/api/v1/drafts/{meeting_draft['id']}", headers=auth_headers)).status_code == 204
    assert (await client.get(f"/api/v1/drafts/{meeting_draft['id']}", headers=auth_headers)).status_code == 404


async def test_provider_failure_marks_draft_failed_and_blocks_blind_resend(client, meeting_draft, auth_headers,
                                                                          monkeypatch):
    from app.integrations.email_providers.base import ProviderRequestError

    async def timeout(self, token, reply):
        raise ProviderRequestError("Send timed out; delivery outcome unknown", None)

    monkeypatch.setattr(SandboxProvider, "send_reply", timeout)
    await approve_clean(client, meeting_draft, auth_headers)
    r = await send(client, meeting_draft["id"], auth_headers)
    assert r.status_code == 502 and r.json()["error"]["details"]["error_code"] == "send_outcome_unknown"
    draft = (await client.get(f"/api/v1/drafts/{meeting_draft['id']}", headers=auth_headers)).json()
    assert draft["status"] == "failed"
    # Possibly delivered: approving again requires a deliberate edit first.
    r = await client.post(f"/api/v1/drafts/{meeting_draft['id']}/approve", headers=auth_headers)
    assert r.status_code == 409 and r.json()["error"]["code"] == "send_outcome_unknown"


async def test_listing_filters_by_status(client, meeting_draft, auth_headers):
    r = await client.get("/api/v1/drafts?status=pending_review", headers=auth_headers)
    assert r.json()["total"] >= 1
    r = await client.get("/api/v1/drafts?status=sent", headers=auth_headers)
    assert r.json()["total"] == 0
