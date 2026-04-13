# PR Context Dossier
Refs: robotsix/robotsix-cai#545

## Files touched
- `cai.py:944` — `category="implement.plan"` → `category="plan.plan"` in `_run_plan_agent`
- `cai.py:972` — `category="implement.select"` → `category="plan.select"` in `_run_select_agent`

## Files read (not touched) that matter
- `cai.py` (lines 935–980) — confirmed current category values were `implement.plan`/`implement.select` (already changed from `fix.*` by #544); this issue renames them to `plan.*`

## Key symbols
- `_run_plan_agent` (`cai.py:939`) — calls `cai-plan` agent; category now `plan.plan`
- `_run_select_agent` (`cai.py:968`) — calls `cai-select` agent; category now `plan.select`
- `_run_plan_select_pipeline` (`cai.py:982`) — caller of both; invoked only from `cmd_plan`

## Design decisions
- Renamed from `implement.*` → `plan.*` (not `fix.*` → `plan.*` as the issue said, because #544 had already changed `fix.*` to `implement.*`); the semantic intent is unchanged
- Hard cut accepted — historical log entries under old keys are not backfilled

## Out of scope / known gaps
- `.claude/agent-memory/cai-audit/known_recurring_issues.md` intentionally not touched per scope guardrails
- No backfill of existing log entries

## Invariants this change relies on
- `_run_plan_agent` and `_run_select_agent` are called only from `_run_plan_select_pipeline`, which is called only from `cmd_plan` — so `plan.*` is the correct top-level category
