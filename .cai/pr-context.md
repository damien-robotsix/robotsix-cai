# PR Context Dossier
Refs: robotsix-cai/robotsix-cai#377

## Files touched
- .claude/agents/cai-plan.md:5 — changed model from `claude-opus-4-6` to `claude-sonnet-4-6`
- .claude/agents/cai-plan.md:88-92 — rewrote efficiency item 5 to specify `model="haiku"` for Explore subagents

## Files read (not touched) that matter
- .claude/agents/cai-plan.md — the only file changed; read to verify current content before staging

## Key symbols
- `model: claude-opus-4-6` (.claude/agents/cai-plan.md:5) — frontmatter model declaration, changed to sonnet
- efficiency guidance item 5 (.claude/agents/cai-plan.md:88-92) — updated to specify haiku for Explore subagents

## Design decisions
- Used staging path `.cai-staging/agents/cai-plan.md` (not direct edit) — required by claude-code write block on `.claude/agents/` paths
- Kept `--max-budget-usd 1.00` cap in cai.py unchanged — serves as independent safety rail
- Retained original "parallelization" rationale text and appended haiku guidance — more informative than replacing

## Out of scope / known gaps
- cai-select.md model not changed (separate agent, out of scope per issue)
- Single-plan architecture (Phase 3) not implemented — medium risk, warrants its own issue
- cai.py invocation logic (_run_plan_agent, _run_plan_select_pipeline) unchanged

## Invariants this change relies on
- cai-revise already uses haiku delegation pattern — this mirrors proven behavior
- Sonnet-class agents (cai-fix, cai-refine, cai-revise) produce acceptable quality for planning tasks
