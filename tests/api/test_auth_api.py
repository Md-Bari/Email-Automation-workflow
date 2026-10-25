from tests.conftest import register_and_login


async def test_register_login_me(client):
    r = await client.post("/api/v1/auth/register",
                          json={"email": "Bob@Example.com", "password": "correct-horse-battery"})
    assert r.status_code == 201
    body = r.json()
    assert body["email"] == "bob@example.com" and "hashed_password" not in body
    r = await client.post("/api/v1/auth/login", json={"email": "bob@example.com", "password": "correct-horse-battery"})
    tokens = r.json()
    assert tokens["token_type"] == "bearer" and tokens["expires_in"] == 900
    r = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {tokens['access_token']}"})
    assert r.status_code == 200 and r.json()["email"] == "bob@example.com"


async def test_duplicate_registration_conflicts(client):
    await register_and_login(client, "dup@example.com")
    r = await client.post("/api/v1/auth/register", json={"email": "dup@example.com", "password": "another-long-pass"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "email_taken"


async def test_weak_password_rejected_and_not_echoed(client):
    r = await client.post("/api/v1/auth/register", json={"email": "w@example.com", "password": "Zq9!xK"})
    assert r.status_code == 422
    assert "Zq9!xK" not in r.text
    assert r.json()["error"]["code"] == "validation_error"


async def test_unauthenticated_requests_get_401(client):
    for path in ("/api/v1/auth/me", "/api/v1/emails", "/api/v1/accounts", "/api/v1/drafts"):
        r = await client.get(path)
        assert r.status_code == 401, path
        assert r.json()["error"]["code"] == "unauthenticated"
    r = await client.get("/api/v1/auth/me", headers={"Authorization": "Bearer not-a-jwt"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_token"


async def test_wrong_password_and_lockout(client):
    await register_and_login(client, "lock@example.com")
    for _ in range(5):
        r = await client.post("/api/v1/auth/login", json={"email": "lock@example.com", "password": "wrong-password"})
        assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_credentials"
    r = await client.post("/api/v1/auth/login", json={"email": "lock@example.com", "password": "correct-horse-battery"})
    assert r.status_code == 429 and "Retry-After" in r.headers


async def test_unknown_user_gets_same_error_as_wrong_password(client):
    r = await client.post("/api/v1/auth/login", json={"email": "nobody@example.com", "password": "whatever-pass"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_credentials"


async def login_tokens(client, email):
    await client.post("/api/v1/auth/register", json={"email": email, "password": "correct-horse-battery"})
    return (await client.post("/api/v1/auth/login", json={"email": email, "password": "correct-horse-battery"})).json()


async def test_refresh_rotation_and_reuse_detection(client):
    tokens = await login_tokens(client, "rot@example.com")
    r1 = await client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert r1.status_code == 200
    new = r1.json()
    assert new["refresh_token"] != tokens["refresh_token"]
    # Reusing the rotated token is treated as theft: it fails and revokes the whole family.
    reuse = await client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert reuse.status_code == 401
    r2 = await client.post("/api/v1/auth/refresh", json={"refresh_token": new["refresh_token"]})
    assert r2.status_code == 401


async def test_logout_revokes_refresh_token(client):
    tokens = await login_tokens(client, "out@example.com")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    r = await client.post("/api/v1/auth/logout", json={"refresh_token": tokens["refresh_token"]}, headers=headers)
    assert r.status_code == 204
    r = await client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert r.status_code == 401


async def test_delete_account_requires_password_and_removes_data(client, mailbox, auth_headers):
    r = await client.request("DELETE", "/api/v1/auth/me", json={"password": "wrong"}, headers=auth_headers)
    assert r.status_code == 401
    r = await client.request("DELETE", "/api/v1/auth/me", json={"password": "correct-horse-battery"},
                             headers=auth_headers)
    assert r.status_code == 204
    assert (await client.get("/api/v1/auth/me", headers=auth_headers)).status_code == 401
    r = await client.post("/api/v1/auth/login",
                          json={"email": "alice@example.com", "password": "correct-horse-battery"})
    assert r.status_code == 401


async def test_error_responses_carry_correlation_id(client):
    r = await client.get("/api/v1/auth/me", headers={"X-Request-ID": "req-12345678"})
    assert r.headers["X-Request-ID"] == "req-12345678"
    assert r.json()["error"]["correlation_id"] == "req-12345678"
    assert r.headers["X-Content-Type-Options"] == "nosniff"
