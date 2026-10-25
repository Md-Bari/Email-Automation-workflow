# Agentic AI Email Assistant — Backend API

A REST backend that connects to a user's mailbox (Gmail via OAuth 2.0 + PKCE; Microsoft Graph
optional), synchronizes mail incrementally, analyzes each email with a LangGraph workflow,
detects genuine emergencies, raises notifications, and prepares reply drafts that are **only ever
sent after explicit human approval and a separate confirmation**.

There is no frontend: everything is exposed through a versioned REST API (`/api/v1`) with
OpenAPI docs, ready for a React, Next.js, Flutter or other client.

---

## Contents

1. [Quick start](#quick-start)
2. [Architecture](#architecture)
3. [Design decisions and changes to the original spec](#design-decisions-and-changes-to-the-original-spec)
4. [Configuration](#configuration)
5. [Getting the keys](#getting-the-keys)
6. [Gmail OAuth setup](#gmail-oauth-setup)
7. [API overview](#api-overview)
8. [The analysis workflow](#the-analysis-workflow)
9. [Security and privacy](#security-and-privacy)
10. [Background jobs](#background-jobs)
11. [Testing](#testing)
12. [Deployment notes](#deployment-notes)
13. [Limitations and unverified parts](#limitations-and-unverified-parts)

---

## Quick start

### Option A — Docker Compose (recommended)

Out of the box, without any `.env`, the stack runs in **sandbox mode**: a mock mailbox with sample
emails and the deterministic mock LLM. No Google or Anthropic credentials are needed.

```bash
cp .env.example .env              # optional at first; edit secrets before real use
docker compose up --build -d      # postgres, redis, migrate, api, worker, scheduler
docker compose logs -f api
```

Windows PowerShell:

```powershell
Copy-Item .env.example .env
docker compose up --build -d
```

- API: http://localhost:8000 · Swagger UI: http://localhost:8000/docs · ReDoc: /redoc
- Migrations run automatically through the one-shot `migrate` service. To run them manually:
  `docker compose run --rm migrate alembic upgrade head`

Try the sandbox end to end:

```bash
# 1. Register and log in
curl -s -X POST localhost:8000/api/v1/auth/register -H 'Content-Type: application/json' \
  -d '{"email":"me@example.com","password":"a-long-password-123"}'
TOKEN=$(curl -s -X POST localhost:8000/api/v1/auth/login -H 'Content-Type: application/json' \
  -d '{"email":"me@example.com","password":"a-long-password-123"}' | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")

# 2. Connect the sandbox mailbox (sync + analysis run in the worker)
curl -s -X POST localhost:8000/api/v1/accounts/sandbox/connect -H "Authorization: Bearer $TOKEN"

# 3. A few seconds later: emergencies, notifications and drafts
curl -s "localhost:8000/api/v1/emails?is_emergency=true" -H "Authorization: Bearer $TOKEN"
curl -s localhost:8000/api/v1/notifications -H "Authorization: Bearer $TOKEN"
curl -s localhost:8000/api/v1/drafts -H "Authorization: Bearer $TOKEN"
```

### Option B — Local Python (development)

Requires Python 3.12+ and running PostgreSQL and Redis (for example
`docker compose up -d postgres redis`).

Linux/macOS:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env               # set DATABASE_URL / REDIS_URL / secrets
alembic upgrade head
uvicorn app.main:app --reload
celery -A app.workers.celery_app worker -Q default,sync,analysis,notifications,maintenance --loglevel=INFO
celery -A app.workers.celery_app beat --loglevel=INFO
```

Windows PowerShell (Celery needs the `solo` pool on Windows):

```powershell
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
Copy-Item .env.example .env
alembic upgrade head
uvicorn app.main:app --reload
celery -A app.workers.celery_app worker -P solo -Q default,sync,analysis,notifications,maintenance --loglevel=INFO
celery -A app.workers.celery_app beat --loglevel=INFO
```

---

## Architecture

```
app/
  main.py                 FastAPI factory: middleware (correlation IDs, security headers), CORS, errors, /metrics
  api/                    HTTP layer only: routing, request/response schemas, auth dependencies
    v1/endpoints/         auth, accounts, emails, drafts, notifications, tasks, dashboard, health
  core/                   config, security (Argon2, JWT, PKCE), encryption (Fernet), logging, errors, rate limits, KV store
  db/                     SQLAlchemy 2.0 async models, portable types, sessions
  schemas/                Pydantic v2 API schemas (credentials never appear in any response model)
  services/               business logic: auth, accounts/OAuth, sync, analysis, drafts, notifications, tasks, retention
  integrations/
    email_providers/      provider interface + Gmail, Microsoft Graph, Sandbox; MIME/HTML parsing
    llm/                  provider interface + Anthropic (Claude) and deterministic mock; structured-output schemas
    notifiers/            Telegram and SMTP channels
  agents/                 LangGraph workflow: state, nodes, prompts, deterministic rules/policy
  workers/                Celery app, task wrappers, broker-independent job implementations
  observability/          Prometheus metrics, optional OpenTelemetry tracing
alembic/                  migrations
tests/                    unit/, integration/, api/, fixtures/
```

Request flow: **API → service → (DB | provider | LLM)**. Long-running work is queued as a
`BackgroundTask` and returns `202 Accepted` with a `task_id` and `status_url`. Celery workers run
the same service code. Services never import FastAPI, and integrations never import services.

---

## Design decisions and changes to the original spec

The original specification was reviewed critically. Most of it was sound and is implemented as
written. These parts were changed because the change gives a better or safer result:

| Original spec | Change | Why |
|---|---|---|
| 13-node graph with separate `classify`, `analyze_priority`, `detect_emergency`, `generate_summary` LLM nodes | **One structured LLM call** (summary, intent, category, priority, emergency assessment, reply recommendation, confidence), followed by a **deterministic policy node** | 4x fewer LLM calls (cost and latency) and no contradictions between separate calls. The policy node then verifies the model's claims with code. |
| `fetch_email`, `normalize_email`, `deduplicate_email` as graph nodes | Done by the **sync job** before the graph runs | Fetching is I/O against the provider with its own retry and quota logic. The graph analyzes an already-stored email, which also makes re-analysis trivial. |
| Notification after draft generation | **Emergency notification is created before the draft** | A slow or failing draft generation must never delay an emergency alert. |
| "Temperature" LLM setting | Replaced by **`LLM_EFFORT`** | Current Claude models reject `temperature` (HTTP 400). Effort is the supported control. |
| LangChain for LLM calls | **Official Anthropic SDK** with native structured outputs (`messages.parse` + Pydantic) and server-side refusal fallback | Fewer layers and guaranteed schema-shaped JSON. LangGraph is still used for orchestration. |
| LangGraph checkpointing | **Idempotent nodes + persisted `processing_status`** instead of a checkpoint store | The graph is short. Recovery means re-running it safely, which the analysis upsert, notification dedup key and draft-exists check guarantee. A checkpointer would add a write per step without adding safety. |
| `mark-read` on the mailbox | **In-app read state** | Changing Gmail labels needs the broader `gmail.modify` scope. Keeping the minimum scopes (`gmail.readonly` + `gmail.send`) matters more. |
| Recipient chosen with the draft | **Recipient derived by code** from the original email (`Reply-To`/`From`), validated (no no-reply, not self), and re-checked at send time | The model can never redirect a reply. |
| Approval of any draft text | **Approval is blocked while `[placeholders]` remain**, and approval is **bound to a content hash** | Prevents sending "[confirm time]" by accident, and prevents changes after approval. |
| Emergency = model says so | **Evidence must be verbatim quotes found in the email**, category must be an emergency category, confidence ≥ threshold, no prompt-injection signals, and no bulk mail with urgency words only. Rejected claims are flagged `needs_review`. | Guards against hallucinated evidence and "URGENT!!!" marketing. |
| — | **Sandbox provider** (`POST /accounts/sandbox/connect`) | A complete end-to-end demo and frontend development without Google credentials. Replies are recorded, never delivered. |
| — | **Refresh-token rotation with reuse detection**, login lockout, `DELETE /auth/me` | Stronger session security and a real data-deletion path. |

---

## Configuration

All configuration is read from environment variables or `.env` (see [`.env.example`](.env.example)
for the full list with comments). The most important settings:

| Variable | Purpose |
|---|---|
| `JWT_SECRET_KEY` | HS256 signing key (≥ 32 random characters; enforced in production) |
| `TOKEN_ENCRYPTION_KEYS` | Comma-separated Fernet keys for OAuth tokens at rest. The first key encrypts; all keys decrypt (rotation). Required in production. |
| `DATABASE_URL`, `REDIS_URL` | PostgreSQL (asyncpg) and Redis |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | Enables Gmail |
| `MICROSOFT_CLIENT_ID`, `MICROSOFT_CLIENT_SECRET`, `MICROSOFT_TENANT` | Enables Microsoft Graph (optional) |
| `SANDBOX_MODE` | Enables the mock mailbox |
| `LLM_PROVIDER` | `mock` (default, no key) or `anthropic` |
| `LLM_MODEL`, `LLM_API_KEY`, `LLM_EFFORT`, `LLM_TIMEOUT_SECONDS`, `LLM_MAX_OUTPUT_TOKENS`, `LLM_MAX_RETRIES` | LLM settings. The default model is `claude-opus-5-5`; any Claude model ID can be configured. |
| `EMERGENCY_CONFIDENCE_THRESHOLD`, `REVIEW_CONFIDENCE_THRESHOLD`, `TRUSTED_ALERT_DOMAINS` | Decision thresholds and rules |
| `SYNC_INTERVAL_SECONDS`, `INITIAL_SYNC_DAYS`, `INITIAL_SYNC_MAX_MESSAGES` | Synchronization |
| `TELEGRAM_BOT_TOKEN`, `SMTP_*` | Optional notification channels |
| `*_RETENTION_DAYS` | Retention policy (0 disables a rule) |
| `FRONTEND_URL` | OAuth callback redirect target and notification links. Empty = callback returns JSON. |
| `CORS_ORIGINS` | Comma-separated allowed origins for the frontend |

---

## Getting the keys

Only the first three secrets are required. The app runs with the sandbox mailbox and mock LLM
without any of the others. Never commit `.env` or paste these values into chats or tickets.

| Variable(s) | Required? | Where it comes from |
|---|---|---|
| `JWT_SECRET_KEY` | Yes | Generate locally ([1](#1-local-secrets-jwt-encryption-database)) |
| `TOKEN_ENCRYPTION_KEYS` | Yes | Generate locally ([1](#1-local-secrets-jwt-encryption-database)) |
| `POSTGRES_PASSWORD` / `DATABASE_URL` | Yes | Choose or generate locally ([1](#1-local-secrets-jwt-encryption-database)) |
| `LLM_API_KEY` | For real AI analysis | Anthropic Console ([2](#2-anthropic-api-key-llm_api_key)) |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | For real Gmail | Google Cloud Console ([3](#3-google-oauth-client-google_client_id-google_client_secret)) |
| `MICROSOFT_CLIENT_ID` / `MICROSOFT_CLIENT_SECRET` | Optional | Azure portal ([4](#4-microsoft-graph-app-microsoft_client_id-microsoft_client_secret)) |
| `TELEGRAM_BOT_TOKEN` | Optional alerts | Telegram @BotFather ([5](#5-telegram-bot-telegram_bot_token)) |
| `SMTP_*` | Optional alerts | Your mail provider ([6](#6-smtp-for-email-alerts-smtp_)) |

After editing `.env`, restart so the containers pick up the new values:

```bash
docker compose up -d --force-recreate api worker scheduler
```

### 1. Local secrets (JWT, encryption, database)

Run these from the project folder with the virtualenv active (they work the same in PowerShell
and bash):

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"                               # JWT_SECRET_KEY
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # TOKEN_ENCRYPTION_KEYS
python -c "import secrets; print(secrets.token_urlsafe(24))"                               # POSTGRES_PASSWORD
```

- Use the same password in `POSTGRES_PASSWORD` and inside `DATABASE_URL`
  (`postgresql+asyncpg://email_assistant:<password>@localhost:5432/email_assistant`).
- **Back up `TOKEN_ENCRYPTION_KEYS`.** If you lose it, stored OAuth tokens cannot be decrypted and
  every mailbox must be reconnected.
- **Rotating the encryption key:** put the new key first and keep the old one after it
  (`TOKEN_ENCRYPTION_KEYS=<new>,<old>`). New tokens use the new key; old ones still decrypt.
- **Changing the database password:** Postgres sets it only on the volume's first start. After a
  change, either update it inside Postgres or recreate the volume with `docker compose down -v`
  (this **deletes all data**).
- **Changing `JWT_SECRET_KEY`:** this logs every user out. Their access tokens stop working.

### 2. Anthropic API key (`LLM_API_KEY`)

1. Sign in at <https://console.anthropic.com>. Create an account or organization if needed.
2. Add billing or credits under **Billing**. API calls fail without them.
3. Open **API Keys** → **Create Key**, name it (for example `email-assistant`), and copy it. It
   starts with `sk-ant-` and is shown only once.
4. In `.env`:

   ```env
   LLM_PROVIDER=anthropic
   LLM_API_KEY=sk-ant-...
   LLM_MODEL=claude-opus-5-5
   ```

5. Optionally set a spend limit in the Console. For high mail volume, consider a cheaper model
   (for example `LLM_MODEL=claude-haiku-5-5`), but check quality on your own mail first.

### 3. Google OAuth client (`GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`)

1. Open <https://console.cloud.google.com> and create a project (project picker → **New project**).
2. Enable the Gmail API: **APIs & Services → Library**, search for **Gmail API**, then **Enable**.
3. Configure the consent screen (**Google Auth Platform** / **OAuth consent screen**):
   - **Branding:** app name, support email and developer contact email.
   - **Audience:** user type **External**. While the app is in **Testing**, add every Google
     account that will connect under **Test users**.
   - **Data access:** add the scopes `https://www.googleapis.com/auth/gmail.readonly` and
     `https://www.googleapis.com/auth/gmail.send`.
4. Create the client: **Clients** (or **Credentials**) → **Create client** → application type
   **Web application**.
   - **Authorized redirect URI:** `http://localhost:8000/api/v1/accounts/gmail/callback`. In
     general, this is your `PUBLIC_API_URL` plus `/api/v1/accounts/gmail/callback`. It must match
     exactly, including scheme and port.
   - No JavaScript origins are needed. The browser only visits Google and the callback.
5. Copy the **Client ID** and **Client secret** into `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`.
   You can also download the JSON, but only these two values are used.

Notes:

- **Testing mode tokens expire.** While the app is in *Testing*, Google expires refresh tokens
  after about 7 days. The account then shows `reauth_required`, and the user reconnects.
- **Production needs verification.** `gmail.readonly` is a restricted scope, so letting other
  users connect requires publishing the app and passing Google's verification (and possibly a
  security assessment).
- **Deployed servers:** add the production redirect URI (`https://api.yourdomain/...`) to the
  same client, and set `PUBLIC_API_URL` to match.

### 4. Microsoft Graph app (`MICROSOFT_CLIENT_ID`, `MICROSOFT_CLIENT_SECRET`)

This is optional, and the Microsoft integration is untested against a live account.

1. Open <https://portal.azure.com> → **Microsoft Entra ID → App registrations → New registration**.
2. **Supported account types:** pick *Accounts in any organizational directory and personal
   Microsoft accounts* to allow both work and Outlook.com mailboxes (`MICROSOFT_TENANT=common`).
   For a single organization, use its tenant ID instead.
3. **Redirect URI:** platform **Web**, value `http://localhost:8000/api/v1/accounts/microsoft/callback`.
4. From **Overview**, copy the **Application (client) ID** into `MICROSOFT_CLIENT_ID`.
5. **Certificates & secrets → New client secret.** Copy the **Value** (not the Secret ID) into
   `MICROSOFT_CLIENT_SECRET`. It is shown once and expires on the date you choose, so note that
   date.
6. **API permissions → Add → Microsoft Graph → Delegated:** `offline_access`, `User.Read`,
   `Mail.Read`, `Mail.Send`. Some organizations require **Grant admin consent**.

### 5. Telegram bot (`TELEGRAM_BOT_TOKEN`)

1. In Telegram, open a chat with **@BotFather**, send `/newbot`, and choose a name and a username
   ending in `bot`.
2. BotFather replies with a token like `123456789:AA...`. Put it in `TELEGRAM_BOT_TOKEN` and
   restart.
3. Find your **chat ID**. Send any message to your new bot, then open the URL below in a browser
   (with your token). The number at `result[0].message.chat.id` is your chat ID.

   ```text
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```

   Group chat IDs are negative numbers.
4. The chat ID is a **per-user preference**, not a server setting. Set it through the API:

   ```http
   PATCH /api/v1/notifications/preferences
   {"telegram_enabled": true, "telegram_chat_id": "123456789"}
   ```

If the token leaks, send `/revoke` to BotFather to issue a new one.

### 6. SMTP for email alerts (`SMTP_*`)

Any SMTP provider works (Gmail, Microsoft 365, SendGrid, Mailgun, Amazon SES, ...). With a Gmail
account:

1. Turn on **2-Step Verification** for the account.
2. Create an **App password** at <https://myaccount.google.com/apppasswords> (16 characters).
3. In `.env`:

   ```env
   SMTP_HOST=smtp.gmail.com
   SMTP_PORT=587
   SMTP_USE_TLS=true
   SMTP_USERNAME=you@gmail.com
   SMTP_PASSWORD=<16-character app password, no spaces>
   SMTP_FROM=you@gmail.com
   ```

4. Each user enables alerts and picks the destination address with
   `PATCH /api/v1/notifications/preferences`:

   ```json
   {"email_enabled": true, "notification_email": "me@example.com"}
   ```

Never use your normal Gmail password here. App passwords can be revoked individually.

---

## Gmail OAuth setup

To create the Google OAuth client, follow [Getting the keys → Google OAuth client](#3-google-oauth-client-google_client_id-google_client_secret).
Once `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` are set and the app restarted, a client
connects a mailbox like this:

1. `POST /api/v1/accounts/gmail/connect` (authenticated) → `{"authorization_url": ...}`.
2. Open that URL in the browser. Google redirects to `/api/v1/accounts/gmail/callback`.
3. The API validates the single-use state, exchanges the code with the PKCE verifier, reads the
   mailbox address from the Gmail profile, stores encrypted tokens, queues the initial sync, and
   redirects to `FRONTEND_URL/oauth/result?status=connected&account_id=...` (or
   `status=error&error_code=...`).

Users never enter their Gmail password. Requested scopes are the minimum for read + reply.

---

## API overview

All endpoints are under `/api/v1`. Interactive documentation is at `/docs`.

| Area | Endpoints |
|---|---|
| Auth | `POST /auth/register`, `POST /auth/login`, `POST /auth/refresh`, `POST /auth/logout`, `GET /auth/me`, `DELETE /auth/me` |
| Accounts | `GET /accounts`, `POST /accounts/gmail/connect`, `GET /accounts/gmail/callback`, `POST /accounts/microsoft/connect`, `GET /accounts/microsoft/callback`, `POST /accounts/sandbox/connect`, `GET /accounts/{id}`, `DELETE /accounts/{id}`, `POST /accounts/{id}/sync`, `GET /accounts/{id}/sync-status` |
| Emails | `GET /emails` (filters: `account_id`, `priority`, `is_emergency`, `needs_review`, `processing_status`, `is_read`, `sender`, `q`, `received_after`, `received_before`), `GET /emails/{id}`, `GET /emails/{id}/analysis`, `POST /emails/{id}/analyze`, `POST /emails/{id}/generate-reply`, `GET /emails/{id}/thread`, `POST /emails/{id}/mark-read` |
| Drafts | `GET /drafts`, `GET /drafts/{id}`, `PATCH /drafts/{id}`, `DELETE /drafts/{id}`, `POST /drafts/{id}/regenerate`, `POST /drafts/{id}/approve`, `POST /drafts/{id}/reject`, `POST /drafts/{id}/send` |
| Notifications | `GET /notifications`, `GET /notifications/{id}`, `POST /notifications/{id}/read`, `POST /notifications/{id}/acknowledge`, `POST /notifications/read-all`, `GET/PATCH /notifications/preferences[?account_id=]` |
| Tasks | `GET /tasks`, `GET /tasks/{id}`, `POST /tasks/{id}/retry` |
| Dashboard | `GET /dashboard/summary`, `GET /dashboard/activity` |
| Health | `GET /health` (liveness), `GET /health/ready` (DB + Redis; 503 if down) |

Conventions:

- **Auth:** `Authorization: Bearer <access token>`. Access tokens last 15 minutes. Refresh tokens
  are opaque, rotate on every use, and reuse of a rotated token revokes the whole session family.
- **Errors:** always `{"error": {"code", "message", "details", "correlation_id"}}`.
  401 = unauthenticated, 403 = not permitted (for example the account lacks send scope),
  404 = missing **or belongs to someone else**, 409 = invalid state transition or idempotency
  conflict, 422 = validation, 429 = rate limited (`Retry-After` header).
- **Async:** `202 Accepted` with `{task_id, status, task_type, status_url}`. Poll `GET /tasks/{id}`.
- **Pagination:** `?page=&page_size=` (max 100). The response contains
  `{items, total, page, page_size, pages}`.
- **Timestamps:** stored in UTC, returned in ISO 8601 with offset.
- **Tracing:** send `X-Request-ID` (8–64 safe characters) or receive a generated one. It appears in
  logs, error bodies, audit logs and Celery jobs.

### Sending a reply (the only consequential action)

```http
PATCH /api/v1/drafts/{id}          {"draft_body": "...final text, no [placeholders]..."}
POST  /api/v1/drafts/{id}/approve  → status "approved" (does NOT send)
POST  /api/v1/drafts/{id}/send     {"confirm": true}
      Idempotency-Key: 3f0c8e1a-...   → 200 {"draft": {...status:"sent"}, "replayed": false}
```

- Repeating the same key returns the original result with `"replayed": true` and nothing is re-sent.
- Editing after approval returns the draft to `pending_review`.
- A timeout during sending marks the draft `failed` with `send_outcome_unknown`. It can only be
  re-approved after a deliberate edit, so a message that may already have been delivered is never
  sent twice by accident.

Draft states: `pending_review → approved → sending → sent`; also `rejected` and `failed`.
All transitions are validated (409 otherwise).

---

## The analysis workflow

```
load_email ─► screen_email ─► analyze_email (LLM) ─► apply_policy ─► persist_analysis
     │             │                                                    │
     └── skip ─────┴────────────────────► finalize ◄────────────────────┤
                                              ▲            emergency / rejected claim
                                              │                         ▼
                              generate_reply_draft ◄── dispatch ◄── create_notification
                                (reply recommended)
```

| Node | Responsibility |
|---|---|
| `load_email` | Loads the stored email. Skips if deleted at the provider or already analyzed with the current `ANALYSIS_VERSION` (idempotency). |
| `screen_email` | Deterministic signals: outage/security/deadline/threat patterns, urgency-words-only, prompt-injection markers, automated/no-reply sender, trusted alert domains. Skips empty mail. |
| `analyze_email` | One structured Claude call (`EmailAssessment` schema). Retried by a LangGraph `RetryPolicy` on transient errors and invalid output. Hard timeout. |
| `apply_policy` | Verifies and calibrates the model output (see the table above). Produces `needs_review` and a `decision_trace`. |
| `persist_analysis` | Upserts `EmailAnalysis` (one row per email) and marks the email `analyzed`. |
| `create_notification` | Deduplicated (`emergency:email:<id>`) in-app notification plus per-channel delivery rows. Rejected emergency claims create an in-app `needs_review` notification. |
| `dispatch_notifications` | Queues external deliveries. A failure here never fails analysis. |
| `generate_reply_draft` | Only when a reply is recommended, auto-drafts are enabled, and no draft exists. Failures are logged; the analysis stays saved. |

If the LLM is unavailable, the email stays `pending` with a `processing_error`, and the periodic
job retries it (up to `ANALYSIS_MAX_ATTEMPTS`). No AI result is ever invented.

---

## Security and privacy

- **Authentication:** Argon2id password hashing, short-lived JWTs, rotating refresh tokens stored
  as SHA-256 digests, reuse detection, login lockout (`LOGIN_MAX_ATTEMPTS`), and the same error
  message for unknown users and wrong passwords (no account enumeration).
- **Authorization:** every query is scoped to the authenticated user. Foreign resources return 404.
  Application code (never the LLM) enforces ownership, approval, state and scope.
- **OAuth:** single-use state stored as a hash in Redis with a 10-minute TTL and bound to the
  initiating user and provider. The PKCE (S256) verifier never leaves the server. The mailbox
  identity is read from the provider API. A mailbox can belong to only one user (account mix-up
  protection). Missing read scope → the tokens are revoked and the connection is rejected.
- **Credentials at rest:** OAuth tokens are Fernet-encrypted with rotatable keys, never logged and
  never returned. Disconnecting revokes them at the provider (best effort) and deletes all mailbox
  data.
- **Prompt injection:**
  - Email content is wrapped as untrusted data, and delimiter tags inside emails are neutralized.
  - Hidden HTML is stripped, and injection patterns are detected.
  - The LLM has no tools and produces data only.
  - Injection emails cannot become emergencies or get replies.
- **Input handling:** Pydantic validation everywhere, SQLAlchemy parameter binding, escaped LIKE
  filters, safe HTML-to-text (scripts and hidden content dropped), CR/LF header-injection
  checks, JSON-only Celery serialization (no pickle). Attachment contents are never downloaded;
  only metadata is stored.
- **Logging:** structured JSON with correlation IDs. Tokens, passwords, authorization headers,
  email bodies and drafts are redacted by key.
- **Transport/headers:** HSTS in production, `nosniff`, `X-Frame-Options: DENY`, `no-store`, and
  CORS restricted to `CORS_ORIGINS`. Run behind TLS (uvicorn uses `--proxy-headers`).
- **Rate limits:** login, registration, refresh, OAuth connect/callback, sync, analyze, generate,
  regenerate, send and retry.
- **Audit log:** registration, login (success/failure), logout, account connect/denied/disconnect,
  credential revocation, draft approve/reject/send, and refresh-token reuse.
- **Retention:** scheduled cleanup clears email bodies after `EMAIL_BODY_RETENTION_DAYS`, deletes
  emails, audit logs, finished tasks and read notifications after their windows, flags stuck sends,
  and fails stale tasks. `DELETE /auth/me` erases a user and all their data.

---

## Background jobs

| Queue | Tasks |
|---|---|
| `sync` | `tasks.sync_account` — initial (bounded: `INITIAL_SYNC_DAYS`, `INITIAL_SYNC_MAX_MESSAGES`) and incremental sync (Gmail `historyId`, Graph `deltaLink`) |
| `analysis` | `tasks.analyze_email`, `tasks.generate_draft` |
| `notifications` | `tasks.deliver_notification` — per-channel delivery with retries and status tracking |
| `maintenance` | beat: `schedule_due_syncs` (every 60 s, for accounts not synced within `SYNC_INTERVAL_SECONDS`), `retry_pending_analyses`, `retry_pending_deliveries`, `cleanup` |

- **Idempotency:**
  - Messages are deduplicated by the unique `(email_account_id, provider_message_id)` constraint.
  - A per-account Redis lock prevents concurrent syncs.
  - The cursor advances only after a sync completes.
  - Analyses upsert, notifications have dedup keys, and deliveries skip non-pending rows.
  - Sends are never retried automatically.
- **Retries:** exponential backoff with jitter (`TASK_MAX_RETRIES`). Provider HTTP calls retry
  408/429/5xx and Gmail quota 403s, honouring `Retry-After`. Manual retries are allowed only for
  failed sync/analysis/draft tasks, by their owner, up to `TASK_MANUAL_RETRY_LIMIT`.
- **Limits:** `acks_late`, `reject_on_worker_lost`, prefetch 1, soft/hard time limits.
- **Failure states:** expired history cursor → bounded resync; revoked token → account
  `reauth_required`; insufficient scope → error on the account; deleted message → skipped or
  marked `provider_deleted_at`.

---

## Testing

```bash
pip install -e ".[dev]"
pytest                 # full suite: no credentials, network, PostgreSQL or Redis required
ruff check app tests alembic
```

The suite (132 tests; 130 run by default, 2 need real PostgreSQL/Redis) uses:
- SQLite (one file per test, WAL mode) and an in-memory KV store
- a recording task dispatcher, plus a helper that runs queued jobs inline like a worker
- the sandbox mailbox, a fake OAuth Gmail, and mocked Gmail HTTP via `respx`
- a fake Anthropic client and the deterministic mock LLM

Covered areas:

| Area | Tests |
|---|---|
| Auth & isolation | registration/login/lockout, refresh rotation + reuse detection, logout, account deletion, 401/404 across users for accounts, emails, drafts, notifications, tasks |
| OAuth | state single-use, forged/missing state, provider mismatch, consent denied, insufficient scope (revoked), mailbox owned by another user, encrypted token storage, frontend redirect |
| Email handling | MIME/charset/attachments parsing, HTML sanitization, malformed content, automated detection, header injection |
| Sync | initial/incremental, checkpoints, new mail, expired cursor resync, lock, revoked credentials, token refresh persistence, provider outage leaving the cursor untouched |
| LLM | schema validation, refusal, truncation, error mapping (429/5xx/connection = transient; 400/401 = permanent), no `temperature` sent |
| Emergency policy | genuine outage/security incidents, "URGENT" marketing false positive, hallucinated evidence, low confidence, prompt injection, no-reply senders |
| Workflow | node order, notification before draft, idempotent re-analysis, LLM outage → pending (nothing invented), permanent error → failed |
| Drafts & sending | auto-draft never sent, placeholders block approval, approval ≠ send, explicit confirm + Idempotency-Key, replay without resend, duplicate-send prevention, edit-after-approval, cross-user send attempts, unknown send outcome |
| Notifications | dedup, minimal external content, retries to the limit, rejected/unconfigured channels, per-account preferences |
| Jobs | status lifecycle, retry → failed at limit, manual retry rules, scheduler skipping inactive or running accounts, retention |
| Infra | Alembic upgrade → no drift vs. models → downgrade; Redis store semantics (fakeredis) |

Real-infrastructure tests (skipped unless configured):

```bash
docker compose up -d postgres redis
TEST_DATABASE_URL=postgresql+asyncpg://email_assistant:<password>@localhost:5432/email_assistant \
TEST_REDIS_URL=redis://localhost:6379/1 pytest -m integration
```

---

## Deployment notes

- Set `ENVIRONMENT=production`. Startup then refuses weak `JWT_SECRET_KEY`, missing
  `TOKEN_ENCRYPTION_KEYS` and `DEBUG=true`, and Swagger is disabled unless `DEBUG` is set.
- Terminate TLS at a reverse proxy or load balancer. Restrict `--forwarded-allow-ips` to it.
- Keep secrets in your platform's secret manager and inject them as environment variables.
- Scale `worker` horizontally (queues can be split across worker pools). Run exactly **one**
  `scheduler`.
- Scrape `/metrics` (Prometheus) from inside the network only. Enable tracing with
  `pip install .[otel]` and `OTEL_EXPORTER_OTLP_ENDPOINT`.
- Google requires app verification for the Gmail scopes before external users can connect.

---

## Limitations and unverified parts

- **Live Gmail:** implemented against the documented REST API and tested with mocked HTTP and a
  fake provider. Not yet exercised against a real Google account (needs your OAuth client).
- **Microsoft Graph:** implemented (OAuth + PKCE, delta sync, reply) but only covered indirectly;
  live behaviour is unverified. Graph's reply endpoint does not return the sent message id.
- **Claude provider:** tested with a fake SDK client. A real call needs `LLM_API_KEY`. The mock
  provider is a keyword heuristic for development only and labels its results `mock-heuristic`.
- **Telegram/SMTP:** implemented with timeouts and retries; not tested against live services.
- **Docker Compose / PostgreSQL / Redis:** configuration validated with `docker compose config`;
  the stack and the `-m integration` tests were not run in the build environment (no Docker
  daemon available there).
- Threads include only messages that were synchronized (the inbox). Sent replies appear only
  after they arrive back through the provider.
