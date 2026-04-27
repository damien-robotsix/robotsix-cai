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


def repo_deps(
    repo_root: Path, *, write_dirs: list[Path] | None = None
) -> DeepAgentDeps:
    """Build agent deps with read across ``repo_root`` and writes gated to ``write_dirs``.

    Reads/globs/greps/ls allow the whole repo minus ``_EXCLUDES``.
    Writes/edits are denied by default and allowed only inside the
    directories listed in ``write_dirs`` (empty == fully read-only).
    Execute is always denied.
    """
    repo_root = repo_root.resolve()
    write_dirs = [d.resolve() for d in (write_dirs or [])]
    write_allow = [
        PermissionRule(pattern=f"{d}/**", action="allow") for d in write_dirs
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
    return DeepAgentDeps(
        backend=LocalBackend(
            root_dir=str(repo_root),
            allowed_directories=[str(repo_root), *(str(d) for d in write_dirs)],
            permissions=permissions,
        )
    )
