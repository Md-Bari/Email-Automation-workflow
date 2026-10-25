"""Application settings loaded from environment variables (never hardcode secrets)."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Application -------------------------------------------------------
    app_name: str = "Email Assistant API"
    environment: Literal["development", "test", "production"] = "development"
    debug: bool = False
    api_v1_prefix: str = "/api/v1"
    # Base URL of the future frontend; used for OAuth result redirects and notification links.
    frontend_url: str = "http://localhost:3000"
    # Public URL of this API (used to build the OAuth redirect URI).
    public_api_url: str = "http://localhost:8000"
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["http://localhost:3000"])
    log_level: str = "INFO"
    log_json: bool = True

    # --- Security ----------------------------------------------------------
    jwt_secret_key: SecretStr = SecretStr("change-me-in-env-at-least-32-characters-long")
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 14
    # Comma-separated Fernet keys. The first key encrypts; all keys decrypt (supports rotation).
    token_encryption_keys: Annotated[list[SecretStr], NoDecode] = Field(default_factory=list)
    login_max_attempts: int = 5
    login_lockout_seconds: int = 900

    # --- Infrastructure ----------------------------------------------------
    database_url: str = "postgresql+asyncpg://email_assistant:email_assistant@localhost:5432/email_assistant"
    database_echo: bool = False
    database_pool_size: int = 10
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str | None = None
    celery_result_backend: str | None = None

    # --- OAuth: Gmail ------------------------------------------------------
    google_client_id: str | None = None
    google_client_secret: SecretStr | None = None
    # Minimum scopes: read mail + send replies. No profile/openid scopes are needed because
    # the mailbox address is read from the Gmail profile endpoint.
    gmail_scopes: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.send",
        ]
    )

    # --- OAuth: Microsoft Graph (optional) ----------------------------------
    microsoft_client_id: str | None = None
    microsoft_client_secret: SecretStr | None = None
    microsoft_tenant: str = "common"

    oauth_state_ttl_seconds: int = 600

    # --- Sandbox -----------------------------------------------------------
    # Enables a mock mailbox provider (seeded sample emails, sends are recorded, never delivered).
    sandbox_mode: bool = True

    # --- Synchronization ---------------------------------------------------
    sync_interval_seconds: int = 300
    initial_sync_days: int = 7
    initial_sync_max_messages: int = 200
    sync_page_size: int = 100
    max_body_chars: int = 100_000
    process_attachments: bool = False
    provider_max_retries: int = 4
    provider_timeout_seconds: float = 20.0

    # --- LLM ---------------------------------------------------------------
    llm_provider: Literal["anthropic", "mock"] = "mock"
    llm_model: str = "claude-opus-5-5"
    llm_api_key: SecretStr | None = None
    # Claude models no longer accept temperature; reasoning depth is controlled with effort.
    llm_effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"
    llm_timeout_seconds: float = 60.0
    llm_max_output_tokens: int = 4096
    llm_max_retries: int = 2
    llm_refusal_fallback: bool = True
    llm_max_input_chars: int = 16_000
    analysis_version: str = "2026.10.1"

    # --- Decision thresholds ----------------------------------------------
    emergency_confidence_threshold: float = 0.75
    review_confidence_threshold: float = 0.55
    # Senders whose automated alerts may legitimately be emergencies (e.g. pagerduty.com).
    trusted_alert_domains: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # --- Notifications -----------------------------------------------------
    telegram_bot_token: SecretStr | None = None
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_from: str | None = None
    smtp_use_tls: bool = True
    notification_timeout_seconds: float = 10.0
    notification_max_attempts: int = 5

    # --- Background jobs ---------------------------------------------------
    task_max_retries: int = 5
    task_manual_retry_limit: int = 3
    task_soft_time_limit_seconds: int = 300
    task_time_limit_seconds: int = 360
    analysis_max_attempts: int = 5

    # --- Retention (days; 0 disables that rule) ---------------------------
    email_body_retention_days: int = 30
    email_retention_days: int = 180
    audit_log_retention_days: int = 365
    task_retention_days: int = 30
    notification_retention_days: int = 90

    # --- Observability -----------------------------------------------------
    metrics_enabled: bool = True
    otel_exporter_otlp_endpoint: str | None = None
    otel_service_name: str = "email-assistant-api"

    @field_validator("cors_origins", "gmail_scopes", "trusted_alert_domains", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("token_encryption_keys", mode="before")
    @classmethod
    def _split_keys(cls, value: object) -> object:
        if isinstance(value, str):
            return [SecretStr(item.strip()) for item in value.split(",") if item.strip()]
        return value

    @model_validator(mode="after")
    def _validate_production(self) -> Settings:
        if self.environment == "production":
            if len(self.jwt_secret_key.get_secret_value()) < 32 or "change-me" in (
                self.jwt_secret_key.get_secret_value()
            ):
                raise ValueError("JWT_SECRET_KEY must be a strong secret in production")
            if not self.token_encryption_keys:
                raise ValueError("TOKEN_ENCRYPTION_KEYS is required in production")
            if self.debug:
                raise ValueError("DEBUG must be false in production")
        return self

    @property
    def broker_url(self) -> str:
        return self.celery_broker_url or self.redis_url

    @property
    def result_backend(self) -> str:
        return self.celery_result_backend or self.redis_url

    @property
    def gmail_enabled(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    @property
    def microsoft_enabled(self) -> bool:
        return bool(self.microsoft_client_id and self.microsoft_client_secret)

    def oauth_redirect_uri(self, provider: str) -> str:
        return f"{self.public_api_url.rstrip('/')}{self.api_v1_prefix}/accounts/{provider}/callback"


@lru_cache
def get_settings() -> Settings:
    return Settings()
