# PR Context Dossier
Refs: robotsix-cai/cai#518

## Files touched
- `cai.py:1263` — added `_select_plan_target` helper (queue-based + specific issue lookup)
- `cai.py:1576` — added `_extract_stored_plan` helper (parses `<!-- cai-plan-start/end -->` markers)
- `cai.py:1591` — added `_strip_stored_plan_block` helper (removes existing plan block before re-prepending)
- `cai.py:1606` — added `cmd_plan` function (orchestrates plan-select pipeline + stores result in issue body)
- `cai.py:8127` — added Phase 2.6 in `cmd_cycle` (runs `cmd_plan` after Phase 2.5 refine)
- `cai.py:8859` — registered `plan` subparser with `--issue` argument
- `cai.py:8931` — added `"plan": cmd_plan` to `handlers` dict

## Files read (not touched) that matter
- `cai.py:1155` — `_select_fix_target` as the naming/structure template for `_select_plan_target`
- `cai.py:2210` — `cmd_fix` as the structural template for `cmd_plan`
- `cai.py:1489` — `_run_plan_select_pipeline` (reused unchanged by `cmd_plan`)

## Key symbols
- `_select_plan_target` (`cai.py:1263`) — queue-based or specific-issue selection for planning
- `_extract_stored_plan` (`cai.py:1576`) — parses plan from `<!-- cai-plan-start/end -->` markers in issue body
- `_strip_stored_plan_block` (`cai.py:1591`) — removes old plan block before re-prepending on re-run
- `cmd_plan` (`cai.py:1606`) — full planning command: select → clone → pipeline → store → label transition
- `LABEL_PLANNED` (`cai.py:217`) — `auto-improve:planned` label applied after successful planning

## Design decisions
- `_select_plan_target` placed right after `_select_fix_target` (before `_set_labels`) for naming convention consistency
- `_extract_stored_plan`, `_strip_stored_plan_block`, and `cmd_plan` placed right after `_run_plan_select_pipeline` for logical grouping
- Plan markers: `<!-- cai-plan-start -->` / `<!-- cai-plan-end -->` with `## Selected Implementation Plan` heading inside
- Phase 2.6 runs immediately after Phase 2.5 (refine) in `cmd_cycle` — a just-refined issue can be planned in the same cycle
- Rejected: modifying `_select_fix_target` or `cmd_fix` — scope guardrails explicitly forbid it for this step

## Out of scope / known gaps
- `cmd_fix` still runs the plan pipeline inline and picks up `:refined` issues — backward compat unchanged until Step 3
- `_extract_stored_plan` is added but not yet consumed by `cmd_fix` — that wiring is Step 3's job
- No `:in-progress` lock during planning (planning is non-destructive; matches existing pattern)

## Invariants this change relies on
- `_run_plan_select_pipeline` is called with `(issue, work_dir, attempt_history_block)` — signature unchanged
- `LABEL_PLANNED` (`auto-improve:planned`) is already defined at line 217
- `_set_labels`, `log_run`, `_fetch_previous_fix_attempts`, `_build_attempt_history_block`, `_run`, `_gh_json` all exist and accept the call signatures used
