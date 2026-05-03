from __future__ import annotations

import re
from pathlib import Path

from git import Repo
from pydantic_graph import BaseNode, GraphRunContext

from cai.git import stage_all
from cai.workflows.state import IssueState

_ALLOW_LIST = [
    re.compile(r"\.github/workflows/cai-.*\.yml$"),
    re.compile(r"docs/workflows/.*\.md$"),
]


class PrePushValidationNode(BaseNode[IssueState]):
    async def run(self, ctx: GraphRunContext[IssueState]) -> "PRNode | ImplementNode":
        from cai.workflows.implement import ImplementNode
        from cai.workflows.pr import PRNode

        state = ctx.state

        # Skip gates for human-review issues
        labels = getattr(state.meta, "labels", None) or []
        if "cai:human-review" in labels:
            return PRNode()

        stage_all(state.repo_root)
        repo = Repo(str(state.repo_root))

        failures: list[str] = []

        # --- Empty-file gate ---
        new_files = repo.git.diff(
            "--cached", "--name-only", "--diff-filter=A", "main..."
        ).splitlines()
        empty_new_files = [
            f for f in new_files
            if f.strip() and (state.repo_root / f.strip()).read_bytes() == b""
        ]
        if empty_new_files:
            failures.append(
                "Pre-push validation failed: empty scratch file(s) detected. "
                "Delete the following file(s) before retrying: "
                + ", ".join(empty_new_files)
            )

        # --- Out-of-scope gate ---
        scope_files = _parse_files_to_change(state.body_path.read_text())

        if scope_files is not None:
            staged_files = [
                f.strip() for f in
                repo.git.diff("--cached", "--name-only", "main...").splitlines()
                if f.strip()
            ]
            out_of_scope = []
            for f in staged_files:
                if f in scope_files:
                    continue
                if any(p.match(f) for p in _ALLOW_LIST):
                    continue
                out_of_scope.append(f)

            if out_of_scope:
                failures.append(
                    "Pre-push validation failed: file(s) edited outside the issue scope. "
                    "Either add the following file(s) to the \"Files to change\" section "
                    "in the issue body, or remove the edits: "
                    + ", ".join(out_of_scope)
                )

        if not failures:
            state.push_validation_failure = ""
            state.push_validation_retry_count = 0
            return PRNode()

        failure_message = "\n\n".join(failures)

        if state.push_validation_retry_count < 2:
            state.push_validation_failure = failure_message
            state.push_validation_retry_count += 1
            return ImplementNode()

        # Retries exhausted
        raise RuntimeError(failure_message)


def _parse_files_to_change(body_text: str) -> set[str] | None:
    """Parse the 'Files to change' section from the issue body.

    Returns a set of file paths, or None if no such section exists.
    """
    pattern = r"(?i)^#+\s*(?:files to change|files)\s*$"
    lines = body_text.split("\n")

    section_start = None
    for i, line in enumerate(lines):
        if re.match(pattern, line.strip()):
            section_start = i
            break

    if section_start is None:
        return None

    file_paths: set[str] = set()
    for i in range(section_start + 1, len(lines)):
        line = lines[i].strip()
        # Stop at next heading
        if re.match(r"^#", line):
            break
        if not line:
            continue
        # Extract from bullet lists
        bullet_match = re.match(r"^[-*]\s+(.+)$", line)
        if bullet_match:
            text = bullet_match.group(1)
            code_fence_matches = re.findall(r"`([^`]+)`", text)
            if code_fence_matches:
                file_paths.update(code_fence_matches)
            else:
                for part in re.split(r",\s*", text):
                    part = part.strip()
                    if part:
                        file_paths.add(part)
            continue
        # Plain text line
        code_fence_matches = re.findall(r"`([^`]+)`", line)
        if code_fence_matches:
            file_paths.update(code_fence_matches)
        else:
            for part in re.split(r",\s*", line):
                part = part.strip()
                if part:
                    file_paths.add(part)

    return file_paths
