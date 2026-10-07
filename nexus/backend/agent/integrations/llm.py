"""Provider-neutral chat model factory for the Nexus banking agent.

Every model call uses the configured OpenAI-compatible endpoint. Switching
providers only changes environment configuration; business and Agent code do
not depend on a vendor name or protocol.
"""
from __future__ import annotations

from langchain_core.language_models import BaseChatModel

from ...core.config import settings


def _openai_compatible_model() -> BaseChatModel:
    """Create a model for any OpenAI-compatible provider endpoint."""
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(
        model=settings.llm_model or "chat-model",
        api_key=settings.llm_api_key.get_secret_value() or "missing-api-key",
        base_url=settings.llm_base_url or "https://api.openai.com/v1",
        temperature=0,
        max_tokens=1800,
        max_retries=settings.llm_max_retries,
        timeout=settings.llm_timeout,
    )


def _mock_model() -> BaseChatModel:
    """Deterministic local model used when no live provider is configured.

    It does not call the network; it is only used to exercise the Agent loop in
    tests/offline demos without making a real LLM request.
    """
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    return FakeMessagesListChatModel(responses=[])


def get_chat_model() -> BaseChatModel | None:
    """Return a bound chat model for the configured provider, or ``None`` when
    no live model is enabled.

    Returning ``None`` lets callers fall back to the deterministic local paths
    (guard / exact commands / rules) exactly like today.
    """
    if not settings.llm_enabled:
        return None
    if settings.llm_provider == "mock":
        return _mock_model()
    if not settings.llm_api_key.get_secret_value():
        return None
    return _openai_compatible_model()


def model_status() -> str:
    """Human-readable provider status used in the health/capability payload."""
    if not settings.llm_enabled:
        return "off"
    return settings.llm_provider
