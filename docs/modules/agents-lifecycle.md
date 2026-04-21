# agents-lifecycle

Subagent definitions driving the issue/PR lifecycle — triage,
refine, propose, explore, dup-check, rescue, and unblock. Invoked
by handlers in `cai_lib/actions/` (and the `cmd_*` entry points in
`cai_lib/cmd_agents.py` / `cmd_rescue.py` / `cmd_unblock.py`) as
issues move through their state machine.

## Key entry points
- [`.claude/agents/lifecycle/cai-triage.md`](../../.claude/agents/lifecycle/cai-triage.md)
  — inline haiku classifier (REFINE / PLAN_APPROVE / APPLY /
  HUMAN). No tools; full issue body arrives in the user message.
- [`.claude/agents/lifecycle/cai-refine.md`](../../.claude/agents/lifecycle/cai-refine.md)
  — opus issue rewriter; emits structured plans with problem,
  steps, verification, scope guardrails. Scope decomposition is
  NOT refine's job — `cai-split` handles that downstream.
- [`.claude/agents/lifecycle/cai-split.md`](../../.claude/agents/lifecycle/cai-split.md)
  — opus scope evaluator; runs after refine and decides whether
  the refined issue ships as one PR (ATOMIC) or must be broken
  into ordered sub-issues (emits a `## Multi-Step Decomposition`
  block). LOW confidence diverts to `:human-needed`.
- [`.claude/agents/lifecycle/cai-explore.md`](../../.claude/agents/lifecycle/cai-explore.md)
  — sonnet exploration / benchmarking agent with Bash.
- [`.claude/agents/lifecycle/cai-propose.md`](../../.claude/agents/lifecycle/cai-propose.md)
  — weekly opus creative proposer (one proposal per run).
- [`.claude/agents/lifecycle/cai-propose-review.md`](../../.claude/agents/lifecycle/cai-propose-review.md)
  — opus reviewer that grades proposals before submission.
- [`.claude/agents/lifecycle/cai-dup-check.md`](../../.claude/agents/lifecycle/cai-dup-check.md)
  — inline haiku duplicate / already-resolved pre-triage.
- [`.claude/agents/lifecycle/cai-rescue.md`](../../.claude/agents/lifecycle/cai-rescue.md)
  — opus autonomous rescue for `:human-needed` issues/PRs; can
  escalate implement-phase to opus one-shot.
- [`.claude/agents/lifecycle/cai-unblock.md`](../../.claude/agents/lifecycle/cai-unblock.md)
  — admin-comment → FSM-resume-target classifier.
- [`.claude/agents/lifecycle/cai-resume-locator.md`](../../.claude/agents/lifecycle/cai-resume-locator.md)
  — inline haiku resume-step locator; reads an issue/PR's labels,
  body, and recent comments and returns the step at which the
  single-handling drive should resume (or `FIRST` on ambiguity).

## Inter-module dependencies
- Invoked by **actions** — `handle_triage` (cai-triage),
  `handle_refine` (cai-refine), `handle_split` (cai-split),
  `handle_explore` (cai-explore).
- Invoked by **cli** — `cmd_propose` / `cmd_propose_review`
  (weekly creative cycle); `cmd_rescue` (cai-rescue); `cmd_unblock`
  (cai-unblock); `dup_check.check_duplicate_or_resolved`
  (cai-dup-check).
- Consumes **docs** — root `CLAUDE.md` efficiency guidance on
  every invocation.
- Uses **agents-config** — permission/hook settings.
- No direct Python dependencies; all inputs flow through the user
  message.

## Operational notes
- **Cost sensitivity varies widely.** `cai-triage` and
  `cai-dup-check` are the cheapest in the pipeline (haiku, inline,
  no tools) and run on every raised issue — latency and cost
  must stay low. `cai-refine`, `cai-split`, `cai-propose`,
  `cai-propose-review`, `cai-rescue` are opus and therefore
  expensive per invocation but rare. Every issue that clears
  triage hits both `cai-refine` and `cai-split` — two opus
  passes in sequence — before any plan work begins.
- **FSM invariant.** Triage and dup-check emit a verdict string
  the Python caller parses; introducing new verdict values
  requires matched updates to the parser
  (`cai_lib/dup_check.py::parse_dup_check_verdict`, and the
  triage branch in `cai_lib/actions/triage.py`).
- **Rescue escalation.** `cai-rescue` has a one-shot opus
  escalation that flips an `opus-attempted` label on the target
  issue. See `cai_lib/cmd_rescue.py::_issue_has_opus_attempted` —
  the label guard prevents repeat escalations.
- **CI implications.** No dedicated tests; behaviour is exercised
  via `tests/test_dup_check.py`, `tests/test_unblock.py`,
  `tests/test_rescue_opus.py` which stub the agent output.
