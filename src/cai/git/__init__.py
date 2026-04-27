"""Generic git plumbing built on GitPython.

GitHub-specific concerns (token URLs, the cai-solve workspace layout)
live in :mod:`cai.github`; this package only knows about git itself.
"""
from .config import add_local, set_local, unset_all_local
from .ops import checkout_branch, clone, commit, push_branch, stage_all

__all__ = [
    "add_local",
    "checkout_branch",
    "clone",
    "commit",
    "push_branch",
    "set_local",
    "stage_all",
    "unset_all_local",
]
