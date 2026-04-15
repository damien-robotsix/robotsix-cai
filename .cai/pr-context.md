# PR Context Dossier
Refs: robotsix-cai/robotsix-cai#626

## Files touched
- `publish.py`:141 — added `"check-workflows:raised"` to `LABELS_TO_DELETE`
- `publish.py`:178-185 — replaced `("check-workflows:raised", ...)` with `("auto-improve", ...)` and `("auto-improve:raised", ...)` in `CHECK_WORKFLOWS_LABELS`
- `publish.py`:340 — added `CHECK_WORKFLOWS_LABELS` to `ensure_all_labels` loop
- `publish.py`:435-440 — changed `create_issue` check-workflows branch to emit `auto-improve,auto-improve:raised,check-workflows` instead of `check-workflows,check-workflows:raised`
- `cai_lib/watchdog.py`:215-267 — added `_migrate_check_workflows_raised()` migration helper
- `cai.py`:211 — extended watchdog import to include `_migrate_check_workflows_raised`
- `cai.py`:3206-3216 — added migration call at top of `cmd_check_workflows`
- `tests/test_publish.py`:10 — added `CHECK_WORKFLOWS_LABELS`, `LABELS_TO_DELETE` to imports
- `tests/test_publish.py`:140-159 — added `TestCheckWorkflowsLabels` test class

## Files read (not touched) that matter
- `cai_lib/watchdog.py` — migration helper pattern copied from `_migrate_audit_raised_labels`
- `publish.py` lines 120-185 — label constants and `LABELS_TO_DELETE`
- `publish.py` lines 332-360 — `ensure_all_labels` loop
- `publish.py` lines 428-458 — `create_issue` label-building logic

## Key symbols
- `_migrate_check_workflows_raised` (`cai_lib/watchdog.py`:215) — migration helper that relabels open `check-workflows:raised` issues to `auto-improve:raised + check-workflows`
- `CHECK_WORKFLOWS_LABELS` (`publish.py`:178) — now includes `auto-improve` and `auto-improve:raised`, excludes `check-workflows:raised`
- `LABELS_TO_DELETE` (`publish.py`:124) — now includes `"check-workflows:raised"` for cleanup

## Design decisions
- Placed migration helper in `cai_lib/watchdog.py` alongside `_migrate_audit_raised_labels` — avoids creating a new module; mirrors the same pattern exactly
- Did NOT add `LABEL_CHECK_WORKFLOWS_RAISED` to config.py — the string literal `"check-workflows:raised"` is only used in the migration helper and `LABELS_TO_DELETE`; a constant is not needed for such limited use
- Rejected: creating `cai_lib/cmd_lifecycle.py` (as suggested in detailed issue plan) — the Selected Implementation Plan explicitly chose `watchdog.py` as the location

## Out of scope / known gaps
- Did not touch `cai_lib/fsm.py` or any FSM transition logic (per scope guardrails)
- Did not touch `cmd_audit_triage` or `audit:raised` path (per scope guardrails)
- Did not retire `auto-improve:no-action` (that is Step 6)
- `check-workflows` source label retained on all new findings so `--label check-workflows` dedup queries still work

## Invariants this change relies on
- `_gh_json`, `_set_labels`, `LABEL_RAISED`, `REPO`, `log_run` are already imported in `cai_lib/watchdog.py`
- `cmd_check_workflows` prints before calling `t0 = time.monotonic()` — migration call inserted between the initial print and `t0`
- `auto-improve` and `auto-improve:raised` labels already exist in `LABELS` (used in the dedup) — adding them to `CHECK_WORKFLOWS_LABELS` just ensures idempotent creation via `ensure_all_labels`
