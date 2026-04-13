# PR Context Dossier
Refs: robotsix/robotsix-cai#529

## Files touched
- `cai.py`:4333 — added `outcome_section` build using `_load_outcome_counts(days=90)` after `cost_section`
- `cai.py`:4335 — added `outcome_section` to `user_message` f-string in `cmd_audit`
- `.claude/agents/cai-audit.md` (via staging) — added bullet 6 to "What you receive", new `workflow_efficiency` check row, new `workflow_efficiency` category row, updated "9 categories" → "10 categories" in two places

## Files read (not touched) that matter
- `cai_lib/logging_utils.py` — confirmed `_load_outcome_counts` returns `{cat: {"total": N, "solved": N}}` dict

## Key symbols
- `_load_outcome_counts` (`cai_lib/logging_utils.py:92`) — already imported in `cai.py:182`, returns per-category total/solved counts
- `cmd_audit` (`cai.py:4183`) — the audit orchestrator where outcome_section is built and injected

## Design decisions
- `total >= 3` guard on the ⚠ flag — prevents spurious warnings on categories with too few data points
- `sorted(outcome_counts.items())` — deterministic table order
- Rejected: computing avg fix_attempt_count — would require additional log parsing; plan explicitly excluded it

## Revision 1 (2026-04-13)

### Rebase
- clean

### Files touched this revision
- `publish.py`:55 — added `"workflow_efficiency"` to `AUDIT_CATEGORIES` set so the publish script accepts findings in the new category

### Decisions this revision
- Added single line to `AUDIT_CATEGORIES`; no other changes needed — category was already defined in `cai-audit.md` by the original fix

### New gaps / deferred
- None

## Out of scope / known gaps
- `cmd_analyze` is not touched — outcome section is audit-only per scope guardrails
- `_load_outcome_counts` internals not changed

## Invariants this change relies on
- `_load_outcome_counts` returns a dict with `"total"` and `"solved"` keys per category bucket
- `_load_outcome_counts` returns `{}` when log is absent (graceful fallback handled)
