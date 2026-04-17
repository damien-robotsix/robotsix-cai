"""Backward-compatibility re-exports from cmd_helpers submodules."""

from cai_lib.cmd_helpers_git import (
    AGENT_EDIT_STAGING_REL,
    PLUGIN_STAGING_REL,
    CLAUDEMD_STAGING_REL,
    _git,
    _work_directory_block,
    _setup_agent_edit_staging,
    _apply_agent_edit_staging,
)
from cai_lib.cmd_helpers_github import (
    _BOT_COMMENT_MARKERS,
    _NO_ADDITIONAL_CHANGES_MARKER,
    _REBASE_FAILED_MARKER,
    _gh_user_identity,
    _is_bot_comment,
    _parse_iso_ts,
    _fetch_review_comments,
    _pr_set_needs_human,
)
from cai_lib.cmd_helpers_issues import (
    _parse_oob_issues,
    _create_oob_issues,
    _fetch_previous_fix_attempts,
    _build_attempt_history_block,
    _extract_stored_plan,
    _strip_stored_plan_block,
)

__all__ = [
    # git / worktree helpers
    "AGENT_EDIT_STAGING_REL",
    "PLUGIN_STAGING_REL",
    "CLAUDEMD_STAGING_REL",
    "_git",
    "_work_directory_block",
    "_setup_agent_edit_staging",
    "_apply_agent_edit_staging",
    # GitHub API helpers
    "_BOT_COMMENT_MARKERS",
    "_NO_ADDITIONAL_CHANGES_MARKER",
    "_REBASE_FAILED_MARKER",
    "_gh_user_identity",
    "_is_bot_comment",
    "_parse_iso_ts",
    "_fetch_review_comments",
    "_pr_set_needs_human",
    # issue-lifecycle helpers
    "_parse_oob_issues",
    "_create_oob_issues",
    "_fetch_previous_fix_attempts",
    "_build_attempt_history_block",
    "_extract_stored_plan",
    "_strip_stored_plan_block",
]
