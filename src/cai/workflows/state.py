from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


def _inline_refs(schema: dict) -> dict:
    """Resolve $ref pointers inline so Google AI function-calling can parse the schema."""
    defs = schema.get("$defs", {})
    if not defs:
        return schema

    def resolve(obj: object) -> object:
        if isinstance(obj, dict):
            if "$ref" in obj:
                ref_name = obj["$ref"].split("/")[-1]
                return resolve(copy.deepcopy(defs.get(ref_name, obj)))
            return {k: resolve(v) for k, v in obj.items() if k != "$defs"}
        if isinstance(obj, list):
            return [resolve(item) for item in obj]
        return obj

    return resolve(copy.deepcopy(schema))  # type: ignore[return-value]


from cai.github.bot import CaiBot
from cai.github.issues import IssueMeta
from cai.github.pr import ReviewThread

_MAX_REFERENCE_FILE_BYTES = 100_000
_MAX_REFERENCE_FILES_TOTAL_BYTES = 200_000


class WithConfidence(BaseModel):
    """Mixin for workflow outputs that expose a self-reported confidence score.

    Workflows use ``confidence`` to gate automatic progression to downstream
    steps (e.g. auto-dispatching audit issues to the solve workflow only at
    >= 9/10). Each agent's instructions specialize the rubric for its own
    domain — keep the field description below as a generic anchor so the
    agent doesn't cluster every output at 7-8.
    """

    confidence: int = Field(
        ge=1,
        le=10,
        description=(
            "Self-reported confidence (1-10) that this output is correct, complete, "
            "and ready for an automated downstream step to act on without human review. "
            "Anchor the score to evidence, not vibe. Generic rubric:\n"
            "  10 — Verified end-to-end against ground truth (test passed, code read, "
            "trace inspected). Stake the next automated step on it.\n"
            "  9  — Strong, multi-source evidence; root cause identified and the next "
            "step's preconditions clearly hold.\n"
            "  7-8 — Plausible and well-reasoned, but unverified — needs a human to "
            "confirm before acting.\n"
            "  5-6 — Tentative hypothesis based on a symptom, with the root cause not "
            "yet confirmed.\n"
            "  1-4 — Speculative; missing context, contradictory signals, or guess "
            "from indirect indicators only.\n"
            "Do not default to 7 or 8. The agent's instructions specialize this rubric "
            "for the specific kind of output."
        ),
    )


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
    files_changed: list[str] = Field(
        default_factory=list,
        description=(
            "Repo-relative paths of every file modified or created during "
            "implementation. Populate this accurately — downstream agents "
            "rely on it instead of re-discovering changes."
        ),
    )

    @classmethod
    def model_json_schema(cls, **kwargs: object) -> dict:
        return _inline_refs(super().model_json_schema(**kwargs))


