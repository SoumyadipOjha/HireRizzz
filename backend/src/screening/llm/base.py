"""Provider-neutral LLM interface. Stages depend only on this module."""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from typing import TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    """The provider call failed (network, auth, quota, safety block...)."""


class LLMResponseError(LLMError):
    """The provider answered, but not with JSON matching the schema."""


class LLMClient(ABC):
    provider: str
    model: str

    @abstractmethod
    def generate_json(self, *, system: str, prompt: str, schema: type[T], temperature: float | None = None) -> T:
        """Return the model's answer parsed and validated as `schema`. Raises LLMError.
        `temperature` overrides the client default for this call."""


_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def parse_json_response(text: str, schema: type[T]) -> T:
    """Validate raw model text against `schema`. Tolerates a ```json fence; nothing else."""
    if not text or not text.strip():
        raise LLMResponseError("empty response")
    m = _FENCE.match(text)
    body = m.group(1) if m else text.strip()
    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        raise LLMResponseError(f"response is not valid JSON ({e.msg} at pos {e.pos}): {body[:300]!r}") from e
    try:
        return schema.model_validate(data)
    except ValidationError as e:
        errs = "; ".join(f"{'.'.join(map(str, x['loc'])) or '<root>'}: {x['msg']}" for x in e.errors()[:8])
        raise LLMResponseError(f"response does not match {schema.__name__}: {errs}") from e
