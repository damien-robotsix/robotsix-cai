# PR Context Dossier
Refs: robotsix/robotsix-cai#698

## Files touched
- `cai_lib/actions/plan.py:342` — added `conf_name` variable and embedded `Confidence: {conf_name}` line inside the `plan_block` string

## Files read (not touched) that matter
- `cai_lib/actions/plan.py` — contains `handle_plan` (writes plan block to issue body) and `handle_plan_gate` (re-reads body and calls `parse_confidence`)

## Key symbols
- `handle_plan` (`cai_lib/actions/plan.py:~280`) — builds and stores the plan block in the issue body
- `plan_block` (`cai_lib/actions/plan.py:342`) — the string written between `<!-- cai-plan-start -->` and `<!-- cai-plan-end -->`
- `parse_confidence` / `_CONFIDENCE_RE` (`cai_lib/fsm.py`) — regex that reads `Confidence: HIGH/MEDIUM/LOW` from the stored body in `handle_plan_gate`

## Design decisions
- Embed `Confidence: {conf_name}` as a plain text line just before `<!-- cai-plan-end -->` — visible to humans reviewing the issue body and parseable by the existing `_CONFIDENCE_RE`
- If `plan_confidence` is `None`, write `Confidence: MISSING` — `parse_confidence` returns `None` for that value, correctly diverting to `:human-needed`

## Out of scope / known gaps
- Manual re-labeling of stuck issues (#626, #628, #649, #650, #670, #671, #675, #679) from `:human-needed` back to `:refined` — operational task, not a code change
- `handle_plan_gate` and `parse_confidence` / `_CONFIDENCE_RE` are unchanged — already fixed by PR #687

## Invariants this change relies on
- `_CONFIDENCE_RE` in `cai_lib/fsm.py` matches `Confidence: HIGH|MEDIUM|LOW` (with optional markdown bold wrapping) — fixed by PR #687
- `_strip_stored_plan_block` strips the *old* plan block before this new one is built, so no double-embedding
