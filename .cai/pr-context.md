# PR Context Dossier
Refs: robotsix-cai/robotsix-cai#485

## Files touched
- `CODEBASE_INDEX.md` — new static file-level index table (one row per tracked file)
- `scripts/generate-index.sh` — new generator script with associative array of descriptions
- `.github/workflows/check-index.yml` — new CI workflow that fails PRs if index is stale
- `CLAUDE.md`:6 — inserted item 0 pointing agents to CODEBASE_INDEX.md before Glob/Grep

## Files read (not touched) that matter
- `CLAUDE.md` — needed to find exact insertion point for item 0

## Key symbols
- `DESCRIPTIONS` (`scripts/generate-index.sh`) — associative array mapping file paths to description strings; single source of truth

## Design decisions
- Descriptions live in the generator script (not the index file) so `CODEBASE_INDEX.md` is always auto-generated and never manually edited
- CI uses `git diff --exit-code` after regeneration to detect drift — simple and dependency-free
- Item 0 in CLAUDE.md (before Grep-before-Read) reflects that index lookup is faster than any grep

## Out of scope / known gaps
- Generator script is not marked executable via `git update-index` (Bash not available); CI calls it via `bash scripts/generate-index.sh` explicitly so this is not a blocker
- Index only covers files tracked at PR time; files added directly to main without a PR won't trigger the CI check until the next PR

## Invariants this change relies on
- `git ls-files` in the generator is run from repo root so paths are relative and match DESCRIPTIONS keys exactly
- CODEBASE_INDEX.md must include the three new files (itself, the generator, and the CI workflow) — verified by inspection
