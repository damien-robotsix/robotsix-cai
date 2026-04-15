# PR Context Dossier
Refs: robotsix-cai/robotsix-cai#658

## Files touched
- `cai_lib/actions/plan.py:282` — replaced silent fallthrough comment with explicit `else` block that logs and returns 1
- `cai_lib/actions/plan.py:376` — captured return value of `apply_transition("planning_to_planned")` and propagated failure as return 1
- `tests/test_plan.py` — new test file verifying unexpected-state early exit

## Files read (not touched) that matter
- `cai_lib/actions/plan.py` — main action handler, lines 270–395

## Key symbols
- `handle_plan()` (`cai_lib/actions/plan.py:~240`) — the action handler being fixed
- `apply_transition()` (`cai_lib/fsm.py`) — returns bool; false on failure
- `IssueState` (`cai_lib/fsm.py`) — enum; RAISED, REFINED, PLANNING etc.
- `log_run` (`cai_lib/actions/plan.py`) — already imported, reused in new else block

## Design decisions
- Early exit logs and returns 1 without calling `apply_transition` — for `:raised` state there is no valid transition
- Capture `ok` from `apply_transition("planning_to_planned")` to surface label-edit failures as non-zero exit
- Rejected: calling `planning_to_human` in the else block — would produce noise/errors for states that have no valid planning transition

## Out of scope / known gaps
- Operational fix for issue #647 (removing stale `:raised` label via `gh issue edit`) — must be done manually
- `handle_plan_gate()` — not modified, its hardcoded labels are correct
- `apply_transition()` in `fsm.py` — not modified

## Invariants this change relies on
- `apply_transition()` returns a truthy bool on success and falsy on failure
- The `else` branch fires only for states that are neither REFINED nor PLANNING
- The early return happens before `work_dir` is created, so the `finally` cleanup block safely skips `shutil.rmtree`
