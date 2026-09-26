"""Filesystem locations. Everything is resolved from the project root."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"
PROMPTS_DIR = PROJECT_ROOT / "prompts"
SCHEMAS_DIR = PROJECT_ROOT / "schemas"
TEMPLATES_DIR = PROJECT_ROOT / "templates"
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"


@dataclass(frozen=True)
class DataPaths:
    """Resolved data-directory layout (spec §2)."""

    root: Path
    input_resumes: Path
    candidates_index: Path
    stage1_output: Path
    stage2_output: Path
    stage3_output: Path
    stage3_transcripts: Path
    stage3_sessions: Path
    stage3_invites: Path
    stage4_output: Path
    credibility_output: Path
    jobs_output: Path
    logs: Path

    @property
    def pipeline_log(self) -> Path:
        return self.logs / "pipeline.log"

    @property
    def failures_log(self) -> Path:
        return self.logs / "failures.jsonl"

    def ensure(self) -> None:
        for p in (
            self.input_resumes,
            self.stage1_output,
            self.stage2_output,
            self.stage3_output,
            self.stage3_transcripts,
            self.stage3_sessions,
            self.stage4_output,
            self.credibility_output,
            self.jobs_output,
            self.logs,
        ):
            p.mkdir(parents=True, exist_ok=True)

    def rel(self, path: Path) -> str:
        """Path as stored in JSON: relative to the project root when possible, else absolute."""
        path = Path(path).resolve()
        try:
            return path.relative_to(PROJECT_ROOT).as_posix()
        except ValueError:
            return path.as_posix()


def resolve_stored(stored: str) -> Path:
    """Inverse of DataPaths.rel(): a path string from a JSON file -> absolute Path."""
    p = Path(stored)
    return p if p.is_absolute() else PROJECT_ROOT / p


def build_data_paths(data_dir: Path, layout: dict[str, str]) -> DataPaths:
    root = Path(data_dir).resolve()
    return DataPaths(root=root, **{k: root / v for k, v in layout.items()})
