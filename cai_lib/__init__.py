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
    CODE_AUDIT_MEMORY,
    PROPOSE_MEMORY,
    UPDATE_CHECK_MEMORY,
    COST_OPTIMIZE_MEMORY,
    AGENT_MEMORY_DIR,
    LOG_PATH,
    COST_LOG_PATH,
    REVIEW_PR_PATTERN_LOG,
    OUTCOME_LOG_PATH,
    ACTIVE_JOB_PATH,
    LABEL_RAISED,
    LABEL_IN_PROGRESS,
    LABEL_PR_OPEN,
    LABEL_MERGED,
    LABEL_SOLVED,
    LABEL_NO_ACTION,
    LABEL_NEEDS_SPIKE,
    LABEL_NEEDS_EXPLORATION,
    LABEL_REFINED,
    LABEL_REVISING,
    LABEL_PARENT,
    LABEL_MERGE_BLOCKED,
    LABEL_AUDIT_RAISED,
    LABEL_AUDIT_NEEDS_HUMAN,
    LABEL_PR_NEEDS_HUMAN,
    LABEL_HUMAN_SUBMITTED,
    LABEL_PLANNED,
    LABEL_PLAN_APPROVED,
    _STALE_IN_PROGRESS_HOURS,
    _STALE_REVISING_HOURS,
    _STALE_NO_ACTION_DAYS,
    _STALE_MERGED_DAYS,
)

from cai_lib.logging_utils import (
    log_run,
    log_cost,
    _write_active_job,
    _clear_active_job,
    _get_issue_category,
    _log_outcome,
    _load_outcome_counts,
    _load_outcome_stats,
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

from cai_lib.cmd_lifecycle import _rollback_stale_in_progress, _reconcile_interrupted

from cai_lib.cmd_implement import _parse_decomposition

__all__ = [
    # config
    "REPO", "SMOKE_PROMPT", "TRANSCRIPT_DIR", "PARSE_SCRIPT", "PUBLISH_SCRIPT",
    "CODE_AUDIT_MEMORY", "PROPOSE_MEMORY", "UPDATE_CHECK_MEMORY",
    "COST_OPTIMIZE_MEMORY", "AGENT_MEMORY_DIR",
    "LOG_PATH", "COST_LOG_PATH", "REVIEW_PR_PATTERN_LOG",
    "OUTCOME_LOG_PATH", "ACTIVE_JOB_PATH",
    "LABEL_RAISED", "LABEL_IN_PROGRESS", "LABEL_PR_OPEN",
    "LABEL_MERGED", "LABEL_SOLVED", "LABEL_NO_ACTION", "LABEL_NEEDS_SPIKE",
    "LABEL_NEEDS_EXPLORATION", "LABEL_REFINED", "LABEL_REVISING", "LABEL_PARENT",
    "LABEL_MERGE_BLOCKED", "LABEL_AUDIT_RAISED", "LABEL_AUDIT_NEEDS_HUMAN",
    "LABEL_PR_NEEDS_HUMAN", "LABEL_HUMAN_SUBMITTED", "LABEL_PLANNED", "LABEL_PLAN_APPROVED",
    "_STALE_IN_PROGRESS_HOURS", "_STALE_REVISING_HOURS",
    "_STALE_NO_ACTION_DAYS", "_STALE_MERGED_DAYS",
    # logging
    "log_run", "log_cost", "_write_active_job", "_clear_active_job",
    "_get_issue_category", "_log_outcome", "_load_outcome_counts",
    "_load_outcome_stats", "_load_cost_log", "_row_ts", "_build_cost_summary",
    # subprocess
    "_run", "_run_claude_p",
    # github
    "_gh_json", "check_gh_auth", "check_claude_auth", "_transcript_dir_is_empty",
    "_set_labels", "_issue_has_label", "_build_issue_block", "_build_implement_user_message",
    # cmd_lifecycle
    "_rollback_stale_in_progress",
    "_reconcile_interrupted",
    # cmd_implement
    "_parse_decomposition",
]
