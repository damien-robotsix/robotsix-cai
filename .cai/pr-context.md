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
