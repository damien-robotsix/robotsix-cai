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
