# PR Context Dossier
Refs: damien-robotsix/robotsix-cai#537

## Files touched
- `cai_lib/config.py`:52 — `LABEL_REQUESTED` value changed from `"auto-improve:requested"` to `"human:requested"`
- `cai_lib/config.py`:68 — `LABEL_PLAN_APPROVED` value changed from `"auto-improve:plan-approved"` to `"human:plan-approved"`
- `publish.py`:86 — label tuple name updated to `"human:requested"`
- `publish.py`:97 — label tuple name updated to `"human:plan-approved"`
- `.github/workflows/admin-only-label.yml`:22 — updated restricted label list: replaced `"auto-improve:requested"` with `"human:requested"`, added `"human:plan-approved"`
- `README.md`:333 — prose updated from `auto-improve:requested` to `human:requested`
- `docs/architecture.md`:9 — parenthetical updated to `human:plan-approved`
- `docs/architecture.md`:30,32 — label table rows updated to new names
- `cai.py`:8157 — dashboard shorthand updated from `"plan-approved"` to `"h:plan-approved"`

## Files read (not touched) that matter
- `docs/cli.md` — checked, no references to old label names

## Key symbols
- `LABEL_REQUESTED` (`cai_lib/config.py`:52) — all Python references use this constant; rename propagates automatically
- `LABEL_PLAN_APPROVED` (`cai_lib/config.py`:68) — same pattern; no other .py edits needed
- `_MANAGED_LABEL_PREFIXES` (`cai.py`:589) — already includes `"human:"` prefix, so no change needed

## Design decisions
- Added `"human:plan-approved"` to the admin-only guard in the workflow — it was previously unguarded despite triggering autonomous fix execution
- Dashboard shorthand changed to `"h:plan-approved"` to reflect new namespace prefix
- No migration code added for existing GitHub issues — a one-time `gh label edit` is sufficient and avoids dead code on every cycle

## Out of scope / known gaps
- Existing GitHub issues labeled `auto-improve:requested` or `auto-improve:plan-approved` retain the old labels; a one-time migration via `gh label edit` on those issues is needed post-merge
- `_BASE_NAMESPACES` in `cai_lib/github.py` intentionally NOT updated — `human:` labels don't need a bare `human` base label

## Invariants this change relies on
- All Python code references the labels via `LABEL_REQUESTED` / `LABEL_PLAN_APPROVED` constants, never as bare strings
- `"human:"` is already in `_MANAGED_LABEL_PREFIXES` so these labels are excluded from auto-ingestion
