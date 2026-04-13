# PR Context Dossier
Refs: robotsix-cai/cai#517

## Files touched
- `cai.py:215` — added `LABEL_HUMAN_SUBMITTED`, `LABEL_PLANNED`, `LABEL_PLAN_APPROVED` constants after `LABEL_AUDIT_NEEDS_HUMAN`
- `cai.py:1106` — added `"human:"` to `_MANAGED_LABEL_PREFIXES` tuple
- `cai.py:7168` — replaced single `LABEL_RAISED` gh issue list call with loop over `(LABEL_RAISED, LABEL_HUMAN_SUBMITTED)`, deduplicates by issue number
- `cai.py:7313` — updated `_set_labels` remove list to include both `LABEL_RAISED` and `LABEL_HUMAN_SUBMITTED`
- `publish.py:96` — added three new label entries: `auto-improve:planned`, `auto-improve:plan-approved`, `human:submitted`

## Files read (not touched) that matter
- `cai.py:1106` — `_MANAGED_LABEL_PREFIXES` controls which labels `_ingest_unlabeled_issues` treats as "managed" (not auto-tagged `:raised`)

## Key symbols
- `LABEL_HUMAN_SUBMITTED` (`cai.py:216`) — new entry point label for human-submitted issues
- `LABEL_PLANNED` (`cai.py:217`) — new planning gate state (unused in logic yet; defined for step 2/3)
- `LABEL_PLAN_APPROVED` (`cai.py:218`) — new planning gate state (unused in logic yet; defined for step 2/3)
- `_MANAGED_LABEL_PREFIXES` (`cai.py:1106`) — guards against `_ingest_unlabeled_issues` re-tagging `human:submitted` issues as `:raised`
- `cmd_refine` (`cai.py:7168`) — updated to also pick up `human:submitted` issues

## Design decisions
- Two separate `gh issue list` calls (one per label) rather than `--search` query — simpler, and `cmd_refine` runs infrequently
- Deduplication by `issue["number"]` in case an issue has both labels
- `_set_labels` safely ignores removing a label not present on the issue (established pattern in codebase)

## Out of scope / known gaps
- `LABEL_PLANNED` and `LABEL_PLAN_APPROVED` are defined but not wired into any logic (step 2/3 of parent issue #481)
- `cmd_fix` / `_select_fix_target` not touched (explicit scope guardrail)
- `cmd_plan` not added (explicit scope guardrail)

## Invariants this change relies on
- `_set_labels` does not error when asked to remove a label that isn't present on the issue
- `_ingest_unlabeled_issues` checks `_MANAGED_LABEL_PREFIXES` to determine if an issue is already managed

## Revision 1 (2026-04-13)

### Rebase
- clean

### Files touched this revision
- `cai.py:929` — added `LABEL_HUMAN_SUBMITTED: 4` to `_STATE_PRIORITY` dict
- `cai.py:1060` — updated `_recover_stale_pr_open` to preserve `LABEL_HUMAN_SUBMITTED` on rollback (ternary chain)
- `cai.py:3802` — added `LABEL_HUMAN_SUBMITTED` to `cmd_verify` remove list for `:pr-open` recovery
- `cai.py:7244` — added `LABEL_HUMAN_SUBMITTED` to early-return remove list ("No Refinement Needed" path)
- `cai.py:7267` — added `LABEL_HUMAN_SUBMITTED` to early-return remove list (multi-step decomposition path)
- `cai.py:8006-8030` — added second `LABEL_HUMAN_SUBMITTED` check in `cmd_cycle` fallback eligibility block
- `docs/cli.md:109` — updated refine description to mention `human:submitted`
- `docs/architecture.md:7` — updated Raise phase to mention `human:submitted`
- `docs/architecture.md:29-34` — added `auto-improve:planned`, `auto-improve:plan-approved`, `human:submitted` rows to Lifecycle Labels table

### Decisions this revision
- `LABEL_HUMAN_SUBMITTED` gets same priority (4) as `LABEL_RAISED` in `_STATE_PRIORITY` — they are equivalent entry-point states
- `cmd_cycle` fallback check uses a second separate `gh issue list` call (same pattern as other checks) rather than combining labels, for consistency
- `_recover_stale_pr_open` ternary preserves `LABEL_HUMAN_SUBMITTED` if present, otherwise falls back to `LABEL_RAISED` (matching the intent of the existing `LABEL_AUDIT_RAISED` preservation logic)

### New gaps / deferred
- None; all five review-pr findings and all three review-docs findings addressed

## Revision 2 (2026-04-13)

### Rebase
- clean

### Files touched this revision
- `README.md:54` — updated refine table row to mention `:raised` or `human:submitted`
- `README.md:91` — updated ASCII lifecycle diagram entry point from `raised` to `raised / human:submitted`
- `README.md:308-316` — expanded entry-points narrative to explain both `auto-improve:requested` (admin) and `human:submitted` (non-admin)
- `cai.py:7151` — updated `cmd_refine()` docstring to mention both entry-point labels
- `docs/cli.md:59` — updated `cycle` command description to mention `human:submitted`

### Decisions this revision
- README diagram updated in-place (single-line label change above the existing `raised` node)
- Narrative expanded to explain the admin vs non-admin distinction between the two entry points

### New gaps / deferred
- None; all five review-pr findings and one review-docs finding addressed
