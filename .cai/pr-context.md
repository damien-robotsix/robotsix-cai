# PR Context Dossier
Refs: damien-robotsix/robotsix-cai#497

## Files touched
- publish.py:67-76 — Added CHECK_WORKFLOWS_CATEGORIES set
- publish.py:125-132 — Added CHECK_WORKFLOWS_LABELS list
- publish.py:258-259 — Added check-workflows branch in _label_set_for()
- publish.py:318-320 — Added check-workflows branch in create_issue() source attribution
- publish.py:343-347 — Added check-workflows branch in create_issue() label assignment
- publish.py:367,374-375 — Added check-workflows to argparse choices and valid_cats dispatch
- cai.py:8458-8547 — Added cmd_check_workflows() function before cmd_test
- cai.py:~8636 — Added check-workflows to argparse subparsers
- cai.py:~8698 — Added check-workflows to handlers dict
- entrypoint.sh:21 — Added check-workflows comment to task list
- entrypoint.sh:55 — Added CAI_CHECK_WORKFLOWS_SCHEDULE env var (default: 0 */6 * * *)
- entrypoint.sh:77 — Added crontab line for check-workflows
- README.md:68 — Added check-workflows row in subcommand table
- README.md:76 — Added CAI_CHECK_WORKFLOWS_SCHEDULE to env var list

## Files read (not touched) that matter
- cai.py:5000-5209 — cmd_cost_optimize pattern (agent invocation + custom issue creation)
- cai.py:5574-5698 — cmd_update_check pattern (publish.py routing)
- cai.py:5478-5566 — cmd_code_audit pattern (clone + agent + publish)
- publish.py — full file, to understand namespace extension points

## Key symbols
- `cmd_check_workflows` (cai.py:8458) — new command handler
- `CHECK_WORKFLOWS_CATEGORIES` (publish.py:67) — valid category set for the namespace
- `CHECK_WORKFLOWS_LABELS` (publish.py:125) — GitHub label definitions
- `_run_claude_p` (cai.py:492) — wraps claude -p, injects cost logging
- `_gh_json` (cai.py:644) — runs gh CLI and parses JSON output
- `PUBLISH_SCRIPT` (cai.py:166) — path to publish.py

## Design decisions
- Used `--max-turns 3` to cap agent cost since it only reads stdin (no tool use needed)
- Filters bot branches (`auto-improve/`) in the Python wrapper, not in the agent prompt
- 24-hour lookback window keeps the issue volume low and avoids re-reporting old failures
- Chose every-6-hours schedule (same as audit) as a sensible default
- Routed through publish.py for free fingerprint dedup, not custom issue creation logic
- Rejected: one-issue-per-SHA approach — would create noise for repeated failures

## Out of scope / known gaps
- No log fetching — agent only sees run metadata (URL, branch, SHA, conclusion), not logs
- No success/flake detection — flake categorization requires multi-run history not fetched

## Revision 1 (2026-04-13)

### Rebase
- clean

### Files touched this revision
- cai.py:1256 — added "check-workflows" to _BASE_NAMESPACES set
- README.md:80 — added "check-workflows" to the "not run at startup" agent list

### Decisions this revision
- _BASE_NAMESPACES addition is minimal and exactly mirrors the audit namespace pattern

### New gaps / deferred
- none

## Revision 2 (2026-04-13)

### Rebase
- clean

### Files touched this revision
- cai.py:114 (module docstring) — added check-workflows entry following health-report

### Decisions this revision
- Entry follows the same indentation/format as other cron-only commands (code-audit, propose, update-check, health-report)
- Placed between health-report and the startup description paragraph

### New gaps / deferred
- none

## Invariants this change relies on
- _gh_json raises CalledProcessError on non-zero exit; cmd_check_workflows catches it
- publish.py's parse_findings() splits on `### Finding:` headers; agent must use that format
- The check-workflows labels must be created before issues are filed; ensure_labels() handles this
