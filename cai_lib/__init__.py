"""cai_lib — extracted internals of the cai automation wrapper.

This package provides the foundation modules extracted from ``cai.py`` as
part of the ongoing structural split (issue #486).  It is the authoritative
source for the symbols listed below; ``cai.py`` still contains the remaining
``cmd_*`` entrypoints but imports these symbols from here.

Re-exported at the package level so that:

  import cai_lib as cai          # test_rollback.py compatibility
  from cai_lib import X          # test_multistep.py and future callers
"""

from cai_lib.config import (
    REPO,
    SMOKE_PROMPT,
    TRANSCRIPT_DIR,
    PARSE_SCRIPT,
    PUBLISH_SCRIPT,
    AGENT_MEMORY_DIR,
    LOG_PATH,
    COST_LOG_PATH,
    REVIEW_PR_PATTERN_LOG,
    OUTCOME_LOG_PATH,
    LABEL_RAISED,
    LABEL_IN_PROGRESS,
    LABEL_PR_OPEN,
    LABEL_MERGED,
    LABEL_SOLVED,
    LABEL_NEEDS_EXPLORATION,
    LABEL_REFINED,
    LABEL_REVISING,
    LABEL_PARENT,
    LABEL_MERGE_BLOCKED,
    LABEL_PR_NEEDS_HUMAN,
    LABEL_PLANNED,
    LABEL_PLAN_APPROVED,
    LABEL_REFINING,
    LABEL_PLANNING,
    LABEL_APPLYING,
    LABEL_APPLIED,
    LABEL_HUMAN_NEEDED,
    LABEL_PR_HUMAN_NEEDED,
    LABEL_LOCKED,
    INSTANCE_ID,
    CAI_LOCK_COMMENT_RE,
    CAI_COST_COMMENT_RE,
    _STALE_IN_PROGRESS_HOURS,
    _STALE_REVISING_HOURS,
    _STALE_APPLYING_HOURS,
    _STALE_LOCKED_HOURS,
    _STALE_MERGED_DAYS,
)

from cai_lib.logging_utils import (
    log_run,
    log_cost,
    _get_issue_category,
    _log_outcome,
    _load_outcome_stats,
)
from cai_lib.audit.cost import (
    _load_outcome_counts,
    _load_cost_log,
    _row_ts,
    _build_cost_summary,
)

from cai_lib.subprocess_utils import _run, _run_claude_p

from cai_lib.github import (
    _gh_json,
    check_gh_auth,
    check_claude_auth,
    _transcript_dir_is_empty,
    _set_labels,
    _issue_has_label,
    _build_issue_block,
    _build_implement_user_message,
)

from cai_lib.watchdog import (
    _rollback_stale_in_progress,
    _rollback_stale_pr_locks,
)

from cai_lib.cmd_implement import _parse_decomposition

from cai_lib.fsm import (
    IssueState, PRState, Transition,
    ISSUE_TRANSITIONS, PR_TRANSITIONS,
    get_issue_state, get_pr_state, render_fsm_mermaid,
)

__all__ = [
    # config
    "REPO", "SMOKE_PROMPT", "TRANSCRIPT_DIR", "PARSE_SCRIPT", "PUBLISH_SCRIPT",
    "AGENT_MEMORY_DIR",
    "LOG_PATH", "COST_LOG_PATH", "REVIEW_PR_PATTERN_LOG",
    "OUTCOME_LOG_PATH",
    "LABEL_RAISED", "LABEL_IN_PROGRESS", "LABEL_PR_OPEN",
    "LABEL_MERGED", "LABEL_SOLVED",
    "LABEL_NEEDS_EXPLORATION", "LABEL_REFINED", "LABEL_REVISING", "LABEL_PARENT",
    "LABEL_MERGE_BLOCKED",
    "LABEL_PR_NEEDS_HUMAN", "LABEL_PLANNED", "LABEL_PLAN_APPROVED",
    "LABEL_REFINING", "LABEL_PLANNING",
    "LABEL_APPLYING", "LABEL_APPLIED",
    "LABEL_HUMAN_NEEDED", "LABEL_PR_HUMAN_NEEDED",
    "LABEL_LOCKED", "INSTANCE_ID", "CAI_LOCK_COMMENT_RE", "CAI_COST_COMMENT_RE",
    "_STALE_IN_PROGRESS_HOURS", "_STALE_REVISING_HOURS", "_STALE_APPLYING_HOURS",
    "_STALE_LOCKED_HOURS", "_STALE_MERGED_DAYS",
    # logging
    "log_run", "log_cost",
    "_get_issue_category", "_log_outcome", "_load_outcome_counts",
    "_load_outcome_stats", "_load_cost_log", "_row_ts", "_build_cost_summary",
    # subprocess
    "_run", "_run_claude_p",
    # github
    "_gh_json", "check_gh_auth", "check_claude_auth", "_transcript_dir_is_empty",
    "_set_labels", "_issue_has_label", "_build_issue_block", "_build_implement_user_message",
    # watchdog
    "_rollback_stale_in_progress",
    "_rollback_stale_pr_locks",
    # cmd_implement
    "_parse_decomposition",
    # fsm
    "IssueState", "PRState", "Transition",
    "ISSUE_TRANSITIONS", "PR_TRANSITIONS",
    "get_issue_state", "get_pr_state", "render_fsm_mermaid",
]
