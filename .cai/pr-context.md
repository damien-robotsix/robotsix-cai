# PR Context Dossier
Refs: robotsix/robotsix-cai#672

## Files touched
- `cai_lib/actions/triage.py`:40 — removed `DuplicateOf:` parsing from `_parse_issue_triage_verdict`
- `cai_lib/actions/triage.py`:202 — removed `context_issues` and `recent_prs` fetches; simplified `user_message` to issue body only
- `cai_lib/actions/triage.py`:238 — removed `DISMISS_DUPLICATE`/`DISMISS_RESOLVED` verdict execution block
- `cai_lib/actions/triage.py`:316 — removed fall-through safety print for DISMISS at sub-HIGH confidence
- `cai_lib/actions/triage.py`:11 — removed unused `import subprocess`
- `cai_lib/actions/triage.py`:29 — removed unused `_gh_json` import
- `.cai-staging/agents/cai-triage.md` — updated agent prompt: removed DISMISS_DUPLICATE/DISMISS_RESOLVED from routing table, removed "Other open issues" and "Recent PRs" from context section, removed `DuplicateOf:` from output format

## Files read (not touched) that matter
- `cai_lib/actions/triage.py` — primary handler; all DISMISS logic lived here

## Key symbols
- `_parse_issue_triage_verdict` (`cai_lib/actions/triage.py`:39) — verdict parser; removed `DuplicateOf:` field and updated docstring
- `handle_triage` (`cai_lib/actions/triage.py`:120) — main handler; removed context fetch, DISMISS verdict branch, and fall-through safety

## Design decisions
- Kept `cai-dup-check` pre-step intact — that is the new owner of dup/resolved logic
- Removed `context_issues` and `recent_prs` fetches entirely since no remaining triage logic needs them
- Rejected: keeping the fetches "just in case" — they add latency and cost with no benefit now

## Out of scope / known gaps
- `cai-dup-check` agent itself (`cai_lib/dup_check.py`) is unchanged
- No tests existed for `DISMISS_DUPLICATE`/`DISMISS_RESOLVED` triage verdicts, so no test removals needed

## Invariants this change relies on
- `cai-dup-check` runs before `cai-triage` and handles duplicate/resolved classification at HIGH confidence
- `cai-triage` only receives issues that survived the dup-check pre-step

## Revision 1 (2026-04-15)

### Rebase
- clean

### Files touched this revision
- `scripts/generate-index.sh`:52 — added missing `.claude/agents/cai-triage.md` entry to DESCRIPTIONS array
- `CODEBASE_INDEX.md` — regenerated; cai-triage.md row now shows real description instead of TODO placeholder

### Decisions this revision
- Used reviewer-suggested description verbatim (matches updated agent frontmatter after DISMISS_* removal)

### New gaps / deferred
- None
