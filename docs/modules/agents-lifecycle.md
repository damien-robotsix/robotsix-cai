# agents-lifecycle

Subagent definitions driving the issue/PR lifecycle — triage, refine,
propose, explore, dup-check, rescue, and unblock. Invoked by handlers
in `cai_lib/actions/` as issues move through their state machine.

## Entry points
- `.claude/agents/lifecycle/cai-triage.md` — REFINE/PLAN_APPROVE/APPLY/HUMAN classifier.
- `.claude/agents/lifecycle/cai-refine.md` — Human-issue rewriter.
- `.claude/agents/lifecycle/cai-explore.md` — Exploration / benchmarking.
- `.claude/agents/lifecycle/cai-propose.md` — Weekly creative proposer.
- `.claude/agents/lifecycle/cai-propose-review.md` — Proposal reviewer.
- `.claude/agents/lifecycle/cai-dup-check.md` — Duplicate / resolved check.
- `.claude/agents/lifecycle/cai-rescue.md` — Parked-state rescuer.
- `.claude/agents/lifecycle/cai-unblock.md` — Admin-comment classifier.
