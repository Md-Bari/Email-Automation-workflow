"""LLM output validation and Anthropic provider error handling (with a fake SDK client)."""

from types import SimpleNamespace

import anthropic
import httpx
import pytest
from pydantic import ValidationError

from app.core.config import get_settings
from app.integrations.llm.anthropic_provider import FALLBACK_BETA, AnthropicProvider
from app.integrations.llm.base import LLMError, LLMOutputError, LLMRefusalError, LLMTransientError
from app.integrations.llm.schemas import EmailAssessment, ReplyDraft

VALID = {
    "summary": "Payments API is down.", "intent": "incident report", "category": "production_incident",
    "priority": "urgent",
    "emergency": {"is_emergency": True, "reason": "outage", "evidence": ["returns HTTP 500"],
                  "concrete_consequence": "lost revenue"},
    "suggested_action": "Roll back", "reply_recommended": True, "confidence": 0.9,
    "prompt_injection_suspected": False,
}


def test_schema_accepts_valid_output():
    assert EmailAssessment.model_validate(VALID).priority == "urgent"


@pytest.mark.parametrize("patch", [
    {"confidence": 1.5}, {"priority": "critical"}, {"category": "unknown"}, {"summary": "  "},
    {"extra_field": 1},
])
def test_schema_rejects_invalid_output(patch):
    with pytest.raises(ValidationError):
        EmailAssessment.model_validate({**VALID, **patch})


def test_reply_schema_rejects_empty_body():
    with pytest.raises(ValidationError):
        ReplyDraft.model_validate({"body": " ", "needs_clarification": False, "unresolved_placeholders": []})


class FakeMessages:
    def __init__(self, result=None, exc=None):
        self.result, self.exc, self.calls = result, exc, []

    async def parse(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc:
            raise self.exc
        return self.result


def provider_with(result=None, exc=None):
    messages = FakeMessages(result, exc)
    client = SimpleNamespace(beta=SimpleNamespace(messages=messages))
    return AnthropicProvider(get_settings(), client=client), messages


def response(stop_reason="end_turn", parsed=None):
    return SimpleNamespace(stop_reason=stop_reason, parsed_output=parsed, model="claude-opus-5-5",
                           usage=SimpleNamespace(input_tokens=10, output_tokens=5))


async def test_anthropic_success_sends_structured_output_and_fallback():
    parsed = EmailAssessment.model_validate(VALID)
    provider, messages = provider_with(response(parsed=parsed))
    result = await provider.structured(system="sys", user="email", schema=EmailAssessment)
    assert result == parsed
    call = messages.calls[0]
    assert call["output_format"] is EmailAssessment
    assert call["output_config"] == {"effort": "low"}
    assert call["betas"] == [FALLBACK_BETA] and call["fallbacks"] == "default"
    assert "temperature" not in call


async def test_anthropic_refusal_and_truncation():
    provider, _ = provider_with(response(stop_reason="refusal"))
    with pytest.raises(LLMRefusalError):
        await provider.structured(system="s", user="u", schema=EmailAssessment)
    provider, _ = provider_with(response(stop_reason="max_tokens"))
    with pytest.raises(LLMOutputError):
        await provider.structured(system="s", user="u", schema=EmailAssessment)
    provider, _ = provider_with(response(parsed=None))
    with pytest.raises(LLMOutputError):
        await provider.structured(system="s", user="u", schema=EmailAssessment)


def _status_error(cls, code):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("error", response=httpx.Response(code, request=req), body=None)


@pytest.mark.parametrize("exc,expected", [
    (lambda: _status_error(anthropic.RateLimitError, 429), LLMTransientError),
    (lambda: _status_error(anthropic.InternalServerError, 500), LLMTransientError),
    (lambda: anthropic.APIConnectionError(request=httpx.Request("POST", "https://x")), LLMTransientError),
    (lambda: _status_error(anthropic.AuthenticationError, 401), LLMError),
    (lambda: _status_error(anthropic.BadRequestError, 400), LLMError),
])
async def test_anthropic_error_mapping(exc, expected):
    provider, _ = provider_with(exc=exc())
    with pytest.raises(expected) as info:
        await provider.structured(system="s", user="u", schema=EmailAssessment)
    if expected is LLMError:
        assert not info.value.retryable


def test_anthropic_provider_requires_api_key():
    with pytest.raises(LLMError):
        AnthropicProvider(get_settings())
