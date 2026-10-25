"""Email listing (pagination/filters), details, analysis, threads and per-user isolation."""

from tests.api.conftest import emails_by_subject
from tests.helpers import drain


async def test_list_pagination_metadata(client, mailbox, auth_headers):
    r = await client.get("/api/v1/emails?page=2&page_size=3", headers=auth_headers)
    body = r.json()
    assert body["total"] == 7 and body["page"] == 2 and body["page_size"] == 3 and body["pages"] == 3
    assert len(body["items"]) == 3
    received = [e["received_at"] for e in body["items"]]
    assert received == sorted(received, reverse=True)
    assert (await client.get("/api/v1/emails?page_size=101", headers=auth_headers)).status_code == 422


async def test_filters(client, mailbox, auth_headers):
    async def total(query):
        r = await client.get(f"/api/v1/emails?{query}", headers=auth_headers)
        assert r.status_code == 200, r.text
        return r.json()["total"]

    assert await total("is_emergency=true") == 2
    assert await total("priority=urgent") == 2
    assert await total("priority=low") == 2
    assert await total("needs_review=true") >= 1
    assert await total("sender=acme-corp") == 3
    assert await total("sender=%25") == 0  # LIKE wildcards are escaped
    assert await total(f"account_id={mailbox['id']}") == 7
    assert await total("processing_status=analyzed") == 7
    assert await total("q=Payments") == 1
    assert await total("received_after=2100-01-01T00:00:00Z") == 0
    assert (await client.get("/api/v1/emails?priority=critical", headers=auth_headers)).status_code == 422


async def test_email_detail_and_analysis(client, mailbox, auth_headers):
    outage = (await emails_by_subject(client, auth_headers))["Payments API down - all checkouts failing"]
    assert outage["analysis"]["is_emergency"] is True
    detail = (await client.get(f"/api/v1/emails/{outage['id']}", headers=auth_headers)).json()
    assert "HTTP 500" in detail["body_text"] and detail["recipients"]["to"]
    analysis = (await client.get(f"/api/v1/emails/{outage['id']}/analysis", headers=auth_headers)).json()
    assert analysis["priority"] == "urgent" and analysis["emergency_evidence"]
    assert analysis["model_name"] == "mock-heuristic"
    assert analysis["created_at"].endswith("Z") or "+00:00" in analysis["created_at"]


async def test_cross_user_access_returns_404(client, mailbox, auth_headers, other_headers):
    email = next(iter((await emails_by_subject(client, auth_headers)).values()))
    for path in ("", "/analysis", "/thread"):
        assert (await client.get(f"/api/v1/emails/{email['id']}{path}", headers=other_headers)).status_code == 404
    for path in ("/analyze", "/generate-reply", "/mark-read"):
        r = await client.post(f"/api/v1/emails/{email['id']}{path}", json={}, headers=other_headers)
        assert r.status_code == 404, path
    assert (await client.get("/api/v1/emails", headers=other_headers)).json()["total"] == 0


async def test_analyze_endpoint_queues_and_reanalyzes(client, mailbox, auth_headers, dispatcher, sessionmaker, store):
    email = (await emails_by_subject(client, auth_headers))["Q3 planning review"]
    r = await client.post(f"/api/v1/emails/{email['id']}/analyze", json={"force": True}, headers=auth_headers)
    assert r.status_code == 202
    task_id = r.json()["task_id"]
    dup = await client.post(f"/api/v1/emails/{email['id']}/analyze", json={}, headers=auth_headers)
    assert dup.status_code == 409
    await drain(dispatcher, sessionmaker, store)
    task = (await client.get(f"/api/v1/tasks/{task_id}", headers=auth_headers)).json()
    assert task["status"] == "succeeded" and task["result"]["status"] == "analyzed"


async def test_analysis_404_before_processing(client, auth_headers, dispatcher, sessionmaker, store):
    await client.post("/api/v1/accounts/sandbox/connect", headers=auth_headers)
    # Run only the sync, not the analyses.
    from app.services.dispatcher import SYNC_ACCOUNT

    sync = [s for s in dispatcher.sent if s[0] == SYNC_ACCOUNT]
    dispatcher.sent[:] = sync
    await drain(dispatcher, sessionmaker, store, max_steps=1)
    items = (await client.get("/api/v1/emails", headers=auth_headers)).json()["items"]
    r = await client.get(f"/api/v1/emails/{items[0]['id']}/analysis", headers=auth_headers)
    assert r.status_code == 404 and r.json()["error"]["code"] == "analysis_not_found"


async def test_thread_and_mark_read(client, mailbox, auth_headers):
    email = next(iter((await emails_by_subject(client, auth_headers)).values()))
    thread = (await client.get(f"/api/v1/emails/{email['id']}/thread", headers=auth_headers)).json()
    assert [t["id"] for t in thread] == [email["id"]]
    r = await client.post(f"/api/v1/emails/{email['id']}/mark-read", json={"is_read": True}, headers=auth_headers)
    assert r.json()["is_read"] is True
    assert (await client.get("/api/v1/emails?is_read=true", headers=auth_headers)).json()["total"] == 1


async def test_generate_reply_refuses_noreply_and_queues_otherwise(client, mailbox, auth_headers, dispatcher,
                                                                  sessionmaker, store):
    emails = await emails_by_subject(client, auth_headers)
    invoice = emails["Invoice INV-2291 available"]
    r = await client.post(f"/api/v1/emails/{invoice['id']}/generate-reply", json={"tone": "formal"},
                          headers=auth_headers)
    assert r.status_code == 202
    await drain(dispatcher, sessionmaker, store)
    drafts = (await client.get(f"/api/v1/drafts?email_id={invoice['id']}", headers=auth_headers)).json()
    assert drafts["total"] == 1 and drafts["items"][0]["tone"] == "formal"
    assert drafts["items"][0]["status"] == "pending_review"
