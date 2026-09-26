"""Google Gemini implementation (free tier via AI Studio key, `google-genai` SDK)."""

from __future__ import annotations

import copy
from typing import Any

from google import genai
from google.genai import errors, types
from pydantic import BaseModel

from .base import LLMClient, LLMError, LLMResponseError, T, parse_json_response

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

    def __init__(self, *, api_key: str, model: str, temperature: float = 0.0, timeout_seconds: int = 120):
        self.model = model
        self.temperature = temperature
        self._client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(
                timeout=timeout_seconds * 1000,
                # Policy: no retries anywhere (log-and-skip instead). Disable SDK-level retries explicitly.
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )

    def generate_json(self, *, system: str, prompt: str, schema: type[T], temperature: float | None = None) -> T:
        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=self.temperature if temperature is None else temperature,
            response_mime_type="application/json",
            response_schema=to_gemini_schema(schema),
        )
        try:
            resp = self._client.models.generate_content(model=self.model, contents=prompt, config=config)
        except errors.APIError as e:
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
