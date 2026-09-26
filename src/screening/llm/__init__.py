"""LLM clients. `make_client` is the only place a provider is chosen."""

from __future__ import annotations

from ..config import AppConfig, ConfigError
from .base import LLMClient, LLMError, LLMResponseError

__all__ = ["LLMClient", "LLMError", "LLMResponseError", "make_client"]


def make_client(config: AppConfig) -> LLMClient:
    llm = config.settings.llm
    if llm.provider == "gemini":
        from .gemini import GeminiClient

        return GeminiClient(api_key=config.api_key(), model=llm.model,
                            temperature=llm.temperature, timeout_seconds=llm.timeout_seconds)
    raise ConfigError(f"Unsupported llm.provider '{llm.provider}' (supported: gemini)")
