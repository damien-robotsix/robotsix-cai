"""cai_lib.cmd_unblock — deprecated, use cai_lib.actions.unblock instead.

This module is kept as a compatibility shim. The implementation has moved
to :mod:`cai_lib.actions.unblock` to match the other state-handler modules.
"""
from cai_lib.actions.unblock import (  # noqa: F401
    _list_human_needed_issues,
    _extract_admin_comments,
    _build_unblock_message,
    _clear_pending_marker_on_body,
    _try_unblock_issue,
    handle_human_needed,
    _try_unblock_pr,
    handle_pr_human_needed,
)
