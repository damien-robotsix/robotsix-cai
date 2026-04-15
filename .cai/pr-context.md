# PR Context Dossier
Refs: damien-robotsix/robotsix-cai#670

## Files touched
- `cai.py:183-187` — merged `from publish import` to add `AUDIT_CATEGORIES, parse_findings, create_issue, issue_exists, ensure_labels`; added `from cai_lib.dup_check import check_duplicate_or_resolved`
- `cai.py:1186-1238` — replaced `_run(["python", PUBLISH_SCRIPT, "--namespace", "audit"])` with inline parse→dup-check→create loop

## Files read (not touched) that matter
- `cai_lib/dup_check.py` — `check_duplicate_or_resolved` takes `issue: dict` with keys `number`, `title`, `body`, `labels`; uses `issue["number"]` only to exclude itself from context (sentinel 0 is safe since real issues are always positive)
- `publish.py` — `parse_findings`, `create_issue`, `issue_exists`, `ensure_labels` signatures and return types

## Key symbols
- `check_duplicate_or_resolved` (`cai_lib/dup_check.py:188`) — runs cai-dup-check haiku agent; returns `DupCheckVerdict` or `None`
- `DupCheckVerdict.should_close` (`cai_lib/dup_check.py:37`) — True iff confidence==HIGH and verdict in (DUPLICATE, RESOLVED)
- `parse_findings` (`publish.py:197`) — parses `### Finding:` blocks from stdout into `list[Finding]`
- `create_issue` (`publish.py:394`) — creates one GitHub issue, returns exit code (0=success)
- `log_run` (`cai_lib/logging_utils`) — writes structured log line; "audit-dup-drop" is the new category

## Design decisions
- Used sentinel `number=0` for pseudo-issue dict — `_fetch_context` excludes the target issue number from context, and 0 is never a real issue number
- Kept `ensure_labels("audit")` call once before the loop (idempotent, cheap)
- `log_run("audit-dup-drop", ...)` before `continue` gives an auditable record of each dropped finding
- Return code is always `0` from the inline loop (subprocess failures are counted in `failed`)

## Out of scope / known gaps
- `cmd_audit_triage` retirement is tracked separately in #671; this PR does NOT remove it
- `publish.py::main()` flow for non-audit namespaces is unchanged

## Invariants this change relies on
- `issue["number"] == 0` is safe as a sentinel because `_fetch_context` uses it only to filter itself out, and GitHub issue numbers start at 1
- `create_issue` returns 0 on success, non-zero on gh CLI failure
- `ensure_labels("audit")` is idempotent; safe to call every audit run
