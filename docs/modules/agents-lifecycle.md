# agents-lifecycle

Subagent definitions driving the issue/PR lifecycle — triage,
refine, propose, explore, dup-check, rescue, confirm, and unblock.
Invoked by handlers in `cai_lib/actions/` (and the `cmd_*` entry
points in `cai_lib/cmd_agents.py` / `cmd_rescue.py` /
`cmd_unblock.py`) as issues move through their state machine.

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
  block). LOW confidence diverts to `:human-needed`. The same
  agent is re-invoked by the post-plan re-split checkpoint
  (issue #1167) with a `## Stored Implementation Plan` section
  in the user message — in that mode it emits KEEP or RESPLIT
  instead of the three pre-plan verdicts.
- [`.claude/agents/lifecycle/cai-explore.md`](../../.claude/agents/lifecycle/cai-explore.md)
  — sonnet exploration / benchmarking agent with Bash.
- [`.claude/agents/lifecycle/cai-propose.md`](../../.claude/agents/lifecycle/cai-propose.md)
  — weekly opus creative proposer (one proposal per run).
- [`.claude/agents/lifecycle/cai-propose-review.md`](../../.claude/agents/lifecycle/cai-propose-review.md)
  — sonnet reviewer that grades proposals before submission.
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
  Defined as the resume-step oracle for the watchdog
  (`cai_lib/watchdog.py`), `cai unblock`
  (`cai_lib/cmd_unblock.py`), and `cai rescue`
  (`cai_lib/cmd_rescue.py`) flows; the Python call sites land
  with parent #1037's inline-drive rollout.
- [`.claude/agents/lifecycle/cai-confirm.md`](../../.claude/agents/lifecycle/cai-confirm.md)
  — sonnet MERGED-state verdict agent; compares a merged PR's diff
  and the recent parsed signals against each open
  `auto-improve:merged` issue's remediation to decide whether it
  can be closed as solved.

## Inter-module dependencies
- Invoked by **actions** — `handle_triage` (cai-triage),
  `handle_refine` (cai-refine), `handle_split` (cai-split),
  `handle_explore` (cai-explore), `handle_confirm` (cai-confirm).
- Invoked by **cli** — `cmd_propose` / `cmd_propose_review`
  (weekly creative cycle); `cmd_rescue` (cai-rescue); `cmd_unblock`
  (cai-unblock); `dup_check.check_duplicate_or_resolved`
  (cai-dup-check). `cai-resume-locator` is defined for use by the
  same CLI-side resume flows (`cmd_rescue`, `cmd_unblock`) and by
  the watchdog (`cai_lib/watchdog.py`); its Python call sites
  land with parent #1037's inline-drive rollout.
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
- **Rescue idempotence.** Every `cai rescue` tick that finishes
  without resuming a target stamps `auto-improve:rescue-attempted`
  on the issue or PR (see `_mark_rescue_attempted` /
  `_NON_RESUMING_TAGS` in `cai_lib/cmd_rescue.py`). Subsequent
  ticks skip labelled targets so the agent stops re-evaluating
  the same parks every cron interval. The marker is stripped by
  every `human_to_*` and `pr_human_to_*` transition (declared in
  `labels_remove`, `cai_lib/fsm_transitions.py`), so any exit
  from `HUMAN_NEEDED` / `PR_HUMAN_NEEDED` re-opens the door for
  a fresh evaluation if the target later re-parks.
- **CI implications.** No dedicated tests; behaviour is exercised
  via `tests/test_dup_check.py`, `tests/test_unblock.py`,
  `tests/test_rescue_opus.py`, and
  `tests/test_rescue_attempted_label.py`, which stub the agent
  output.
