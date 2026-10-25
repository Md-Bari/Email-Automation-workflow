"""Deterministic heuristic provider for development and tests (no network, no API key).

It is intentionally simple and labelled ``mock-heuristic`` in stored results so its output is
never mistaken for a real model's analysis. Do not use it in production.
"""

from __future__ import annotations

from typing import Any, cast

from app.agents.rules import compute_signals
from app.integrations.llm.base import LLMError, LLMProvider, T
from app.integrations.llm.schemas import EmailAssessment, EmailCategory, EmergencyAssessment, ReplyDraft


class MockLLMProvider(LLMProvider):
    model_name = "mock-heuristic"

    async def structured(
        self,
        *,
        system: str,
        user: str,
        schema: type[T],
        context: dict[str, Any] | None = None,
    ) -> T:
        ctx = context or {}
        if schema is EmailAssessment:
            return cast(T, self._assess(ctx))
        if schema is ReplyDraft:
            return cast(T, self._reply(ctx))
        raise LLMError(f"Mock provider does not support {schema.__name__}")

    def _assess(self, ctx: dict[str, Any]) -> EmailAssessment:
        subject, body = ctx.get("subject", ""), ctx.get("body", "")
        signals = compute_signals(
            subject=subject, body=body, sender_email=ctx.get("sender_email", ""),
            is_automated=bool(ctx.get("is_automated")), trusted_alert_domains=[],
        )
        text = f"{subject}\n{body}"
        sentences = [s.strip() for s in body.replace("\n", " ").split(". ") if s.strip()]
        evidence = [s.rstrip(".") for s in sentences if any(k in s.lower() for k in (
            "500", "failing", "compromised", "sign-in", "losing", "breach", "within", "export"))][:3]

        if signals.prompt_injection:
            category, emergency = EmailCategory.PHISHING_OR_SPAM, False
        elif signals.automated_sender and not signals.emergency_signals:
            category, emergency = EmailCategory.MARKETING, False
        elif "security" in signals.emergency_signals:
            category, emergency = EmailCategory.SECURITY_INCIDENT, True
        elif "outage" in signals.emergency_signals:
            category, emergency = EmailCategory.PRODUCTION_INCIDENT, True
        elif "invoice" in text.lower():
            category, emergency = EmailCategory.TRANSACTIONAL, False
        elif "meet" in text.lower():
            category, emergency = EmailCategory.MEETING, False
        elif "?" in body:
            category, emergency = EmailCategory.REQUEST, False
        else:
            category, emergency = EmailCategory.INFORMATIONAL, False

        priority = "urgent" if emergency else ("high" if signals.urgency_words else "normal")
        if category in (EmailCategory.MARKETING, EmailCategory.TRANSACTIONAL):
            priority = "low"
        reply = category in (EmailCategory.REQUEST, EmailCategory.MEETING) or emergency
        summary = (sentences[0] if sentences else subject or "(empty email)")[:300]
        return EmailAssessment(
            summary=summary,
            intent={EmailCategory.MEETING: "schedule a meeting", EmailCategory.REQUEST: "request"}.get(
                category, category.value.replace("_", " ")),
            category=category,
            priority=priority,  # type: ignore[arg-type]
            emergency=EmergencyAssessment(
                is_emergency=emergency,
                reason=f"Detected {', '.join(signals.emergency_signals)} with concrete impact" if emergency else None,
                evidence=evidence if emergency else [],
                concrete_consequence="Service or security impact described in the email" if emergency else None,
            ),
            suggested_action="Respond and coordinate immediately" if emergency else (
                "Reply to the sender" if reply else "No action needed"),
            reply_recommended=reply,
            confidence=0.85 if emergency or category != EmailCategory.INFORMATIONAL else 0.6,
            prompt_injection_suspected=signals.prompt_injection,
        )

    def _reply(self, ctx: dict[str, Any]) -> ReplyDraft:
        name = (ctx.get("sender_name") or "").split(" ")[0] or "there"
        tone = ctx.get("tone", "professional")
        greeting = {"friendly": f"Hi {name}!", "formal": f"Dear {ctx.get('sender_name') or 'Sir or Madam'},"}.get(
            tone, f"Hi {name},")
        body = (
            f"{greeting}\n\nThank you for your message regarding \"{ctx.get('subject', '')}\". "
            "I have received it and will follow up [with details / by proposed time].\n\n"
            "Best regards,\n[Your name]"
        )
        return ReplyDraft(body=body, needs_clarification=True,
                          unresolved_placeholders=["[with details / by proposed time]", "[Your name]"])
