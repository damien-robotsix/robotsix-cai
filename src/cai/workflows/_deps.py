"""Shared filesystem deps for the cai-solve agents.

Explore (read-only) and refine (read-everywhere, write-into-issue-dir)
sandbox against the same repo-wide exclude list — they only differ in
which directories writes are allowed in.
"""
from __future__ import annotations

from pathlib import Path

from pydantic_ai_backends.permissions.types import (
    OperationPermissions,
    PermissionRule,
    PermissionRuleset,
)
from pydantic_deep import DeepAgentDeps, LocalBackend

# Vendored caches, build artefacts, and VCS internals — torching token
# budget on these does the agent no good.
_EXCLUDES: list[PermissionRule] = [
    PermissionRule(pattern="**/__pycache__/**", action="deny"),
    PermissionRule(pattern="**/pycache/**", action="deny"),
    PermissionRule(pattern="**/__pycache__", action="deny"),
    PermissionRule(pattern="**/*.pyc", action="deny"),
    PermissionRule(pattern="**/dist/**", action="deny"),
    PermissionRule(pattern="**/*.egg-info/**", action="deny"),
    PermissionRule(pattern="**/.git/**", action="deny"),
    PermissionRule(pattern="**/node_modules/**", action="deny"),
]


def _glob_dir_prefix(pattern: str) -> Path:
    """Return the longest leading path prefix of ``pattern`` with no wildcards.

    Used to register the parent directory of a write glob in
    ``allowed_directories`` so the operation reaches the permission gate.
    """
    parts: list[str] = []
    for part in Path(pattern).parts:
        if any(c in part for c in "*?["):
            break
        parts.append(part)
    return Path(*parts) if parts else Path("/")


def repo_deps(
    repo_root: Path,
    *,
    write_dirs: list[Path] | None = None,
    write_globs: list[str] | None = None,
) -> DeepAgentDeps:
    """Build agent deps with read across ``repo_root`` and tightly-scoped writes.

    Reads/globs/greps/ls allow the whole repo minus ``_EXCLUDES``.
    Writes/edits are denied by default and allowed via two channels:

    * ``write_dirs`` — every path under each given directory (pattern
      ``<d>/**``). Use for "this whole tree is fair game" cases like
      the implement agent rewriting repo source.
    * ``write_globs`` — explicit glob patterns. Use for "only these
      files" cases like refine, where the agent should only touch the
      issue body and sub-issue siblings, not the cloned repo or the
      spike scratch dir that happen to live under the same parent.

    When both are empty the deps are fully read-only. Execute is always
    denied — code execution goes through the ``spike_run`` tool, which
    subprocesses directly without involving the backend's permission gate.
    """
    repo_root = repo_root.resolve()
    write_dirs = [d.resolve() for d in (write_dirs or [])]
    write_globs = list(write_globs or [])
    write_allow = [
        PermissionRule(pattern=f"{d}/**", action="allow") for d in write_dirs
    ] + [
        PermissionRule(pattern=g, action="allow") for g in write_globs
    ]
    permissions = PermissionRuleset(
        default="allow",
        read=OperationPermissions(default="allow", rules=_EXCLUDES),
        glob=OperationPermissions(default="allow", rules=_EXCLUDES),
        grep=OperationPermissions(default="allow", rules=_EXCLUDES),
        ls=OperationPermissions(default="allow", rules=_EXCLUDES),
        write=OperationPermissions(default="deny", rules=write_allow),
        edit=OperationPermissions(default="deny", rules=write_allow),
        execute=OperationPermissions(default="deny"),
    )
    glob_dirs = {str(_glob_dir_prefix(g).resolve()) for g in write_globs}
    return DeepAgentDeps(
        backend=LocalBackend(
            root_dir=str(repo_root),
            allowed_directories=[
                str(repo_root),
                *(str(d) for d in write_dirs),
                *glob_dirs,
            ],
            permissions=permissions,
        )
    )
