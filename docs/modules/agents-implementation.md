# agents-implementation

Subagent definitions that implement fixes and rework on auto-improve
issues and PRs. Includes the plan/select pair for the two-planner
pipeline, the single-shot implement agent, and the revise/rebase/fix-ci
agents that handle review-comment addressing and CI recovery.

## Entry points
- `.claude/agents/implementation/cai-plan.md` — Plan generator.
- `.claude/agents/implementation/cai-implement.md` — Code edit agent.
- `.claude/agents/implementation/cai-revise.md` — Review-comment addresser.
- `.claude/agents/implementation/cai-rebase.md` — Rebase-only conflict resolver.
- `.claude/agents/implementation/cai-fix-ci.md` — CI failure fixer.
