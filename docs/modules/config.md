# config

Shared infrastructure utilities — constants / path definitions,
structured logging, subprocess helpers, and the stale-lock
watchdog. These are cross-cutting dependencies imported by nearly
every handler and `cmd_*` function; changes here ripple everywhere.

## Key entry points
- [`cai_lib/config.py`](../../cai_lib/config.py) — repo-wide
  constants: `REPO`, label names (`LABEL_HUMAN_SOLVED`,
  `LABEL_PARENT`, `LABEL_*`), log paths (`LOG_PATH`,
  `COST_LOG_PATH`, `OUTCOME_LOG_PATH`, `AUDIT_LOG_DIR`), worktree/base
  directories. Helpers `_repo_slug(repo)`, `_resolve_machine_id()`,
  `_resolve_instance_id()`, `transcript_sync_enabled()`,
  `is_admin_login(login)`, `audit_log_path(kind, module)`.
  `AUDIT_LOG_DIR` is `/var/log/cai/audit` — see `docs/modules/audit.md`
  for the per-workflow structured log format.
- [`cai_lib/logging_utils.py`](../../cai_lib/logging_utils.py) —
  `log_run(category, **fields)` appends a structured row to
  `LOG_PATH`; `log_cost(row)` writes cost events;
  `_get_issue_category(issue)`, `_log_outcome(…)`,
  `_load_outcome_stats(days)` power the audit helpers.
- [`cai_lib/subprocess_utils.py`](../../cai_lib/subprocess_utils.py)
  — `_run(cmd, **kwargs)` is the subprocess wrapper with timeout
  and logging; `_run_claude_p(…)` launches a headless claude-code
  session via the Claude Agent SDK and returns a CompletedProcess
  with `.stdout` containing the result; `_argv_to_options(argv, cwd)`
  parses command-line arguments into SDK options.
- [`cai_lib/watchdog.py`](../../cai_lib/watchdog.py) —
  `_rollback_stale_in_progress(immediate)` rolls back orphaned
  issue `:in-progress` / `:revising` labels;
  `_rollback_stale_pr_locks(immediate)` does the same for PR
  locks.

## Inter-module dependencies
- No imports from other pipeline modules (leaf dependency).
- Imported by **fsm**, **actions**, **cli**, **github-glue**,
  **audit**, **transcripts** — essentially every Python file in
  the pipeline.
- Imported by **tests** — `tests/test_subprocess_utils.py`,
  `tests/test_rollback.py`, and many handler tests pin the
  constants and helpers.

## Operational notes
- **Constant changes are breaking.** Renaming a label or log path
  requires a sweep across every module and test. Prefer adding a
  new constant over redefining an existing one.
- **`_run_claude_p` is the cost spout.** Every Claude invocation
  goes through this helper; changes to its defaults (timeout,
  model, prompt prefix) move cost across every handler.
  Instrument before refactoring.
- **Watchdog invariant.** `_rollback_stale_in_progress` must
  remain safe to re-run; it relies on lock comments emitted by
  `github.py._acquire_remote_lock` to detect abandonment.
- **`:locked` age comes from the claim comment, not `updatedAt`.**
  The watchdog reads the oldest `<!-- cai-lock -->` comment's
  `created_at` to decide whether a `:locked` label is past its TTL.
  Using the issue/PR's `updatedAt` was a prior bug: GitHub bumps
  `updatedAt` for CI check-runs and each cycle's losing
  `_acquire_remote_lock` race (post+delete of a claim comment),
  which kept hours-old locks looking "fresh" forever.
- **CI implications.** Tests import from these files directly;
  breaking a public helper breaks ~30 tests.
- **Cost sensitivity — indirect but central.** Logging is free;
  `_run_claude_p` is the dominant cost centre for the whole
  project.
