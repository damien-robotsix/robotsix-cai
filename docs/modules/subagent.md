# subagent

Agent invocation infrastructure extracted from `cai_lib/subprocess_utils.py`. Owns both the typed-options Claude Agent SDK execution path and the deprecated `claude -p` argv facade, plus shared cross-cutting concerns: cost-row attribution, stderr capture, FSM state stamping for telemetry, and SDK error diagnostics.

## Entry points

- [`cai_lib/subagent/__init__.py`](../../cai_lib/subagent/__init__.py) — Public API re-exports: `run_subagent` (typed SDK driver), `_run_claude_p` (deprecated argv facade), `set_current_fsm_state` (dispatcher-scoped FSM stamp).
- [`cai_lib/subagent/core.py`](../../cai_lib/subagent/core.py) — `run_subagent(prompt, options, *, ...)` executes agents via the Claude Agent SDK with typed options; owns the `_collect_results` query driver and the `cli_path` pin that keeps the SDK pointed at the npm-installed `claude` binary (issue #1226).
- [`cai_lib/subagent/legacy.py`](../../cai_lib/subagent/legacy.py) — Deprecated `_run_claude_p(...)` argv facade wrapping the SDK with `claude -p` command-line syntax; `_argv_to_options(argv, cwd)` parses command-line arguments into SDK options; both retained for 12+ remaining handlers not yet ported to `run_subagent`; will become deletable once migration completes.
- [`cai_lib/subagent/cost.py`](../../cai_lib/subagent/cost.py) — Cost-row build helpers, `<!-- cai-cost … -->` comment format, and best-effort issue/PR comment posting after invocation (when a target kind and number are provided). Shared between `run_subagent` and `_run_claude_p`.
- [`cai_lib/subagent/fsm_state.py`](../../cai_lib/subagent/fsm_state.py) — `set_current_fsm_state(state_name)` context manager that stamps FSM state on cost-log rows (issue #1203); used by the dispatcher to attribute cost to the funnel position (REFINING, PLANNING, IN_PROGRESS, etc.).
- [`cai_lib/subagent/stderr_sink.py`](../../cai_lib/subagent/stderr_sink.py) — Stderr-capture sink wired into `ClaudeAgentOptions.stderr`; bounds the capture and routes SDK errors to logs so transient failures (network, OOM, signal) don't disappear into the wrapper's stream.
- [`cai_lib/subagent/errors.py`](../../cai_lib/subagent/errors.py) — SDK-error diagnostic summariser; parses `ClaudeAgentError` exceptions and emits readable error descriptions for logging and debugging.

## Dependencies

- **config** — imports constants (`REPO`, `CAI_COST_COMMENT_RE`) and logging infrastructure (`log_run`, `log_cost`).
- **github-glue** — calls `_strip_cost_comments` to remove stale cost-attribution comments before re-invoking an agent.
- **audit** (indirect) — cost rows published by this module feed into `cai_lib/audit/cost.py` for reporting and analysis.

## Operational notes

- **Two call paths coexist.** `run_subagent` is the new SDK-native path (issue #1226); `_run_claude_p` is the deprecated argv path. Both emit identical cost-row schemas so telemetry readers need not distinguish. Migration is in progress (12+ handlers still on `_run_claude_p`); **do NOT delete `legacy.py` until all handlers are ported.**
- **Cost-row schema is shared.** Both paths stamp `cache_hit_rate`, `models`, `fsm_state`, `target_kind`/`target_number`, `module`/`scope_files`, and optional per-call fields. Readers of `cai-cost.jsonl` (audit, cost-optimize, cost-report CLI) depend on consistent schema — any field added to one path must be added to both.
- **FSM state is context-only.** The dispatcher (not the handler) calls `set_current_fsm_state(state.name)` before invoking a handler. Non-FSM call sites (rescue, unblock, audit runners, `cmd_misc.init`) leave the contextvar unset and cost rows simply omit the `fsm_state` key. This is intentional — avoid threading state through 27+ call sites just to add context to a few.
- **Stderr sink is best-effort.** The sink bounds capture size and routes SDK errors to logs; a very large stderr output may be truncated. This preserves observability for crash diagnostics without unbounded log growth.
- **Cost comments are strips-on-re-invoke.** When an issue is re-invoked (e.g. on a retry or re-dispatch), `_strip_cost_comments` removes prior cost-attribution comments before the new invocation posts an updated comment. Stale cost comments are never orphaned.
