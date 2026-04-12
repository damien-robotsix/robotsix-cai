# PR Context Dossier
Refs: robotsix-cai/robotsix-cai#459

## Files touched
- `cai.py:1383` — docstring: "3×" → "2×" in `_run_plan_agent`
- `cai.py:1438` — docstring: "3-plan" → "2-plan" in `_run_plan_select_pipeline`
- `cai.py:1445` — comment: "3 plan agents" → "2 plan agents"
- `cai.py:1446` — print statement: "3 plan agents" → "2 plan agents"
- `cai.py:1447` — plans list: `["", "", ""]` → `["", ""]`
- `cai.py:1448` — ThreadPoolExecutor: `max_workers=3` → `max_workers=2`
- `cai.py:1451` — range: `range(1, 4)` → `range(1, 3)`
- `.cai-staging/agents/cai-plan.md` — description frontmatter: "One of three parallel planners" → "One of two parallel planners"

## Files read (not touched) that matter
- `cai.py:1409-1434` — `_run_select_agent` uses `enumerate(plans, 1)` so it adapts to any list length automatically

## Key symbols
- `_run_plan_select_pipeline` (`cai.py:1437`) — orchestrates parallel plan generation; all 3→2 changes live here
- `_run_plan_agent` (`cai.py:1380`) — individual plan runner; only docstring updated

## Design decisions
- Changed only the count from 3 to 2; no structural changes to the pipeline
- Rejected: leaving `_run_select_agent` unchanged — it already uses `enumerate(plans, 1)` so no change needed

## Out of scope / known gaps
- `cai-select.md` agent definition does not hardcode "3 plans" — no change needed

## Revision 1 (2026-04-12)

### Rebase
- clean

### Files touched this revision
- `cai.py:25` — module docstring: "3 parallel plan agents" → "2 parallel plan agents"
- `cai.py:2258` — cmd_fix comment: "3 plan agents in parallel" → "2 plan agents in parallel"
- `README.md:56` — fix table row: "runs 3 parallel plan agents" → "runs 2 parallel plan agents"

### Decisions this revision
- All three were straightforward stale references — updated to match the implementation change

### New gaps / deferred
- None

## Invariants this change relies on
- `plans[idx - 1]` indexing is safe: `range(1, 3)` produces indices 1 and 2, `plans` has length 2
- `_run_select_agent` iterates with `enumerate(plans, 1)` — list-length agnostic
