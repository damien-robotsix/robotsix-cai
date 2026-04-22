# tests

Pytest suite covering the FSM, dispatcher, handlers, helpers,
parse, publish, rescue, transcript sync, and lint hygiene. Every
functional module has at least one dedicated test module here;
the suite is the primary safety net for refactors.

## Key entry points
- [`tests/test_fsm.py`](../../tests/test_fsm.py) — states,
  transitions, Confidence, divert/marker/resume helpers.
- [`tests/test_dispatcher.py`](../../tests/test_dispatcher.py) —
  state→handler registries and routing.
- [`tests/test_plan.py`](../../tests/test_plan.py),
  [`tests/test_multistep.py`](../../tests/test_multistep.py),
  [`tests/test_implement_consecutive_failures.py`](../../tests/test_implement_consecutive_failures.py),
  [`tests/test_implement_helper_extract.py`](../../tests/test_implement_helper_extract.py),
  [`tests/test_implement_scope.py`](../../tests/test_implement_scope.py),
  [`tests/test_revise_filter.py`](../../tests/test_revise_filter.py),
  [`tests/test_merge_diff.py`](../../tests/test_merge_diff.py),
  [`tests/test_maintain.py`](../../tests/test_maintain.py) —
  handler-specific coverage.
- [`tests/test_parse.py`](../../tests/test_parse.py),
  [`tests/test_publish.py`](../../tests/test_publish.py),
  [`tests/test_dup_check.py`](../../tests/test_dup_check.py),
  [`tests/test_transcript_sync.py`](../../tests/test_transcript_sync.py)
  — transcript + github-glue coverage.
- [`tests/test_orphaned_prs.py`](../../tests/test_orphaned_prs.py),
  [`tests/test_remote_lock.py`](../../tests/test_remote_lock.py),
  [`tests/test_retroactive_sweep.py`](../../tests/test_retroactive_sweep.py),
  [`tests/test_rollback.py`](../../tests/test_rollback.py),
  [`tests/test_blocked_on.py`](../../tests/test_blocked_on.py) —
  watchdog / sweep / lock coverage.
- [`tests/test_rescue_opus.py`](../../tests/test_rescue_opus.py),
  [`tests/test_unblock.py`](../../tests/test_unblock.py) —
  rescue/unblock plumbing.
- [`tests/test_subprocess_utils.py`](../../tests/test_subprocess_utils.py),
  [`tests/test_agent_staging.py`](../../tests/test_agent_staging.py),
  [`tests/test_audit_modules.py`](../../tests/test_audit_modules.py)
  — shared infra + audit.
- [`tests/test_lint.py`](../../tests/test_lint.py) — asserts ruff
  reports zero violations.
- [`pyproject.toml`](../../pyproject.toml) — Python project
  configuration (ruff lint settings).

## Inter-module dependencies
- Imports from **fsm**, **actions**, **cli**, **config**,
  **audit**, **transcripts**, **github-glue** — the suite covers
  every functional module.
- Run by **cli** — `cmd_test` (`cai test`) is the wrapper that
  installs deps and runs pytest + ruff.
- Run by **installer** — `entrypoint.sh` runs `cai test` on fresh
  container starts in some deployment modes.
- No reverse imports — nothing in the pipeline imports `tests/`.

## Operational notes
- **Lint gate.** `test_lint.py` runs ruff against the whole repo;
  a style violation fails the full suite. Fix locally before
  pushing.
- **No integration tests.** The suite exclusively stubs
  subprocess calls and network I/O; nothing talks to real
  GitHub, Docker Hub, or Claude APIs. Behaviour against real
  services is validated only via live pipeline runs.
- **CI implications.** `cai test` is the canonical green-light
  before merge; failing tests are the most common reason for a
  `:human-needed` divert.
- **Cost sensitivity.** Zero (pure local Python execution).
