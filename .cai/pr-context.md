# PR Context Dossier
Refs: robotsix/robotsix-cai#478

## Files touched
- cai.py:142 — removed `from concurrent.futures import ThreadPoolExecutor, as_completed` (no longer needed)
- cai.py:1406 — `_run_plan_agent()`: added optional `first_plan: str = ""` parameter; when set, appends a "First Plan (for reference)" section to the user message instructing the agent to propose an alternative
- cai.py:1467 — `_run_plan_select_pipeline()`: replaced parallel `ThreadPoolExecutor` block with serial calls (plan1 then plan2 with `first_plan=plan1`)
- cai.py:2291 — updated comment from "parallel" to "serial" to reflect new flow
- cai.py:2335 — corrected "3 independently generated" to "2 serially generated"
- .claude/agents/cai-plan.md (via staging) — updated description; added item 4 to "What you receive"; updated budget cap hard rule
- .claude/agents/cai-select.md (via staging) — updated description to reflect serial planners

## Files read (not touched) that matter
- cai.py:1439–1464 — `_run_select_agent()`: unchanged; still receives a list of plans and the issue

## Key symbols
- `_run_plan_agent` (cai.py:1406) — now accepts `first_plan` kwarg to drive alternative-plan generation
- `_run_plan_select_pipeline` (cai.py:1467) — rewritten from parallel to serial; plan2 receives plan1's output

## Design decisions
- Removed `ThreadPoolExecutor`/`as_completed` import since it was only used in the now-replaced parallel block
- If Plan 1 fails (returns an error string), Plan 2 still runs with that error string as `first_plan` — it will effectively generate an independent plan, which is acceptable
- Rejected: keeping parallel execution and just passing plan1 to plan2 asynchronously — serial is simpler and matches the issue's intent

## Out of scope / known gaps
- Wall-clock time increases ~2× due to serial execution; this is an accepted trade-off per the issue
- No changes to `_run_select_agent` — it still handles exactly 2 plans

## Invariants this change relies on
- `_run_plan_agent` returns a string (either plan text or an error string) — never raises
- `_run_select_agent` handles any list of plan strings, including error strings

## Revision 1 (2026-04-12)

### Rebase
- clean

### Files touched this revision
- README.md:56 — "2 parallel plan agents" → "2 serial plan agents (second sees first and proposes alternative)"
- cai.py:25-26 — module docstring updated to match serial execution model
- .cai-staging/agents/cai-fix.md:456 — "multiple independently generated candidates" → "2 serially generated candidates"

### Decisions this revision
- Updated all three stale_docs sites flagged by reviewer in one pass

### New gaps / deferred
- None
