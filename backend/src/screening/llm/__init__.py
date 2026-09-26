"""LLM clients. `make_client` is the only place a provider is chosen."""

from __future__ import annotations

from ..config import AppConfig, ConfigError
from .base import LLMClient, LLMError, LLMResponseError

__all__ = ["LLMClient", "LLMError", "LLMResponseError", "make_client"]


class FallbackClient(LLMClient):
    """Tries each client in order; the next one answers when a call fails (overloaded, quota used
    up, bad output). `model` is the model that answered last, so records name the real author."""

    def __init__(self, clients: list[LLMClient], logger=None):
        self.clients = clients
        self.provider = clients[0].provider
        self.model = clients[0].model
        self._log = logger

    def _order(self) -> list[LLMClient]:
        """Models known to be out of quota go last (still tried if nothing else answers)."""
        cool = [c for c in self.clients if getattr(c, "cooling_down", lambda: False)()]
        return [c for c in self.clients if c not in cool] + cool

    def generate_json(self, *, system, prompt, schema, temperature=None):
        last = None
        order = self._order()
        for c in order:
            try:
                out = c.generate_json(system=system, prompt=prompt, schema=schema, temperature=temperature)
                self.provider, self.model = c.provider, c.model
                return out
            except LLMError as e:
                last = e
                if self._log and c is not order[-1]:
                    self._log.warning("LLM %s/%s failed (%s); trying the next fallback model", c.provider, c.model,
                                      str(e)[:160])
        raise last

    def list_models(self) -> list[str]:
        return self.clients[0].list_models()


def make_client(config: AppConfig) -> LLMClient:
    import logging

    llm = config.settings.llm
    if llm.provider == "gemini":
        from .gemini import GeminiClient

        models = [llm.model] + [m for m in llm.fallback_models if m != llm.model]
        clients = [GeminiClient(api_key=config.api_key(), model=m, temperature=llm.temperature,
                                timeout_seconds=llm.timeout_seconds, retry_delays=tuple(llm.transient_retry_delays))
                   for m in models]
        return clients[0] if len(clients) == 1 else FallbackClient(clients, logging.getLogger("screening"))
    raise ConfigError(f"Unsupported llm.provider '{llm.provider}' (supported: gemini)")
