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
