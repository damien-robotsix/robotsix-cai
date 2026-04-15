# PR Context Dossier
Refs: robotsix-cai/robotsix-cai#659

## Files touched
- `cai_lib/actions/plan.py`:394-404 — replaced `log_run(result="ok"); return 0` with `return handle_plan_gate(issue)` at the tail of `handle_plan`

## Files read (not touched) that matter
- `cai_lib/actions/plan.py` — source of both `handle_plan` and `handle_plan_gate`; verified `_cai_plan_confidence` stash at line 380 is set before the gate call

## Key symbols
- `handle_plan` (`cai_lib/actions/plan.py`:~200) — planning handler; now calls gate inline
- `handle_plan_gate` (`cai_lib/actions/plan.py`:411) — unchanged; diverts MEDIUM/LOW to `:human-needed`
- `issue["_cai_plan_confidence"]` (`cai_lib/actions/plan.py`:380) — stashed before the inline call so the gate can read it without reparsing

## Design decisions
- Removed intermediate `log_run(result="ok")` — `handle_plan_gate`'s own `log_run(result="gate_ok")` is the authoritative terminal log
- Kept dispatcher registry `PLANNED → handle_plan_gate` unchanged — it acts as recovery safety net for issues already stuck at `:planned`
- `return handle_plan_gate(issue)` is inside the `try` block so the `finally` cleanup still fires

## Out of scope / known gaps
- Operational fix for issue #648 (manually move label to `:human-needed`) — cannot be done via code change; requires `gh` CLI access
- No changes to `handle_plan_gate`, dispatcher registry, FSM, or any other handler

## Invariants this change relies on
- `issue["_cai_plan_confidence"]` is always set at line 380 before the success-path tail is reached
- All error paths in `handle_plan` return before line 394, so the gate call only fires on success
