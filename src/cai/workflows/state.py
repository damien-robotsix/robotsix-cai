from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, Field

from cai.github.issues import IssueMeta


class ExploreOutput(BaseModel):
    summary: str = Field(
        description="Concise description of codebase findings relevant to the issue."
    )
    related_files: list[str] = Field(
        description=(
            "Paths to the files most relevant to the issue, "
            "relative to the repository root."
        )
    )


class RefineOutput(BaseModel):
    """Structured metadata changes. The body is mutated on disk by the agent."""

    title: str = Field(description="Refined title (or the original if already clear).")


@dataclass
class IssueState:
    meta: IssueMeta
    body_path: Path
    repo_root: Path
    body: str = field(default="")
    meta_json: str = field(default="")
    findings: ExploreOutput | None = field(default=None)
    new_meta: IssueMeta | None = field(default=None)
    refine_output: RefineOutput | None = field(default=None)
