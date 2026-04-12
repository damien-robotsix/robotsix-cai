# PR Context Dossier
Refs: robotsix-cai#425

## Files touched
- `cai.py:3399-3409` — added `headRefName` (as `branch:`) and truncated body snippet (as `body:`) to `prs_section` PR line

## Files read (not touched) that matter
- `cai.py:3222-3228` — `recent_prs` fetch already requests `headRefName` and `body` fields; no query change needed

## Key symbols
- `prs_section` (`cai.py:3396`) — markdown section passed to the audit agent user message; the loop now surfaces `headRefName` and body
- `head_ref` (`cai.py:3400`) — local var holding `pr.get("headRefName", "")`
- `body_snippet` (`cai.py:3401`) — first 200 chars of PR body with newlines collapsed

## Design decisions
- Truncate body to 200 chars — enough to capture `Refs REPO#N` and summary without bloating the audit message
- Collapse `\n` to space — keeps each PR on a single markdown list line
- Omit fields when empty — cleaner output when `headRefName` or body is absent
- Rejected: updating `cai-audit.md` to remove documented fields — the data was already fetched, so surfacing it is the right fix

## Out of scope / known gaps
- No changes to `cai-audit.md` — the agent definition already documents linked issue references

## Invariants this change relies on
- `recent_prs` gh query already includes `headRefName` and `body` in `--json` fields (cai.py:3222-3228)
- Each PR occupies exactly one line in `prs_section` — newline collapsing preserves this
