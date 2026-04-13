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

## Revision 1 (2026-04-13)

### Rebase
- clean

### Files touched this revision
- `cai.py:73` — added `plan` subcommand entry to module docstring (after `refine`)
- `cai.py:131` — added clarifying note that `plan` runs on cron, not synchronous startup
- `entrypoint.sh:22` — added `plan` to independent cron-scheduled tasks comment
- `entrypoint.sh:56` — added `CAI_PLAN_SCHEDULE` env var (default `0 11 * * *`)
- `entrypoint.sh:79` — added `$CAI_PLAN_SCHEDULE python /app/cai.py plan` cron entry

### Decisions this revision
- `plan` documented as cron-only (not startup) — matches its nature as an async planning step
- Default schedule `0 11 * * *` (daily at 11:00) — arbitrary but consistent with other daily tasks

### New gaps / deferred
- none

## Revision 2 (2026-04-13)

### Rebase
- clean

### Files touched this revision
- `README.md` — added `cai.py plan` row to command table; added `CAI_PLAN_SCHEDULE` to env vars list; added "plan" to "not run at startup" list
- `docs/configuration.md` — added `CAI_PLAN_SCHEDULE` row to Agent Schedules table
- `docker-compose.yml` — added `CAI_PLAN_SCHEDULE: "0 11 * * *"` after `CAI_CHECK_WORKFLOWS_SCHEDULE`
- `install.sh` — added `CAI_PLAN_SCHEDULE: "0 11 * * *"` to both YAML template sections (replace_all)
- `docs/cli.md` — added `## plan` section between `refine` and `review-docs`
- `docs/architecture.md` — added Plan step (step 3) to Pipeline Overview; updated Cycle Command step 5 to mention plan

### Decisions this revision
- Inserted Plan as step 3 in Pipeline Overview, renumbering Fix→Review→Revise→Merge→Confirm accordingly
- Added `## plan` section in docs/cli.md between `refine` and `review-docs` (alphabetical proximity to `refine`)
- `replace_all: true` on install.sh because both YAML templates had identical `CAI_CHECK_WORKFLOWS_SCHEDULE` lines

### New gaps / deferred
- none

## Revision 3 (2026-04-13)

### Rebase
- clean

### Files touched this revision
- none

### Decisions this revision
- All four `stale_docs` findings from cai-review-docs were already addressed in Revision 2; no changes needed.

### New gaps / deferred
- none

## Revision 4 (2026-04-13)

### Rebase
- clean

### Files touched this revision
- `cai.py:950` — added `LABEL_PLAN_APPROVED: 3` and `LABEL_PLANNED: 3` to `_STATE_PRIORITY` dict so `_issue_state_label` reports these states correctly instead of "other"
- `cai.py:1187` — added NOTE comment to `_select_fix_target` docstring explaining `:planned`/`:plan-approved` issues are intentionally excluded until Step 3
- `cai.py:4011` — added `LABEL_PLANNED` to `cmd_verify` recovery cleanup list so conflicting `:planned` + `:pr-open` states are resolved correctly

### Decisions this revision
- `LABEL_PLANNED` and `LABEL_PLAN_APPROVED` both assigned priority 3 (same as `LABEL_REFINED`) — they are all "queued/ready" states at similar pipeline depth; collision is harmless since the dict picks the one that appears first when labels co-occur
- `_select_fix_target` unchanged per scope guardrails; comment added instead to document the intentional gap and point to Step 3

### New gaps / deferred
- none

## Revision 5 (2026-04-13)

### Rebase
- clean

### Files touched this revision
- `cai.py:8481` — added `("planned", LABEL_PLANNED)` and `("plan-approved", LABEL_PLAN_APPROVED)` to `label_states` in `cmd_health_report` Issue Throughput section

### Decisions this revision
- Inserted after `"refined"` to maintain pipeline order (raised → refined → planned → plan-approved → in-progress → pr-open → …)

### New gaps / deferred
- none

## Revision 6 (2026-04-13)

### Rebase
- resolved: docs/architecture.md (conflict between PR's Plan step 3 and HEAD's enhanced Review description from PR #522)

### Files touched this revision
- `docs/architecture.md:9-19` — resolved conflict: kept Plan as step 3, Fix as step 4, took HEAD's enhanced Review description (with review-docs ordering note), renumbered Review→Revise→Merge→Confirm to steps 5→6→7→8
- `cai.py:4019` — added `LABEL_PLAN_APPROVED` to cmd_verify pr-open recovery cleanup list alongside `LABEL_PLANNED`

### Decisions this revision
- Took HEAD's enhanced Review description (includes review-docs ordering enforcement note from PR #522) over the PR branch's shorter version — HEAD version is strictly more complete
- Added LABEL_PLAN_APPROVED to cmd_verify cleanup: both plan-related labels are symmetric intermediate states that should be cleaned up when a PR is found, per reviewer finding

### New gaps / deferred
- none

## Revision 7 (2026-04-13)

### Rebase
- clean

### Files touched this revision
- `cai.py:8175-8182` — added Phase 2.6 to `cmd_cycle` docstring Flow section

### Decisions this revision
- Inserted `2.6. plan one :refined issue (plan-select pipeline → store plan → :planned)` between 2.5 and 3 in the docstring to match implementation

### New gaps / deferred
- docs/cli.md, docs/configuration.md, docs/architecture.md findings from second review comment were already addressed in Revision 2 — no changes needed

## Revision 8 (2026-04-13)

### Rebase
- resolved: docs/architecture.md (conflict between PR's Plan step numbering and HEAD's enhanced Review description from PR #522)

### Files touched this revision
- `docs/architecture.md:9-22` — resolved conflict: kept Plan as step 3, took HEAD's enhanced Review description (with review-docs ordering note and direct-fix detail), renumbered steps 5→8

### Decisions this revision
- Review comment from @damien-robotsix flagged 3 missing registrations (\_STATE\_PRIORITY, cmd\_verify cleanup, label\_states) — all three were already addressed in Revisions 4, 5, and 6 respectively; no changes needed

### New gaps / deferred
- none

## Revision 8 (2026-04-13)

### Rebase
- clean

### Files touched this revision
- none

### Decisions this revision
- All four `stale_docs` findings from third review comment (docs/cli.md ## plan section, docs/configuration.md CAI_PLAN_SCHEDULE row, docs/architecture.md Plan step + Cycle Command, README.md command table + env vars + startup list) were already addressed in Revision 2. Verified by reading all four files — every referenced change is present in the clone.

### New gaps / deferred
- none

## Revision 9 (2026-04-13)

### Rebase
- clean

### Files touched this revision
- none

### Decisions this revision
- All four `stale_docs` findings from fourth review comment (docs/cli.md ## plan section, docs/configuration.md CAI_PLAN_SCHEDULE row, docs/architecture.md Plan step 3 + Cycle Command step 5) were already addressed in Revision 2. Verified by reading all three files — every referenced change is present in the clone.

### New gaps / deferred
- none

## Revision 10 (2026-04-13)

### Rebase
- clean

### Files touched this revision
- `cai.py:92-94` — removed accidental markdown table (argument table) that was embedded inside the module docstring `plan` entry; plain-text prose only belongs in a module docstring

### Decisions this revision
- All five `stale_docs` findings from fifth review comment were already addressed except the embedded table: docs/cli.md, docs/configuration.md, docs/architecture.md, README.md changes all present; cai.py module docstring had the plan entry and cron note but also had 3 spurious table lines that were removed.

### New gaps / deferred
- none
