"""Google Gemini implementation (free tier via AI Studio key, `google-genai` SDK)."""

from __future__ import annotations

import copy
import re
import threading
import time
from typing import Any

from google import genai
from google.genai import errors, types
from pydantic import BaseModel

from .base import LLMClient, LLMError, LLMResponseError, T, parse_json_response

# Gemini errors worth one more try after a short wait: 429 rate limit, 503 overloaded.
TRANSIENT_CODES = {429, 503}

# A model whose quota is used up is skipped until Google says it resets (shared by every client in
# this process, so one refusal isn't paid for again on every request).
DEFAULT_COOLDOWN_SECONDS = 600
MAX_COOLDOWN_SECONDS = 3600
_cooldown: dict[str, float] = {}
_cooldown_lock = threading.Lock()


def cooling_until(model: str) -> float:
    with _cooldown_lock:
        return _cooldown.get(model, 0.0)


def _cool(model: str, seconds: float) -> None:
    with _cooldown_lock:
        _cooldown[model] = time.time() + max(30.0, min(seconds, MAX_COOLDOWN_SECONDS))


def _quota_exhausted(e: errors.APIError) -> bool:
    """429 because the quota is used up (per day / per minute), not a momentary burst."""
    return e.code == 429 and "quota" in f"{e.status} {e.message}".lower()


def _retry_delay(e: errors.APIError) -> float:
    """Google's RetryInfo.retryDelay ("34s"), or the default cooldown."""
    for d in ((e.details or {}).get("error", {}) or {}).get("details", []) if isinstance(e.details, dict) else []:
        m = re.match(r"^(\d+(?:\.\d+)?)s$", str(d.get("retryDelay", "")))
        if m:
            return float(m.group(1))
    return DEFAULT_COOLDOWN_SECONDS


# Keys from Pydantic's JSON Schema that Gemini's response_schema (OpenAPI subset) does not accept.
_DROP_KEYS = {"title", "additionalProperties", "default", "$defs", "examples"}


def to_gemini_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Pydantic model -> self-contained OpenAPI-style schema dict for `response_schema`.

    Inlines $ref, turns `X | None` (anyOf [X, null]) into `nullable: true`, and
    drops unsupported keywords. Output is still validated by Pydantic afterwards,
    so this only needs to *guide* the model, not be a perfect translation.
    """
    root = model.model_json_schema()
    defs = root.get("$defs", {})

    def conv(node: Any) -> Any:
        if isinstance(node, list):
            return [conv(n) for n in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            target = copy.deepcopy(defs[node["$ref"].split("/")[-1]])
            extra = {k: v for k, v in node.items() if k != "$ref"}
            return conv({**target, **extra})
        if "anyOf" in node:
            variants = [v for v in node["anyOf"] if v.get("type") != "null"]
            nullable = len(variants) < len(node["anyOf"])
            rest = {k: v for k, v in node.items() if k != "anyOf"}
            if len(variants) == 1:
                merged = conv({**variants[0], **rest})
            else:
                merged = conv({**rest, "anyOf": variants})
            if nullable:
                merged["nullable"] = True
            return merged
        out = {}
        for k, v in node.items():
            if k in _DROP_KEYS:
                continue
            if k == "const":  # single-value Literal
                out["enum"] = [v]
                continue
            out[k] = {pk: conv(pv) for pk, pv in v.items()} if k == "properties" else conv(v)
        if "properties" in out:
            out["propertyOrdering"] = list(out["properties"].keys())
        return out

    return conv(root)


class GeminiClient(LLMClient):
    provider = "gemini"

    def __init__(self, *, api_key: str, model: str, temperature: float = 0.0, timeout_seconds: int = 120,
                 retry_delays: tuple[float, ...] = (2.0, 5.0)):
        self.model = model
        self.temperature = temperature
        self.retry_delays = tuple(retry_delays)  # only for TRANSIENT_CODES; () = never retry
        self._sleep = time.sleep
        self._client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(
                timeout=timeout_seconds * 1000,
                # SDK-level retries stay off; the only retries are the short transient ones above.
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )

    def cooling_down(self) -> bool:
        return cooling_until(self.model) > time.time()

    def generate_json(self, *, system: str, prompt: str, schema: type[T], temperature: float | None = None) -> T:
        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=self.temperature if temperature is None else temperature,
            response_mime_type="application/json",
            response_schema=to_gemini_schema(schema),
        )
        for attempt in range(len(self.retry_delays) + 1):
            try:
                resp = self._client.models.generate_content(model=self.model, contents=prompt, config=config)
                break
            except errors.APIError as e:
                if _quota_exhausted(e):  # waiting a few seconds won't help: skip this model for a while
                    _cool(self.model, _retry_delay(e))
                    raise LLMError(f"Gemini API error {e.code} {e.status}: {e.message}") from e
                # Overloaded (503) / rate-limited (429) are temporary: wait briefly and try again.
                # Everything else fails at once (log-and-skip).
                if e.code in TRANSIENT_CODES and attempt < len(self.retry_delays):
                    self._sleep(self.retry_delays[attempt])
                    continue
                raise LLMError(f"Gemini API error {e.code} {e.status}: {e.message}") from e
            except Exception as e:  # network/timeouts surface as httpx errors
                raise LLMError(f"Gemini request failed: {type(e).__name__}: {e}") from e

        text = resp.text
        if not text:
            raise LLMResponseError(f"Gemini returned no text ({_why_empty(resp)})")
        cand = resp.candidates[0] if resp.candidates else None
        if cand is not None and cand.finish_reason not in (None, types.FinishReason.STOP):
            # e.g. MAX_TOKENS: JSON is probably truncated; fail loudly instead of trusting partial output.
            raise LLMResponseError(f"Gemini stopped early: finish_reason={cand.finish_reason}")
        return parse_json_response(text, schema)

    def list_models(self) -> list[str]:
        try:
            return sorted(m.name.removeprefix("models/") for m in self._client.models.list()
                          if "generateContent" in (m.supported_actions or []))
        except errors.APIError as e:
            raise LLMError(f"Gemini API error {e.code} {e.status}: {e.message}") from e


def _why_empty(resp) -> str:
    fb = getattr(resp, "prompt_feedback", None)
    if fb and fb.block_reason:
        return f"prompt blocked: {fb.block_reason}"
    if resp.candidates:
        return f"finish_reason={resp.candidates[0].finish_reason}"
    return "no candidates"