class ResolveStepOutput(BaseModel):
    summary: str = Field(
        description="One or two sentences describing how the conflicts were reconciled."
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
    files_changed: list[str] = Field(
        default_factory=list,
        description=(
            "Repo-relative paths of every file modified or created during "
            "documentation updates. Populate this accurately — downstream agents "
            "rely on it instead of re-discovering changes."
        ),
    )


class PythonReviewOutput(BaseModel):
    summary: str = Field(
        description="Bulleted list of issues found and fixed per file, or 'No issues found.' if nothing changed."
    )
    commit_message: str = Field(
        description="Git commit message for the review fixes, or empty string if nothing changed."
    )


class GitHubWorkflowReviewOutput(BaseModel):
    summary: str = Field(
        description="Bulleted list of issues found and fixed per GitHub workflow file, or 'No issues found.' if nothing changed."
    )
    commit_message: str = Field(
        description="Git commit message for the workflow review fixes, or empty string if nothing changed."
    )


class PydanticAIReviewOutput(BaseModel):
    summary: str = Field(
        description="Bulleted list of issues found and fixed per file related to pydantic-ai usage, or 'No issues found.' if nothing changed."
    )
    commit_message: str = Field(
        description="Git commit message for the pydantic-ai review fixes, or empty string if nothing changed."
    )


class MergeEvaluationOutput(BaseModel):
    auto_merge: bool = Field(
        description=(
            "True only when the PR clearly meets the auto-merge eligibility rules. "
            "Default False when in doubt — a wrong 'yes' lands an unreviewed bad change, "
            "a wrong 'no' costs a human thirty seconds."
        )
    )
    reason: str = Field(
        description=(
            "One or two sentences naming the specific signal that drove the decision."
        )
    )


SESSION_STATE_FILE = "session_state.json"


class SessionState(BaseModel):
    """Persisted across sequential cai-solve invocations on the same issue."""

    explore_findings: str = ""  # summary from the best explore run so far
    explore_files: list[str] = Field(default_factory=list)
    known_corruptions: list[str] = Field(default_factory=list)
        # Forward-looking plumbing — populating this is out of scope.
        # e.g. "test_refine.py was corrupted in a prior run; verify before editing"
    attempt_count: int = 0
    prior_file_hashes: dict[str, str] = Field(default_factory=dict)
        # Forward-looking plumbing — path -> sha256 of last known-good content.


def load_session_state(workspace_root: Path) -> SessionState:
    path = workspace_root / SESSION_STATE_FILE
    if path.exists():
        return SessionState.model_validate_json(path.read_text())
    return SessionState()


def save_session_state(state: SessionState, workspace_root: Path) -> None:
    (workspace_root / SESSION_STATE_FILE).write_text(
        state.model_dump_json(indent=2)
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
    push_validation_retry_count: int = field(default=0)
    push_validation_failure: str = field(default="")
    python_review_output: PythonReviewOutput | None = field(default=None)
    github_workflow_review_output: GitHubWorkflowReviewOutput | None = field(default=None)
    pydantic_ai_review_output: PydanticAIReviewOutput | None = field(default=None)
    docs_output: DocsOutput | None = field(default=None)
    merge_evaluation: MergeEvaluationOutput | None = field(default=None)
    auto_merge_enabled: bool = field(default=False)
    branch_name: str | None = field(default=None)
    pr_url: str | None = field(default=None)
    reference_files: list[str] = field(default_factory=list)
    review_threads: list[ReviewThread] = field(default_factory=list)
    prior_corrections: list[ReviewThread] = field(default_factory=list)
    pr_number: int | None = field(default=None)
    session_state: SessionState | None = field(default=None)
    flow_kind: Literal["code-change", "analysis"] = field(default="code-change")
    comment_url: str | None = field(default=None)

    def reference_files_section(self) -> str:
        """Render ``reference_files`` as a markdown section ready to splice into a prompt.

        Returns an empty string when no readable files remain after filtering
        (missing paths and oversized files are silently dropped).

        A cumulative byte budget (``_MAX_REFERENCE_FILES_TOTAL_BYTES``)
        prevents prompt bloat; files beyond the budget are skipped and a
        truncation note is appended so the agent knows to ``read_file`` them
        on demand.
        """
        header = "## Reference files\n\n"
        total_bytes = len(header)
        sections: list[str] = []
        omitted = 0
        for i, path_str in enumerate(self.reference_files):
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
                entry = f"### {rel}\n\n```\n{tagged}\n```"
                cost = len(entry) + len("\n\n")
                if total_bytes + cost > _MAX_REFERENCE_FILES_TOTAL_BYTES:
                    omitted = len(self.reference_files) - i
                    break
                sections.append(entry)
                total_bytes += cost
            except (ValueError, OSError):
                pass
        if not sections:
            return ""
        result = header + "\n\n".join(sections)
        if omitted:
            result += f"\n\n_... ({omitted} reference files omitted due to size limit)_"
        return result
