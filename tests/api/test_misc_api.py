"""Notifications, tasks, dashboard and health endpoints."""


async def test_emergency_notifications_and_actions(client, mailbox, auth_headers, other_headers):
    r = await client.get("/api/v1/notifications?notification_type=emergency", headers=auth_headers)
    body = r.json()
    assert body["total"] == 2
    note = body["items"][0]
    assert note["severity"] == "critical" and note["is_read"] is False and note["email_id"]
    assert "Suggested action:" in note["message"]

    detail = (await client.get(f"/api/v1/notifications/{note['id']}", headers=auth_headers)).json()
    assert detail["deliveries"] == []  # no external channels configured
    assert (await client.get(f"/api/v1/notifications/{note['id']}", headers=other_headers)).status_code == 404

    r = await client.post(f"/api/v1/notifications/{note['id']}/acknowledge", headers=auth_headers)
    assert r.json()["acknowledged_at"] and r.json()["is_read"]
    r = await client.post("/api/v1/notifications/read-all", headers=auth_headers)
    assert r.json()["updated"] >= 1
    assert (await client.get("/api/v1/notifications?is_read=false", headers=auth_headers)).json()["total"] == 0


async def test_notification_preferences(client, mailbox, auth_headers, other_headers):
    prefs = (await client.get("/api/v1/notifications/preferences", headers=auth_headers)).json()
    assert prefs["emergency_alerts_enabled"] and prefs["available_channels"] == ["in_app"]
    changes = {"telegram_enabled": True, "telegram_chat_id": "123456", "default_reply_tone": "friendly"}
    r = await client.patch("/api/v1/notifications/preferences", json=changes, headers=auth_headers)
    assert r.status_code == 200 and r.json()["telegram_chat_id"] == "123456"
    r = await client.patch(f"/api/v1/notifications/preferences?account_id={mailbox['id']}",
                           json={"emergency_alerts_enabled": False}, headers=auth_headers)
    assert r.json()["email_account_id"] == mailbox["id"] and r.json()["emergency_alerts_enabled"] is False
    assert r.json()["default_reply_tone"] == "friendly"  # inherited from user defaults
    assert (await client.get("/api/v1/notifications/preferences", headers=auth_headers)).json()[
        "emergency_alerts_enabled"] is True
    bad = await client.patch("/api/v1/notifications/preferences", json={"telegram_chat_id": "not a chat id!"},
                             headers=auth_headers)
    assert bad.status_code == 422
    r = await client.patch(f"/api/v1/notifications/preferences?account_id={mailbox['id']}", json={},
                           headers=other_headers)
    assert r.status_code == 404


async def test_tasks_listing_and_retry_rules(client, mailbox, auth_headers, other_headers):
    tasks = (await client.get("/api/v1/tasks", headers=auth_headers)).json()
    assert tasks["total"] == 1 and tasks["items"][0]["status"] == "succeeded"
    task_id = tasks["items"][0]["id"]
    r = await client.post(f"/api/v1/tasks/{task_id}/retry", headers=auth_headers)
    assert r.status_code == 409 and r.json()["error"]["code"] == "task_not_failed"
    assert (await client.get(f"/api/v1/tasks/{task_id}", headers=other_headers)).status_code == 404
    assert (await client.post(f"/api/v1/tasks/{task_id}/retry", headers=other_headers)).status_code == 404
    assert (await client.get("/api/v1/tasks?status=failed", headers=auth_headers)).json()["total"] == 0


async def test_dashboard(client, mailbox, auth_headers, other_headers):
    s = (await client.get("/api/v1/dashboard/summary", headers=auth_headers)).json()
    assert s["accounts"] == 1 and s["emails_total"] == 7 and s["emails_processed"] == 7
    assert s["urgent_emails"] == 2 and s["emergencies_last_7_days"] == 2
    assert s["unread_notifications"] == 2 and s["failed_jobs"] == 0
    assert s["pending_drafts"] >= 2
    activity = (await client.get("/api/v1/dashboard/activity?limit=10", headers=auth_headers)).json()
    assert {"user.login", "account.connect"} <= {a["action"] for a in activity}
    other = (await client.get("/api/v1/dashboard/summary", headers=other_headers)).json()
    assert other["emails_total"] == 0


async def test_health_endpoints_expose_no_details(client):
    assert (await client.get("/api/v1/health")).json() == {"status": "ok"}
    r = await client.get("/api/v1/health/ready")
    assert r.status_code == 200
    assert r.json() == {"status": "ready", "checks": {"database": True, "redis": True}}


async def test_readiness_reports_503_when_dependency_down(client, store, monkeypatch):
    async def down():
        return False

    monkeypatch.setattr(store, "ping", down)
    r = await client.get("/api/v1/health/ready")
    assert r.status_code == 503 and r.json()["checks"]["redis"] is False


async def test_openapi_and_metrics(client):
    spec = (await client.get("/api/v1/openapi.json")).json()
    assert "/api/v1/drafts/{draft_id}/send" in spec["paths"]
    assert (await client.get("/docs")).status_code == 200
    metrics = await client.get("/metrics")
    assert metrics.status_code == 200 and "http_requests_total" in metrics.text
