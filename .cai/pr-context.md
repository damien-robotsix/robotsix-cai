# PR Context Dossier
Refs: robotsix-cai/robotsix-cai#309

## Files touched
- `CLAUDE.md` (new) — shared 4-rule efficiency guidance block for all agents
- `.claude/agents/cai-analyze.md` — removed `## Efficiency guidance` section (was at end of file)
- `.claude/agents/cai-confirm.md` — removed `## Efficiency guidance` section (was at end of file)
- `.claude/agents/cai-propose-review.md` — removed `## Efficiency guidance` section (was at end of file)
- `.claude/agents/cai-refine.md` — removed `## Efficiency guidance` section (was at end of file)
- `.claude/agents/cai-update-check.md` — removed `## Efficiency guidance` section (was at end of file)
- `.claude/agents/cai-audit.md` — removed `## Efficiency guidance` section (mid-file, before `## Guardrails`)
- `.claude/agents/cai-code-audit.md` — removed `## Efficiency guidance` section (mid-file, before `## Guardrails`)
- `.claude/agents/cai-propose.md` — removed `## Efficiency guidance` section (mid-file, before `## Memory update`)
- `.claude/agents/cai-plan.md` — renamed section to `## Agent-specific efficiency guidance`, removed rules 1–4, kept rule 5 renumbered as 1
- `.claude/agents/cai-review-pr.md` — renamed section to `## Agent-specific efficiency guidance`, removed rules 1–4, kept rule 5 renumbered as 1

## Files read (not touched) that matter
- All 10 agent files — read to get exact content before writing modified versions to staging

## Key symbols
- `## Efficiency guidance` (10 agent files) — section removed/renamed in all 10 agents
- `## Agent-specific efficiency guidance` (cai-plan.md, cai-review-pr.md) — new section header for agent-unique rule 5

## Design decisions
- Created `CLAUDE.md` at repo root with the 4 shared rules; loaded automatically by Claude Code in headless mode
- For cai-plan and cai-review-pr: renamed section to signal rules 1–4 come from CLAUDE.md; kept rule 5 (Agent-for-broad-exploration) as it's specific to agents with the Agent tool
- All agent file edits go through `.cai-staging/agents/` (write-protected path workaround)
- Did NOT touch: cai-fix.md (9-rule extended version), cai-revise.md (3-rule condensed), cai-review-docs.md (3-rule condensed)

## Out of scope / known gaps
- "Consult your memory first" sections are agent-specific and intentionally left unchanged
- cai-fix.md has an extended 9-rule efficiency block that overlaps but extends the shared 4 rules — not deduplicated (different wording/scope)

## Invariants this change relies on
- Claude Code headless mode (`claude -p --agent`) loads project-root CLAUDE.md (confirmed at v2.1.101)
- The 4 shared rules are byte-for-byte identical across all 10 agents (verified before extraction)
