# PR Context Dossier
Refs: robotsix/robotsix-cai#675

## Files touched
- `scripts/generate-index.sh`:29 — removed `.cai/pr-context.md` entry from DESCRIPTIONS array
- `scripts/generate-index.sh`:121 — added `grep -v '^\.cai/'` filter to the `git ls-files` pipeline
- `CODEBASE_INDEX.md`:8 — removed stale `.cai/pr-context.md` row

## Files read (not touched) that matter
- `CODEBASE_INDEX.md` — confirmed stale entry existed at line 8

## Key symbols
- `DESCRIPTIONS` (`scripts/generate-index.sh`:14) — associative array mapping filenames to descriptions; `.cai/pr-context.md` entry removed
- `git ls-files` pipeline (`scripts/generate-index.sh`:121) — now filters `.cai/` paths before generating index rows

## Design decisions
- Used `grep -v '^\.cai/'` in the pipeline rather than a gitignore-style list — the generator has no existing exclusion list, and a pipe filter is the minimal, targeted change
- Removed only the `.cai/pr-context.md` DESCRIPTIONS entry; did not add a `.cai/` catch-all comment to the array (out of scope)

## Out of scope / known gaps
- Did not add `.cai/` to `.gitignore` — the issue only asks for generator-side filtering
- Did not modify any agent prompts or workflow files

## Invariants this change relies on
- `.cai/` paths are never legitimate source files that should appear in CODEBASE_INDEX.md
- The generator uses `git ls-files` (not `find`) so the `grep -v` filter covers all tracked files
