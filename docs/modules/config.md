# config

Shared infrastructure utilities — constants / path definitions,
structured logging, subprocess helpers, per-issue cost aggregation,
and the stale-lock watchdog. These are cross-cutting dependencies
imported by nearly every handler and `cmd_*` function; changes here
ripple everywhere.

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
- [`cai_lib/utils/log.py`](../../cai_lib/utils/log.py) —
  `log_run(category, **fields)` appends a structured row to
  `LOG_PATH`; `log_cost(row)` writes cost events;
  `_get_issue_category(issue)`, `_log_outcome(…)` power the audit helpers.
- [`cai_lib/subprocess_utils.py`](../../cai_lib/subprocess_utils.py)
  — `_run(cmd, **kwargs)` is the thin subprocess wrapper for shell
  operations (gh, git, jq). Agent invocation infrastructure has been
  extracted to the **subagent** module — see [`cai_lib/claude_argv.py`](../../cai_lib/claude_argv.py)
  for the deprecated `_run_claude_p` argv facade and
  [`cai_lib/cai_subagent.py`](../../cai_lib/cai_subagent.py) for the
  new SDK-native `run_subagent` path.
- [`cai_lib/cost_comment.py`](../../cai_lib/cost_comment.py) —
  Cost-row schema helpers and best-effort issue/PR comment posting.
  `_split_cost_by_category()` allocates a `CostRow`'s total cost across
  token categories; `_post_cost_comment()` formats and posts the
  `<!-- cai-cost … -->` attribution comment. Relocated from
  `cai_lib/subagent.cost` to decouple the base subagent module from
  repo-specific dependencies (issue #1269).
- [`cai_lib/cost_summary.py`](../../cai_lib/cost_summary.py) —
  `post_final_cost_summary(issue_number, pr_number)` aggregates
  per-invocation cost records tagged against an issue or its linked PR
  and posts a final roll-up comment on the closed issue; called by
  merge handler to provide per-issue cost transparency.
- [`cai_lib/watchdog.py`](../../cai_lib/watchdog.py) —
  `_rollback_stale_in_progress(immediate)` rolls back orphaned
  issue `:in-progress` / `:revising` labels;
  `_rollback_stale_pr_locks(immediate)` does the same for PR
  locks.

## Inter-module dependencies
- **Mostly a leaf dependency,** except `cost_summary` module has
  dynamic imports from **audit** (to load cost rows) and
  **transcripts** (for multi-host sync). These imports occur inside
  function bodies to avoid circular dependencies.
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
