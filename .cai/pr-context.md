# PR Context Dossier
Refs: robotsix/robotsix-cai#430

## Files touched
- `cai.py`:261 — added `_row_ts(row)` helper that parses cost-log `ts` field to Unix timestamp
- `cai.py`:5854 — added `cmd_health_report(args)` (~200 lines) implementing four metric sections
- `cai.py`:6286 — added `health-report` argparse subparser with `--dry-run` flag
- `cai.py`:6325 — registered `"health-report": cmd_health_report` in handlers dict
- `entrypoint.sh`:48 — added `CAI_HEALTH_REPORT_SCHEDULE` env var (default `0 7 * * 1`)
- `entrypoint.sh`:67 — added crontab line for `health-report`
- `docker-compose.yml`:45 — added `CAI_HEALTH_REPORT_SCHEDULE` env entry

## Files read (not touched) that matter
- `cai.py`:227-260 — `_load_cost_log` reference for timestamp parse pattern used in `_row_ts`
- `cai.py`:4293-4314 — `cmd_propose` pattern for posting GitHub issues via `_run` (not `_gh_json`)
- `cai.py`:467 — `_gh_json` signature (raises on non-zero exit; wrapped in try/except throughout)

## Key symbols
- `_row_ts` (`cai.py`:261) — new helper, parses cost-log row ts to float Unix time
- `cmd_health_report` (`cai.py`:5854) — main new command function
- `_parse_gh_ts` (inner function in cmd_health_report) — parses GitHub API timestamps, strips fractional seconds

## Design decisions
- Wrapped every `_gh_json` call in its own try/except so one API failure only degrades one section
- Used `_run` (not `_gh_json`) for `gh issue create` to avoid raising on non-zero exit
- PR state comparison done case-insensitively (`.upper() == "CLOSED"`) to handle both `gh` CLI variants
- Stall lists capped to 10 items with "…and N more" to control report body size
- `_parse_gh_ts` strips fractional seconds before parsing to handle GitHub timestamps with microseconds
- Rejected: using `--search` flag on `gh pr list` (unreliable vs Python-side filtering)

## Out of scope / known gaps
- `health-report` label must exist in the repo; if it doesn't, `gh issue create` fails gracefully (logged)
- "Median time per state" metric deliberately excluded (requires per-issue timeline API calls)
- No modification to existing subcommands

## Invariants this change relies on
- `_load_cost_log` returns list[dict] with `ts` in `%Y-%m-%dT%H:%M:%SZ` format
- `_gh_json` raises `subprocess.CalledProcessError` on non-zero exit (so bare except catches it)
- `LABEL_*` constants are defined at module level (lines 165-174)
- `log_run` is importable and available at module scope

## Revision 1 (2026-04-12)

### Rebase
- resolved: entrypoint.sh (kept both CAI_SPIKE_SCHEDULE from main and CAI_HEALTH_REPORT_SCHEDULE from PR)

### Files touched this revision
- `entrypoint.sh`:17 — added `health-report` to header comment task list
- `cai.py`:109 — added `health-report` entry to module docstring
- `README.md`:66 — added `health-report` row to command reference table
- `README.md`:74 — added `CAI_HEALTH_REPORT_SCHEDULE` to env vars list
- `README.md`:534 — added `health-report` to run log commands list

### Decisions this revision
- All five stale_docs findings from review-pr addressed with minimal targeted changes

### New gaps / deferred
- None

## Revision 2 (2026-04-12)

### Rebase
- clean

### Files touched this revision
- `install.sh`:148 — added `CAI_HEALTH_REPORT_SCHEDULE: "0 7 * * 1"` after `CAI_MERGE_SCHEDULE` in OAuth-mode generated docker-compose.yml template
- `install.sh`:215 — added same `CAI_HEALTH_REPORT_SCHEDULE` entry in API-key-mode generated docker-compose.yml template

### Decisions this revision
- Inserted health-report schedule variable in the same relative position (after CAI_MERGE_SCHEDULE) as in docker-compose.yml to maintain consistency

### New gaps / deferred
- None
