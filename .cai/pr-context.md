# PR Context Dossier
Refs: robotsix-cai/robotsix-cai#460

## Files touched
- cai.py:1395 — added `"--max-budget-usd", "1.00"` to `_run_plan_agent()` CLI invocation

## Files read (not touched) that matter
- cai.py — contains `_run_plan_agent()` at line 1380; invokes claude CLI for each of 3 parallel plan agents

## Key symbols
- `_run_plan_agent` (cai.py:1380) — function that spawns a single cai-plan claude session; budget cap added here
- `_run_claude_p` (cai.py) — subprocess wrapper called by `_run_plan_agent`; non-zero exit already handled at line 1404

## Design decisions
- Cap set at $1.00 — covers 95%+ of normal runs (median ~$0.60) per issue evidence
- `--add-dir` kept last — it takes a dynamic value, keeping static flags grouped before it
- No cap on `_run_select_agent` — select agent is a single cheap invocation, not an explorer

## Out of scope / known gaps
- No cap added to other agent invocations (spike, fix, revise, etc.) — not requested
- On budget exhaustion, plan agent may exit non-zero; existing error handling at line 1404 returns a failure string, letting the other 2 plans proceed normally

## Invariants this change relies on
- Claude CLI accepts `--max-budget-usd` as a float string flag
- The 3-plan parallel pipeline provides redundancy if one plan is cut short

## Revision 1 (2026-04-12)

### Rebase
- clean

### Files touched this revision
- cai.py:1381 — added $1.00 budget cap mention to `_run_plan_agent()` docstring
- cai.py:1446 — updated Step 1 comment to note "$1.00 budget" per agent
- cai.py:2259 — updated plan-select pipeline comment to note "$1.00 cap" per plan agent
- README.md:56 — updated fix row to note "each capped at $1.00"

### Decisions this revision
- Addressed all four stale_docs locations flagged by reviewer (cai-review-pr)

### New gaps / deferred
- None

## Revision 2 (2026-04-12)

### Rebase
- resolved: README.md, cai.py — both had "2 vs 3 parallel plan agents" conflicts from PR #459 (3→2) landing on main after Revision 1 was authored; resolved by keeping "2" from HEAD and merging in the budget-cap documentation from the incoming branch

### Files touched this revision
- .claude/agents/cai-plan.md (via staging) — added Hard Rule #2 documenting the $1.00 budget cap per reviewer request

### Decisions this revision
- Used "one of two parallel plans can still succeed" wording in the Hard Rule to match current 2-agent count (not 3 as suggested by reviewer, since PR #459 already reduced the count)

### New gaps / deferred
- None

## Revision 3 (2026-04-12)

### Rebase
- clean

### Files touched this revision
- cai.py:25 — updated module docstring fix subcommand description to note "(each capped at $1.00)" for plan agents

### Decisions this revision
- Inserted cap mention inline in the existing "run 2 parallel plan agents" phrase per reviewer suggestion

### New gaps / deferred
- None
