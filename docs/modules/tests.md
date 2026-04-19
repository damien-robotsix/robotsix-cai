# tests

Pytest suite covering the FSM, dispatcher, handlers, helpers, parse,
publish, rescue, and transcript sync. Also enforces lint hygiene.

## Entry points
- `tests/__init__.py` — Test package init.
- `tests/test_agent_staging.py` — Agent staging directory handling.
- `tests/test_dispatcher.py` — FSM dispatcher and state→handler registries.
- `tests/test_dup_check.py` — Duplicate-check pre-triage.
- `tests/test_fsm.py` — States, transitions, Confidence, divert helpers.
- `tests/test_implement_consecutive_failures.py` — Implement-agent retry bookkeeping.
- `tests/test_lint.py` — Ruff hygiene check.
- `tests/test_maintain.py` — `cai-maintain` handler routing.
- `tests/test_merge_diff.py` — Merge-diff helpers.
- `tests/test_multistep.py` — Multi-step plan support.
- `tests/test_orphaned_prs.py` — Orphan PR detection.
- `tests/test_parse.py` — Signal extraction from transcripts.
- `tests/test_plan.py` — Planning pipeline.
- `tests/test_pr_bounce.py` — PR-bounce handler.
- `tests/test_publish.py` — Issue publishing.
- `tests/test_remote_lock.py` — Remote lock handling.
- `tests/test_rescue_opus.py` — Opus-escalation verdict plumbing in `cai_lib.cmd_rescue`.
- `tests/test_retroactive_sweep.py` — Retroactive sweep.
- `tests/test_revise_filter.py` — Revise filtering.
- `tests/test_rollback.py` — Rollback functionality.
- `tests/test_subprocess_utils.py` — Subprocess helpers.
- `tests/test_transcript_sync.py` — Cross-host transcript sync.
- `tests/test_unblock.py` — `cmd_unblock` admin-comment handling.

## Dependencies
- `cai-lib` — all tests import from `cai_lib/`.
