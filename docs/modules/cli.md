# cli

Top-level CLI dispatcher and subcommand implementations. `cai.py` is
the main entry point that parses arguments and routes to the
per-command handlers in `cai_lib/cmd_*.py`. Root-level `parse.py`
and `publish.py` are thin shims re-exporting their `cai_lib/`
counterparts, preserved so existing shell scripts, cron entries,
and docs that call `python parse.py` / `python publish.py` keep
working. `cai_lib/__init__.py` is an empty package marker;
`cai_lib/dispatcher.py` wires state→handler routing for the
lifecycle FSM.

## Key entry points
- [`cai.py`](../../cai.py) — `main()` parses `argparse` subcommands
  and delegates to the `cmd_*` functions below.
- [`cai_lib/dispatcher.py`](../../cai_lib/dispatcher.py) —
  `dispatch_issue`, `dispatch_pr`, `dispatch_drain`,
  `dispatch_oldest_actionable`; the state→handler registries are
  built by `_build_issue_registry` and `_build_pr_registry`.
- [`cai_lib/cmd_cycle.py`](../../cai_lib/cmd_cycle.py) — `cmd_cycle`
  (full audit → publish → dispatch loop) and `cmd_dispatch`
  (single-target step).
- [`cai_lib/cmd_agents.py`](../../cai_lib/cmd_agents.py) —
  `cmd_audit_module`; on-demand per-module audit dispatcher.
- [`cai_lib/cmd_misc.py`](../../cai_lib/cmd_misc.py) — `cmd_init`,
  `cmd_verify`, `cmd_cost_report`, `cmd_health_report`,
  `cmd_check_workflows`, `cmd_test`.
- [`cai_lib/cmd_implement.py`](../../cai_lib/cmd_implement.py) —
  implement-pipeline helpers (e.g. `_parse_decomposition`).
- [`cai_lib/cmd_rescue.py`](../../cai_lib/cmd_rescue.py) —
  `cmd_rescue`; autonomous rescue of `:human-needed` targets.
- [`cai_lib/cmd_unblock.py`](../../cai_lib/cmd_unblock.py) —
  `cmd_unblock`; admin-comment-driven FSM resume.
- [`cai_lib/cmd_helpers.py`](../../cai_lib/cmd_helpers.py) plus
  `cmd_helpers_git.py`, `cmd_helpers_github.py`,
  `cmd_helpers_issues.py` — shared helpers used by the `cmd_*`
  functions and the action handlers.
- [`parse.py`](../../parse.py), [`publish.py`](../../publish.py) —
  root-level shims re-exporting `cai_lib/parse.py` and
  `cai_lib/publish.py` for backwards compatibility.
- [`cai_lib/__init__.py`](../../cai_lib/__init__.py) — empty
  package init.

## Inter-module dependencies
- Imports from **config** — constants (`REPO`, label names, log
  paths) used by nearly every `cmd_*` function.
- Imports from **fsm** — `IssueState`/`PRState` enums and
  transition helpers consumed by the dispatcher.
- Imports from **actions** — handler callables registered in the
  dispatcher's state→handler tables.
- Imports from **github-glue** — `_gh_json`, `_set_labels`,
  `_post_issue_comment`, remote-lock helpers.
- Imports from **transcripts** — `cmd_analyze` drives the parse
  pipeline; `transcript_sync.cmd_transcript_sync` is a subcommand.
- Imports from **audit** — `cmd_cost_report` and related audit
  commands call `cai_lib.audit.cost` helpers.
- Imported by **tests** — `tests/test_dispatcher.py`,
  `tests/test_rescue_opus.py`, `tests/test_unblock.py`, and the
  multistep/plan/publish suites exercise these entry points.
- Imported by **workflows** and **installer** via `entrypoint.sh`
  and cron, which invoke `python cai.py <subcommand>`.

## Operational notes
- **Cost sensitivity — HIGH for audit subcommands.** `cmd_propose`,
  `cmd_cost_optimize`, `cmd_external_scout`, `cmd_update_check`,
  and the agents they call (cai-propose, cai-external-scout,
  etc.) are among the largest single-invocation token spenders.
- **FSM invariant.** `cmd_dispatch` is the only production path
  that advances FSM state outside a handler; anything else that
  flips labels bypasses the watchdog rollback and remote lock.
  Preserve this chokepoint.
- **CI implications.** Every Python CLI subcommand is exercised
  by the pytest suite via `tests/test_dispatcher.py` and
  `tests/test_multistep.py`; `tests/test_lint.py` enforces ruff
  hygiene. A new `cmd_*` function not wired into `cai.py`'s
  argparse tree is dead code.
- `cai.py` itself is ~63 k tokens — do not `Read` it whole; use
  `Grep` and offset-based `Read` to navigate.
