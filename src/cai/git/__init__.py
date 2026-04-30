"""Generic git plumbing built on GitPython.

GitHub-specific concerns (token URLs, the cai-solve workspace layout)
live in :mod:`cai.github`; this package only knows about git itself.
"""
from .config import add_local, set_local, unset_all_local
from .ops import (
    checkout_branch,
    clone,
    commit,
    conflicted_paths,
    current_rebase_step,
    fetch,
    index_matches_head,
    merge_no_commit,
    push_branch,
    rebase_abort,
    rebase_continue,
    rebase_in_progress,
    rebase_onto,
    rebase_skip,
    rev_parse,
    stage_all,
)

__all__ = [
    "add_local",
    "checkout_branch",
    "clone",
    "commit",
    "conflicted_paths",
    "current_rebase_step",
    "fetch",
    "index_matches_head",
    "merge_no_commit",
    "push_branch",
    "rebase_abort",
    "rebase_continue",
    "rebase_in_progress",
    "rebase_onto",
    "rebase_skip",
    "rev_parse",
    "set_local",
    "stage_all",
    "unset_all_local",
]
