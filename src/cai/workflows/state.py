from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from cai.github.bot import CaiBot
from cai.github.issues import IssueMeta
from cai.github.pr import ReviewThread

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


class ThreadReply(BaseModel):
    """Per-thread reply produced when implementing review-comment fixes.

    ``action`` is per-thread even though the resulting commit is bundled:
    we resolve a thread on GitHub only when the agent intended a fix for
    that specific thread.
    """

    thread_id: str = Field(description="GraphQL node id of the review thread.")
    action: Literal["fix", "reply_only"] = Field(
        description="Whether the agent edited code for this thread (fix) or only wants to reply (reply_only)."
    )
    reply: str = Field(description="Message to post as a reply on the thread.")


class ImplementOutput(BaseModel):
    summary: str = Field(description="Concise description of code changes made.")
    commit_message: str = Field(description="Git commit message for the changes.")
    required_checks: list[Literal["documentation", "python"]] = Field(
        default_factory=list,
        description=(
            "Checks required for this MR. "
            "Include 'documentation' if docs/ or other documentation may need updating. "
            "Valid values: 'documentation'."
        ),
    )
    replies: list[ThreadReply] = Field(
        default_factory=list,
        description=(
            "Per-thread replies. Populate only when review-comment threads are "
            "in the prompt — one entry per thread. Leave empty otherwise."
        ),
    )


class TestOutput(BaseModel):
    summary: str = Field(description="Concise description of tests written or updated.")
    commit_message: str = Field(
        default="",
        description="Git commit message for the test changes, or empty string if nothing changed.",
    )


class DocsOutput(BaseModel):
    summary: str = Field(
        description="Concise description of documentation changes made (or why none were needed)."
    )
    commit_message: str = Field(
        description="Git commit message for the docs changes, or empty string if nothing changed."
    )


class PythonReviewOutput(BaseModel):
    summary: str = Field(
        description="Bulleted list of issues found and fixed per file, or 'No issues found.' if nothing changed."
    )
    commit_message: str = Field(
        description="Git commit message for the review fixes, or empty string if nothing changed."
    )


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
    test_output: TestOutput | None = field(default=None)
    tests_passed: bool | None = field(default=None)
    test_failure_details: str = field(default="")
    test_retry_count: int = field(default=0)
    python_review_output: PythonReviewOutput | None = field(default=None)
    docs_output: DocsOutput | None = field(default=None)
    branch_name: str | None = field(default=None)
    pr_url: str | None = field(default=None)
    reference_files: list[str] = field(default_factory=list)
    review_threads: list[ReviewThread] = field(default_factory=list)
    prior_corrections: list[ReviewThread] = field(default_factory=list)
    pr_number: int | None = field(default=None)

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
                from pydantic_ai_backends.hashline import format_hashline_output
                tagged = format_hashline_output(p.read_text())
                sections.append(f"### {rel}\n\n```\n{tagged}\n```")
            except (ValueError, OSError):
                pass
        if not sections:
            return ""
        return "## Reference files\n\n" + "\n\n".join(sections)
