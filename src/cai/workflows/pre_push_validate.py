from __future__ import annotations

from fnmatch import fnmatch
from pathlib import Path

from git import Repo
from pydantic_graph import BaseNode, GraphRunContext

from cai.git import stage_all
from cai.workflows.state import IssueState


def _parse_files_to_change(body: str) -> list[str] | None:
    """Parse the "Files to change" section from the issue body.

    Returns a list of file paths, or None if no such section exists.
    """
    lines = body.splitlines()
    in_section = False
    in_fence = False
    files: list[str] = []

    for line in lines:
        stripped = line.strip()

        if stripped.startswith("#"):
            # Extract heading text (strip leading # and whitespace)
            heading = stripped.lstrip("#").strip().lower()
            if heading in ("files to change", "files"):
                in_section = True
                in_fence = False
                continue
            elif in_section:
                # Next heading ends the section
                break

        if in_section and stripped:
            if stripped.startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            if stripped.startswith("- ") or stripped.startswith("* "):
                path = stripped[2:].strip()
                # Remove parenthetical comments like "(new)" or "(wire node into graph)"
                if "(" in path:
                    path = path.split("(")[0].strip()
                path = path.strip("`")
                if path:
                    files.append(path)
            else:
                # Possibly comma-separated on a single line
                for part in stripped.split(","):
                    part = part.strip().strip("`")
                    if "(" in part:
                        part = part.split("(")[0].strip()
                    if part:
                        files.append(part)

    return files if files else None


ALLOW_LIST_PATTERNS = [
    ".github/workflows/cai-*.yml",
    "docs/workflows/*.md",
]


def _is_allow_listed(path: str) -> bool:
    """Check if a path matches any allow-list pattern."""
    for pattern in ALLOW_LIST_PATTERNS:
        if fnmatch(path, pattern):
            return True
    return False


class PrePushValidationNode(BaseNode[IssueState]):
    async def run(self, ctx: GraphRunContext[IssueState]) -> PRNode | ImplementNode:  # noqa: F821
        from cai.workflows.implement import ImplementNode
        from cai.workflows.pr import PRNode

        state = ctx.state

        # Stage all changes
        stage_all(state.repo_root)

        # Skip gates for human-review issues
        if "cai:human-review" in state.meta.labels:
            return PRNode()

        repo = Repo(str(state.repo_root))

        # --- Empty-file gate ---
        try:
            new_files = repo.git.diff(
                "--cached", "--name-only", "--diff-filter=A", "main..."
            ).splitlines()
        except Exception:
            new_files = []

        empty_files = []
        for f in new_files:
            f = f.strip()
            if not f:
                continue
            file_path = state.repo_root / f
            try:
                if file_path.exists() and file_path.read_bytes() == b"":
                    empty_files.append(f)
            except OSError:
                pass

        if empty_files:
            failure_msg = (
                "Pre-push validation failed: empty scratch file(s) detected. "
                "Delete the following file(s) before retrying: "
                + ", ".join(empty_files)
            )
            state.push_validation_failure = failure_msg
            if state.push_validation_retry_count < 2:
                state.push_validation_retry_count += 1
                return ImplementNode()
            else:
                raise RuntimeError(failure_msg)

        # --- Out-of-scope gate ---
        body = state.body_path.read_text()
        declared_files = _parse_files_to_change(body)

        if declared_files is not None:
            try:
                staged = repo.git.diff(
                    "--cached", "--name-only", "main..."
                ).splitlines()
            except Exception:
                staged = []

            out_of_scope = []
            for f in staged:
                f = f.strip()
                if not f:
                    continue
                if f in declared_files:
                    continue
                if _is_allow_listed(f):
                    continue
                out_of_scope.append(f)

            if out_of_scope:
                failure_msg = (
                    "Pre-push validation failed: file(s) edited outside the issue scope. "
                    'Either add the following file(s) to the "Files to change" section '
                    "in the issue body, or remove the edits: "
                    + ", ".join(out_of_scope)
                )
                state.push_validation_failure = failure_msg
                if state.push_validation_retry_count < 2:
                    state.push_validation_retry_count += 1
                    return ImplementNode()
                else:
                    raise RuntimeError(failure_msg)

        return PRNode()
