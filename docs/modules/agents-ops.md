# agents-ops

Ops-oriented subagent definitions — scheduled workflow-failure
check, maintenance-operation runner, and Claude Code release
checker. These agents act on infrastructure concerns rather than
code quality; they sit alongside the lifecycle + review pipelines
rather than inside them.

## Key entry points
- [`.claude/agents/ops/cai-check-workflows.md`](../../.claude/agents/ops/cai-check-workflows.md)
  — analyses recent GitHub Actions workflow failures and writes
  structured findings to `findings.json` for unreported failures.
  Groups related failures and identifies root causes.
- [`.claude/agents/ops/cai-maintain.md`](../../.claude/agents/ops/cai-maintain.md)
  — worktree Bash+Read agent that reads the `Ops:` block from a
  `kind:maintenance` issue body, executes each declared operation
  via the `gh` CLI, and emits a Confidence level.
- [`.claude/agents/ops/cai-update-check.md`](../../.claude/agents/ops/cai-update-check.md)
  — periodic Claude Code release checker. Compares the pinned
  version in the Dockerfile against the latest release and writes
  findings for new versions, feature adoptions, deprecations, and
  best-practice changes.

## Inter-module dependencies
- Invoked by **cli** — `cmd_check_workflows` (cai-check-workflows),
  `cmd_update_check` (cai-update-check).
- Invoked by **actions** — `handle_maintain` / `handle_applied`
  (cai-maintain).
- Consumes **docs** — root `CLAUDE.md`.
- Uses **agents-config** — permission/hook settings.
- Emits findings that **github-glue** (`publish.py`) consumes to
  raise `auto-improve:raised` issues.

## Operational notes
- **Cost tiers.** `cai-check-workflows` and `cai-update-check` are
  sonnet weekly; `cai-maintain` is sonnet and fires only when a
  `kind:maintenance` issue reaches the APPLYING state.
- **Maintain safety.** `cai-maintain` runs Bash inside a worktree
  — operations are executed as declared in the issue's `Ops:`
  block and must be reversible. The confidence signal is the gate
  for whether the ops are actually applied (`handle_applied`).
- **Update-check blast radius.** Findings from `cai-update-check`
  typically propose Dockerfile pin bumps; these hit every
  subagent after the image rebuild, so downstream tests must
  pass before the proposal is applied.
- **FSM invariant.** `cai-maintain` emits `Confidence:
  HIGH|MEDIUM|LOW|STOP`; the handler treats anything below HIGH
  as a divert-to-human.
- **CI implications.** `tests/test_maintain.py` pins
  `handle_maintain` routing.
