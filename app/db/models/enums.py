from __future__ import annotations

from enum import StrEnum


class EmailProvider(StrEnum):
    GMAIL = "gmail"
    MICROSOFT = "microsoft"
    SANDBOX = "sandbox"


class ConnectionStatus(StrEnum):
    ACTIVE = "active"
    REAUTH_REQUIRED = "reauth_required"
    REVOKED = "revoked"
    ERROR = "error"


class ProcessingStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    ANALYZED = "analyzed"
    SKIPPED = "skipped"
    FAILED = "failed"


class Priority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


PRIORITY_RANK = {Priority.LOW: 0, Priority.NORMAL: 1, Priority.HIGH: 2, Priority.URGENT: 3}


class DraftStatus(StrEnum):
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    SENDING = "sending"
    SENT = "sent"
    REJECTED = "rejected"
    FAILED = "failed"


class ReplyTone(StrEnum):
    PROFESSIONAL = "professional"
    FRIENDLY = "friendly"
    CONCISE = "concise"
    FORMAL = "formal"


class NotificationType(StrEnum):
    EMERGENCY = "emergency"
    NEEDS_REVIEW = "needs_review"
    SYSTEM = "system"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class Channel(StrEnum):
    IN_APP = "in_app"
    EMAIL = "email"
    TELEGRAM = "telegram"


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"
    SKIPPED = "skipped"


class TaskType(StrEnum):
    INITIAL_SYNC = "initial_sync"
    INCREMENTAL_SYNC = "incremental_sync"
    ANALYZE_EMAIL = "analyze_email"
    GENERATE_DRAFT = "generate_draft"
    DELIVER_NOTIFICATION = "deliver_notification"
    CLEANUP = "cleanup"


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    RETRYING = "retrying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class AuditOutcome(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    DENIED = "denied"
