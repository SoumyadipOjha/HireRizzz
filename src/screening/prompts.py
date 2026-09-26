"""Prompt files live under prompts/<stage>/ and use {{placeholder}} substitution."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .paths import PROJECT_ROOT, PROMPTS_DIR

_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")


class PromptError(Exception):
    """Prompt file missing or placeholders don't match."""


@dataclass(frozen=True)
class Prompt:
    path: Path
    text: str

    @property
    def rel_path(self) -> str:
        return self.path.relative_to(PROJECT_ROOT).as_posix()

    def render(self, **values: str) -> str:
        needed = set(_PLACEHOLDER.findall(self.text))
        missing = needed - values.keys()
        extra = values.keys() - needed
        if missing or extra:
            raise PromptError(f"{self.rel_path}: missing placeholders {sorted(missing)}, unexpected {sorted(extra)}")
        # Single pass: values containing "{{...}}" (e.g. resume text) are never re-expanded.
        return _PLACEHOLDER.sub(lambda m: str(values[m.group(1)]), self.text)


def load_prompt(stage_dir: str, name: str) -> Prompt:
    path = PROMPTS_DIR / stage_dir / name
    if not path.is_file():
        raise PromptError(f"Prompt file not found: {path}")
    return Prompt(path=path, text=path.read_text(encoding="utf-8").strip() + "\n")
