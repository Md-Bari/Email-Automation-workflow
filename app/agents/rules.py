"""Deterministic business rules that run alongside the LLM.

Rules produce *signals* and enforce *policy*; they never make an email an emergency on keywords
alone. The final decision combines the LLM assessment with these checks (see ``apply_policy``).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from app.db.models.enums import PRIORITY_RANK, Priority
from app.integrations.email_providers.parsing import is_noreply_address
from app.integrations.llm.schemas import EMERGENCY_CATEGORIES, EmailAssessment, EmailCategory

_PATTERNS: dict[str, re.Pattern[str]] = {
    "outage": re.compile(
        r"\b(outage|is down|went down|returns? (?:http )?5\d\d|all (?:requests|checkouts|customers)|"
        r"service unavailable|failing for (?:all|every))\b",
        re.I,
    ),
    "security": re.compile(
        r"\b(breach|compromised|unauthori[sz]ed (?:access|sign-?in|login)|ransomware|data leak|"
        r"suspicious (?:sign-?in|login|activity)|credentials? (?:leak|exposed|stolen))\b",
        re.I,
    ),
    "deadline": re.compile(
        r"\b(within (?:the next )?\d+ (?:minutes|hours)|by (?:end of day|eod|tonight|today)|"
        r"deadline (?:is )?(?:today|tonight|in \d+ hours?))\b",
        re.I,
    ),
    "threat": re.compile(r"\b(bomb threat|fire alarm|evacuat\w+|injur\w+|threaten\w* to)\b", re.I),
    "financial_impact": re.compile(r"(\$\s?\d[\d,.]*\s?(?:k|m)?\s?(?:per|an|/)\s?hour|losing (?:money|revenue))", re.I),
}
_URGENCY_WORDS = re.compile(r"\b(urgent|asap|important|immediately|act now|right away|critical)\b", re.I)
_INJECTION = re.compile(
    r"(ignore (?:all |any )?(?:previous|prior|above) instructions|disregard (?:the|your) (?:rules|instructions)|"
    r"system prompt|notice to the ai|as an ai (?:assistant|model)|you are now|"
    r"forward all (?:emails|messages)|reveal (?:your|the) (?:password|instructions|secrets?))",
    re.I,
)
_WS = re.compile(r"\s+")


@dataclass(slots=True)
class RuleSignals:
    emergency_signals: list[str] = field(default_factory=list)
    urgency_words: list[str] = field(default_factory=list)
    prompt_injection: bool = False
    automated_sender: bool = False
    noreply_sender: bool = False
    trusted_alert_sender: bool = False

    @property
    def urgency_language_only(self) -> bool:
        return bool(self.urgency_words) and not self.emergency_signals

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["urgency_language_only"] = self.urgency_language_only
        return data


def compute_signals(
    *, subject: str, body: str, sender_email: str, is_automated: bool, trusted_alert_domains: list[str]
) -> RuleSignals:
    text = f"{subject}\n{body}"
    domain = sender_email.rsplit("@", 1)[-1].lower()
    return RuleSignals(
        emergency_signals=sorted(name for name, rx in _PATTERNS.items() if rx.search(text)),
        urgency_words=sorted({m.group(0).lower() for m in _URGENCY_WORDS.finditer(text)}),
        prompt_injection=bool(_INJECTION.search(text)),
        automated_sender=is_automated,
        noreply_sender=is_noreply_address(sender_email),
        trusted_alert_sender=any(domain == d or domain.endswith("." + d) for d in trusted_alert_domains),
    )


def _norm(text: str) -> str:
    return _WS.sub(" ", text).strip().lower()


def verify_evidence(evidence: list[str], *, subject: str, body: str) -> list[str]:
    """Keep only quotes that actually occur in the email (guards against hallucinated evidence)."""
    haystack = _norm(f"{subject}\n{body}")
    verified = []
    for quote in evidence:
        q = _norm(quote.strip("\"'“”‘’ "))
        if len(q) >= 8 and q in haystack:
            verified.append(quote.strip())
    return verified


@dataclass(slots=True)
class PolicyDecision:
    priority: Priority
    is_emergency: bool
    emergency_reason: str | None
    emergency_evidence: list[str]
    reply_recommended: bool
    confidence: float
    needs_review: bool
    review_reason: str | None
    emergency_claim_rejected: bool
    adjustments: list[str]


def apply_policy(
    assessment: EmailAssessment,
    signals: RuleSignals,
    *,
    subject: str,
    body: str,
    emergency_threshold: float,
    review_threshold: float,
) -> PolicyDecision:
    adjustments: list[str] = []
    review_reasons: list[str] = []
    priority = Priority(assessment.priority)
    confidence = assessment.confidence
    em = assessment.emergency
    evidence = verify_evidence(em.evidence, subject=subject, body=body)
    injection = signals.prompt_injection or assessment.prompt_injection_suspected

    is_emergency = em.is_emergency
    rejected = False
    if is_emergency:
        failures = []
        if assessment.category not in EMERGENCY_CATEGORIES:
            failures.append("category_not_emergency")
        if not evidence:
            failures.append("evidence_not_found_in_email")
        if injection:
            failures.append("prompt_injection_suspected")
        if assessment.category == EmailCategory.MARKETING or (
            signals.automated_sender and signals.urgency_language_only and not signals.trusted_alert_sender
        ):
            failures.append("bulk_mail_urgency_language")
        if confidence < emergency_threshold:
            failures.append("confidence_below_emergency_threshold")
        if failures:
            is_emergency = False
            rejected = True
            adjustments.extend(f"emergency_rejected:{f}" for f in failures)
            review_reasons.append("Emergency claim did not pass verification: " + ", ".join(failures))

    if is_emergency:
        if PRIORITY_RANK[priority] < PRIORITY_RANK[Priority.URGENT]:
            adjustments.append("priority_raised_to_urgent")
        priority = Priority.URGENT
    elif priority == Priority.URGENT and (signals.urgency_language_only or injection):
        # "URGENT!!!" in a newsletter or an injection attempt does not earn top priority.
        priority = Priority.HIGH if not signals.automated_sender else Priority.NORMAL
        adjustments.append("urgent_priority_downgraded")

    if injection:
        review_reasons.append("Email contains instructions aimed at an AI system")
        if PRIORITY_RANK[priority] > PRIORITY_RANK[Priority.NORMAL]:
            priority = Priority.NORMAL
            adjustments.append("priority_capped_prompt_injection")

    reply = assessment.reply_recommended
    if reply and (signals.noreply_sender or signals.automated_sender or injection):
        reply = False
        adjustments.append("reply_suppressed_automated_or_untrusted_sender")

    if confidence < review_threshold:
        review_reasons.append(f"Low confidence ({confidence:.2f})")

    return PolicyDecision(
        priority=priority,
        is_emergency=is_emergency,
        emergency_reason=em.reason if is_emergency else None,
        emergency_evidence=evidence if is_emergency else [],
        reply_recommended=reply,
        confidence=confidence,
        needs_review=bool(review_reasons),
        review_reason="; ".join(review_reasons)[:500] if review_reasons else None,
        emergency_claim_rejected=rejected,
        adjustments=adjustments,
    )
