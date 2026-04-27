from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, Field

from cai.github.bot import CaiBot
from cai.github.issues import IssueMeta

_MAX_REFERENCE_FILE_BYTES = 100_000


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
    reference_files: list[str] = Field(
        description=(
            "Curated list of repo-relative paths the implementation agent should "
            "treat as required reading. Start from the explore agent's list, then "
            "add any files your refined plan now depends on and drop ones that "
            "turned out to be irrelevant."
        )
    )
    sub_issues: list[str] = Field(
        default_factory=list,
        description=(
            "Titles of decomposed sub-tasks to create as child GitHub issues. "
            "Each string is the title of one new sub-issue. "
            "Leave empty for single-feature issues."
        ),
    )


class ImplementOutput(BaseModel):
    summary: str = Field(description="Concise description of code changes made.")
    commit_message: str = Field(description="Git commit message for the changes.")


@dataclass
class IssueState:
    bot: CaiBot
    meta: IssueMeta
    body_path: Path
    repo_root: Path
    body: str = field(default="")
    meta_json: str = field(default="")
    findings: ExploreOutput | None = field(default=None)
    new_meta: IssueMeta | None = field(default=None)
    refine_output: RefineOutput | None = field(default=None)
    implement_output: ImplementOutput | None = field(default=None)
    branch_name: str | None = field(default=None)
    pr_url: str | None = field(default=None)
    reference_files: list[str] = field(default_factory=list)

    def reference_files_section(self) -> str:
        """Render ``reference_files`` as a markdown section ready to splice into a prompt.

        Returns an empty string when no readable files remain after filtering
        (missing paths and oversized files are silently dropped).
        """
        sections: list[str] = []
        for path_str in self.reference_files:
            p = Path(path_str)
            if not p.is_absolute():
                p = self.repo_root / p
            try:
                p = p.resolve()
                if not p.is_file():
                    continue
                if p.stat().st_size > _MAX_REFERENCE_FILE_BYTES:
                    continue
                rel = p.relative_to(self.repo_root)
                sections.append(f"### {rel}\n\n```\n{p.read_text()}\n```")
            except (ValueError, OSError):
                pass
        if not sections:
            return ""
        return "## Reference files\n\n" + "\n\n".join(sections)
