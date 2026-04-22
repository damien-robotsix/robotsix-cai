# agents-implementation

Subagent definitions that implement fixes and rework on
auto-improve issues and PRs. Includes the plan/select pair for the
two-planner pipeline, the single-shot implement agent, and the
revise/rebase/fix-ci agents that handle review-comment addressing
and CI recovery. Every agent here runs in a fresh worktree and
expects its user message to carry the full issue/PR context from
the Python caller.

## Key entry points
- [`.claude/agents/implementation/cai-plan.md`](../../.claude/agents/implementation/cai-plan.md)
  — opus plan generator. First of two serial planners; output is
  graded by `cai-select`.
- [`.claude/agents/implementation/cai-select.md`](../../.claude/agents/implementation/cai-select.md)
  — inline opus selector that compares two plans and emits a
  confidence verdict.
- [`.claude/agents/implementation/cai-implement.md`](../../.claude/agents/implementation/cai-implement.md)
  — sonnet code-editing agent. Has no git/gh — the caller
  (`handle_implement`) owns remote state and PR creation. Invokes
  `cai-test-runner` (haiku) in-session to verify tests before
  exiting; the caller runs the suite again post-exit and pushes the
  PR regardless, routing any residual failure to `cai-revise` via a
  top-level PR comment.
- [`.claude/agents/implementation/cai-revise.md`](../../.claude/agents/implementation/cai-revise.md)
  — sonnet review-comment addresser. Runs only when the
  haiku-filtered comment set is non-empty.
- [`.claude/agents/implementation/cai-rebase.md`](../../.claude/agents/implementation/cai-rebase.md)
  — lightweight rebase-only conflict resolver for the conflict-only
  branch of the revise handler.
- [`.claude/agents/implementation/cai-fix-ci.md`](../../.claude/agents/implementation/cai-fix-ci.md)
  — sonnet CI-failure fixer. Receives a failure-log section and
  makes the minimal targeted fix.

## Inter-module dependencies
- Invoked by **actions** — `handle_plan` (cai-plan, cai-select),
  `handle_implement` (cai-implement), `handle_revise` (cai-revise,
  cai-comment-filter), `handle_rebase` (cai-rebase),
  `handle_fix_ci` (cai-fix-ci).
- Delegates to **agents-utility** — `cai-implement` calls
  `cai-test-runner` (haiku) for regression test runs;
  `cai-revise` / `cai-rebase` call `cai-git` for git plumbing.
- Consumes **docs** — the root `CLAUDE.md` is loaded by
  claude-code in headless mode for every subagent invocation.
- Uses **agents-config** — permission allowlist, hook, and env
  settings from `.claude/settings.json`.
- No direct Python dependencies; inputs arrive as user-message
  text constructed by the caller in `cai_lib/actions/` and
  `cai_lib/cmd_helpers*.py`.

## Operational notes
- **Cost tiers.** `cai-plan` and `cai-select` are opus (quality of
  plan matters most); `cai-implement`, `cai-revise`,
  `cai-rebase`, `cai-fix-ci` are sonnet. Dropping a tier is a
  measurable quality regression; `cai-cost-optimize` weighs these
  trade-offs.
- **FSM invariant.** Every agent here must emit `Confidence:
  HIGH|MEDIUM|LOW|STOP` on its last line; missing or malformed
  confidence is parsed as `STOP` and diverts the target to
  `:human-needed`.
- **Worktree model.** The caller provides a work-directory path
  in the user message (`/tmp/cai-*-<n>-<hash>`); the agent uses
  absolute paths under that tree for every Read/Edit/Write.
  Writes to `.claude/agents/*.md` must go through the
  `.cai-staging/agents/` directory.
- **CI implications.** Agent-definition changes are exercised
  indirectly via live runs — no unit tests. The on-demand
  `cai-audit-good-practices` auditor flags best-practice drift
  when explicitly invoked against this module.
