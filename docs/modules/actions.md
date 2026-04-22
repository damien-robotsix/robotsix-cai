# actions

Per-state handlers that form the body of the single-handling
inline-drive pipeline — one module per lifecycle state. Each
handler is a pure pipeline function called by
[`cai_lib/dispatcher.py::_drive_target_to_completion`](../../cai_lib/dispatcher.py),
which walks a target from its current state through every
actionable state inside one dispatch tick. Handlers read the
current state, drive the relevant Claude subagent(s), parse the
`Confidence:` line, and decide which FSM transition to apply
next via `cai_lib/fsm_transitions.py`. All handlers share a
common shape: they accept the issue / PR dict fetched by the
dispatcher, emit logs via `log_run`, and return a `HandlerResult`
NamedTuple (`trigger`, `confidence`, `divert_reason`,
`artifacts`, `stop_driving`; defined in
[`cai_lib/dispatcher.py`](../../cai_lib/dispatcher.py)) that the
driver translates into a `fire_trigger` call via `_driver_fire`.

## Key entry points

Issue-side handlers (take an issue dict):
- [`triage.py`](../../cai_lib/actions/triage.py) —
  `handle_triage`: drives `cai-triage` to classify RAISED issues
  into REFINE / PLAN_APPROVE / APPLY / HUMAN; runs the dup-check
  pre-filter first.
- [`refine.py`](../../cai_lib/actions/refine.py) —
  `handle_refine`: runs `cai-refine` (opus) to rewrite a raised
  issue into a structured `## Refined Issue` block. Scope
  decomposition is NOT handled here — the downstream
  `handle_split` owns that decision.
- [`split.py`](../../cai_lib/actions/split.py) —
  `handle_split`: runs `cai-split` (opus) on a `:refined` or
  `:splitting` issue; fires `splitting_to_planning` on ATOMIC +
  HIGH confidence, creates sub-issues via `_create_sub_issues`
  on DECOMPOSE + HIGH confidence, and diverts to `:human-needed`
  on LOW confidence / malformed output / depth-gate violations.
- [`plan.py`](../../cai_lib/actions/plan.py) — `handle_plan`
  (dual-planner + `cai-select` pipeline) and `handle_plan_gate`
  (confidence-based gate into PLAN_APPROVED / HUMAN).
- [`implement.py`](../../cai_lib/actions/implement.py) —
  `handle_implement`: runs `cai-implement` on a fresh worktree,
  opens a PR, and handles multi-step suggested sub-issues.
- [`explore.py`](../../cai_lib/actions/explore.py) —
  `handle_explore`: runs `cai-explore` for NEEDS_EXPLORATION
  issues.
- [`confirm.py`](../../cai_lib/actions/confirm.py) —
  `handle_confirm`: runs `cai-confirm` on merged PRs to verify
  issues are truly resolved.
- The IssueState.PR bounce is not a separate action — the
  recovery decision tree lives inline in
  [`cai_lib/dispatcher.py`](../../cai_lib/dispatcher.py) as
  `_resolve_pr_state` (invoked by `drive_issue`).
- [`maintain.py`](../../cai_lib/actions/maintain.py) —
  `handle_maintain` / `handle_applied`: runs `cai-maintain` for
  `kind:maintenance` infra-ops issues.

PR-side handlers (take a PR dict):
- [`open_pr.py`](../../cai_lib/actions/open_pr.py) —
  `handle_open_to_review`: tags a freshly-opened PR into
  REVIEWING_CODE.
- [`review_pr.py`](../../cai_lib/actions/review_pr.py) —
  `handle_review_pr`: runs `cai-review-pr` and posts ripple
  findings as PR comments.
- [`review_docs.py`](../../cai_lib/actions/review_docs.py) —
  `handle_review_docs`: runs `cai-review-docs` to sync `/docs`
  and the `docs/modules*` registry.
- [`revise.py`](../../cai_lib/actions/revise.py) —
  `handle_revise`: runs `cai-revise` to address review comments;
  uses `_filter_comments_with_haiku` to pre-classify
  resolved/unresolved.
- [`rebase.py`](../../cai_lib/actions/rebase.py) —
  `handle_rebase`: runs `cai-rebase` for rebase-only cycles.
- [`fix_ci.py`](../../cai_lib/actions/fix_ci.py) —
  `handle_fix_ci`: runs `cai-fix-ci` when CI checks fail; fetches
  logs via `_fetch_ci_failure_log`.
- [`merge.py`](../../cai_lib/actions/merge.py) — `handle_merge`:
  runs `cai-merge` and, on HIGH confidence, merges the PR.

## Inter-module dependencies
- Imports from **fsm** — transitions, `Confidence`, state enums
  (every handler).
- Imports from **github-glue** — `_gh_json`, `_set_labels`,
  `_post_issue_comment`, `_post_pr_comment`, remote-lock helpers.
- Imports from **config** — label constants, log paths,
  `WORKTREE_BASE`.
- Imports from **dup_check** — `triage.py` calls
  `check_duplicate_or_resolved` as a pre-filter.
- Imports from **cmd_helpers*** — shared worktree setup,
  agent-edit staging (`_setup_agent_edit_staging`,
  `_apply_agent_edit_staging`), and issue-body formatting.
- Imports from **subprocess_utils** — `_run`, `_run_claude_p`.
- Imports from **logging_utils** — `log_run` for cost/outcome
  accounting.
- Imported by **cli** — the dispatcher registry is built from
  these handler callables.
- Imported by **tests** — `tests/test_maintain.py`,
  `tests/test_plan.py`, `tests/test_multistep.py`, and more.

## Operational notes
- **Cost sensitivity — HIGH.** Every handler launches one or more
  Claude subagents; these are the dominant cost centre alongside
  the audit commands. `handle_plan` is especially expensive (two
  planner runs plus a selector); `handle_implement` runs on Sonnet
  with worktree Read/Write.
- **FSM invariant.** A handler must either advance the FSM or
  divert to `:human-needed`; silently returning 0 strands the
  issue. Use `fire_trigger` with `_confidence_gated=True` so a missing
  `Confidence:` line diverts safely.
- **Worktree hygiene.** `handle_implement`, `handle_revise`,
  `handle_rebase`, `handle_fix_ci`, and `handle_maintain` each
  create and clean up a worktree under `WORKTREE_BASE`; stale
  worktrees are reaped by the watchdog.
- **Remote lock.** Handlers that post comments or flip labels must
  be guarded by `_acquire_remote_lock` / `_release_remote_lock`
  so two dispatcher runs on different hosts do not race.
- **CI implications.** Tests import handlers directly; renaming
  or deleting one without updating the dispatcher registry and
  the matching test breaks the suite.
