# PR Context Dossier
Refs: robotsix-cai/robotsix-cai#377

## Files touched
- .claude/agents/cai-plan.md:71-80 — expanded efficiency item 1 with haiku model guidance and fallback threshold

## Files read (not touched) that matter
- .claude/agents/cai-plan.md — the only file changed; read to verify current content before staging

## Key symbols
- `model: claude-opus-4-6` (.claude/agents/cai-plan.md:5) — frontmatter model declaration, kept as opus per reviewer request
- efficiency guidance item 1 (.claude/agents/cai-plan.md:71-80) — updated to specify haiku for Explore subagents with fallback threshold

## Design decisions
- Reverted model from sonnet back to opus per reviewer request ("we should keep opus for planning the solution")
- Kept `--max-budget-usd 1.00` cap in cai.py unchanged — serves as independent safety rail
- Only haiku exploration guidance change remains — reviewer said "This is alright"

## Out of scope / known gaps
- cai-select.md model not changed (separate agent, out of scope per issue)
- Single-plan architecture (Phase 3) not implemented — medium risk, warrants its own issue
- cai.py invocation logic (_run_plan_agent, _run_plan_select_pipeline) unchanged

## Invariants this change relies on
- cai-revise already uses haiku delegation pattern — this mirrors proven behavior

## Revision 1 (2026-04-12)

### Rebase
- clean

### Files touched this revision
- .claude/agents/cai-fix.md:204-208 — updated efficiency item 9 to specify `model="haiku"` for Explore subagents
- .claude/agents/cai-review-pr.md:141-145 — updated efficiency item 5 to specify `model="haiku"` for Explore subagents
- .claude/agents/cai-review-docs.md:117-119 — updated efficiency item 3 to specify `model="haiku"` for Explore subagents

### Decisions this revision
- Applied same haiku optimization pattern to cai-fix, cai-review-pr, cai-review-docs — aligns all agents with cai-plan and cai-revise
- cai-review-pr and cai-review-docs already run on haiku themselves but guidance still applies when they spawn Explore subagents

### New gaps / deferred
- None

## Revision 2 (2026-04-12)

### Rebase
- clean

### Files touched this revision
- .claude/agents/cai-review-pr.md:62-65 — updated "How to work" item 2 to specify `model="haiku"` for Explore subagents (matched efficiency guidance section)
- .claude/agents/cai-spike.md:65 — updated process step 4 to specify `model="haiku"` for Explore subagents

### Decisions this revision
- Both changes use the same wording pattern as efficiency guidance sections in each file — minimal targeted fix
- cai-review-pr.md "How to work" item 2 now consistent with its own efficiency guidance item 5

### New gaps / deferred
- None

## Revision 3 (2026-04-13)

### Rebase
- resolved: .claude/agents/cai-plan.md, .claude/agents/cai-fix.md, .claude/agents/cai-review-docs.md, .claude/agents/cai-review-pr.md, .claude/agents/cai-spike.md

### Files touched this revision
- .cai/pr-context.md — updated to reflect model revert and correct file references

### Decisions this revision
- Reverted cai-plan.md model from sonnet back to opus — reviewer requested "keep opus for planning the solution"
- The rebase conflict resolution script for cai-plan.md already restored `claude-opus-4-6`; no separate edit needed
- Rebase conflicts: cai-fix.md took HEAD (no item 9 — cai-fix has no Agent tool); cai-review-docs.md, cai-review-pr.md, cai-spike.md all took HEAD (already had haiku + "Do NOT delegate decisions" guard)

### New gaps / deferred
- None

## Revision 4 (2026-04-13)

### Rebase
- clean

### Files touched this revision
- .claude/agents/cai-plan.md:79 — changed `< 3 files` to `3 or fewer files` to match cai-revise.md threshold

### Decisions this revision
- Harmonized Explore fallback threshold to "3 or fewer files" — matches cai-revise.md lines 259/278; reviewer suggested fix

### New gaps / deferred
- None
