# PR Context Dossier
Refs: robotsix-cai/robotsix-cai#463

## Files touched
- .claude/agents/cai-plan.md:63 — added hard rule 2 requiring Explore subagent delegation above threshold; removed soft efficiency item 5

## Files read (not touched) that matter
- .claude/agents/cai-plan.md — the full file was read to produce the staged replacement

## Key symbols
- `## Hard rules` (.claude/agents/cai-plan.md:63) — section where new rule 2 was inserted
- `## Efficiency guidance` (.claude/agents/cai-plan.md:67) — item 5 removed from here (superseded by hard rule 2)

## Design decisions
- Threshold set at 3 files / 5 sections / 5 patterns — calibrated to evidence (sessions had 14–25 Read and 14–23 Grep calls)
- Used 6-space indented code block for the example to avoid markdown fence nesting issues
- Added "Do NOT perform the exploration yourself" reinforcing line for stronger compliance
- Rejected: lower threshold of 3 sections / 4 patterns — risks over-delegation on simple issues

## Out of scope / known gaps
- No changes to any other agent definition files
- Model compliance with hard rules is not guaranteed but evidence shows hard rules get higher compliance than efficiency guidance

## Invariants this change relies on
- The `.cai-staging/agents/` mechanism copies files by basename to `.claude/agents/` after the session exits
- The existing `## Hard rules` section is the right location for non-negotiable behavioral constraints

## Revision 1 (2026-04-12)

### Rebase
- clean

### Files touched this revision
- .claude/agents/cai-review-pr.md:hard-rules — added hard rule 7 requiring Explore delegation above threshold; removed soft efficiency item 5
- .claude/agents/cai-review-docs.md:hard-rules — added hard rule 6 requiring Explore delegation above threshold; removed soft efficiency item 3
- .claude/agents/cai-fix.md:efficiency — removed dead efficiency item 9 (Agent not in cai-fix tools list; exploration is cai-plan's job)

### Decisions this revision
- Applied same hard rule (3 files / 5 sections / 5 patterns threshold) to cai-review-pr and cai-review-docs — they both have Agent in their tools lists
- Removed item 9 from cai-fix efficiency guidance rather than promoting it — cai-fix has no Agent tool and receives a ready-made plan from cai-plan, so broad exploration is not its job

### New gaps / deferred
- none

## Revision 2 (2026-04-12)

### Rebase
- clean

### Files touched this revision
- .claude/agents/cai-plan.md:68 — renumbered duplicate hard rule "2" to "3" to restore sequential ordering

### Decisions this revision
- Single-character fix: `2. **Delegate broad exploration` → `3. **Delegate broad exploration` — no other changes

### New gaps / deferred
- none
