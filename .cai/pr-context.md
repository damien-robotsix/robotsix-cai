# PR Context Dossier
Refs: damien-robotsix/robotsix-cai#594

## Files touched
- `cai.py:7454` — `cmd_refine` "Refined Issue" path: wrap `issue.get("body")` with `_strip_stored_plan_block`
- `cai.py:7662` — spike `refine_and_retry` path: wrap `issue.get("body")` with `_strip_stored_plan_block`
- `cai.py:7693` — spike "Refined Issue" path: wrap `issue.get("body")` with `_strip_stored_plan_block`
- `cai.py:7922` — explore `refine_and_retry` path: wrap `issue.get("body")` with `_strip_stored_plan_block`
- `cai.py:7953` — explore "Refined Issue" path: wrap `issue.get("body")` with `_strip_stored_plan_block`

## Files read (not touched) that matter
- `cai.py` (lines 1069 area) — `_strip_stored_plan_block` defined here, confirmed available at all call sites

## Key symbols
- `_strip_stored_plan_block` (`cai.py:~1069`) — strips `<!-- cai-plan-start/end -->` blocks from issue body before quoting
- `original_body` (all five sites) — local variable holding the issue body to be quoted as `> ` prefix lines

## Design decisions
- Wrap at `original_body` assignment rather than at `quoted_original` construction — cleaner, avoids re-introducing stripped content
- Rejected: changing `_extract_stored_plan` to handle quoted markers — would mask the root cause and accumulate corruption across re-refinements

## Out of scope / known gaps
- Did not change `_extract_stored_plan`, `_strip_stored_plan_block`, `cmd_plan`, or `_select_fix_target` per scope guardrails
- Existing plan blocks in already-corrupted issues are not retroactively cleaned

## Invariants this change relies on
- `_strip_stored_plan_block` is idempotent: if no plan block exists, returns body unchanged (no regression)
- All five `original_body` assignments are followed immediately by `quoted_original = "\n".join(...)` — the pattern is consistent
