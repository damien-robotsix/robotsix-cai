# PR Context Dossier
Refs: robotsix/robotsix-cai#644

## Files touched
- cai.py:1825 — added `CLAUDEMD_STAGING_REL = Path(".cai-staging") / "claudemd"` constant
- cai.py:1839-1840 — `_setup_agent_edit_staging`: mkdir claudemd staging subdir
- cai.py:1926-1957 — `_apply_agent_edit_staging`: insert CLAUDE.md rglob block between plugin block and cleanup
- cai.py:2060-2094 — `_work_directory_block`: appended CLAUDE.md staging section before closing paren
- .claude/agents/cai-implement.md — updated heading + added CLAUDE.md bullet + updated rules heading + added CLAUDE.md example (via staging)
- .claude/agents/cai-revise.md — updated heading + added CLAUDE.md bullet + updated rules line (via staging)
- .claude/agents/cai-fix-ci.md — updated heading + added CLAUDE.md bullet + updated rules line (via staging)

## Files read (not touched) that matter
- cai.py:1841-1936 — `_apply_agent_edit_staging` full body to understand existing plugin block pattern

## Key symbols
- `CLAUDEMD_STAGING_REL` (cai.py:1825) — new constant for claudemd staging subdir path
- `_setup_agent_edit_staging` (cai.py:1827) — extended to mkdir claudemd dir
- `_apply_agent_edit_staging` (cai.py:1841) — extended with rglob-based CLAUDE.md copy block
- `_work_directory_block` (cai.py:1944) — extended to document CLAUDE.md staging path

## Design decisions
- Used `rglob("CLAUDE.md")` instead of `shutil.copytree` — only files literally named CLAUDE.md are copied, preventing accidental overwrite of unrelated work_dir files
- Inserted CLAUDE.md block between plugin block and cleanup (not after cleanup) — same ordering pattern as agents → plugins → claudemd → cleanup
- Early-return on first OSError — consistent with plugin block behavior, preserves staging tree for inspection

## Out of scope / known gaps
- Did not update `_apply_agent_edit_staging` docstring to mention CLAUDE.md
- Did not add CLAUDE.md staging docs to read-only agents (cai-refine, cai-analyze, cai-plan, cai-select, etc.)
- Did not change the existing shutil.rmtree cleanup block — it already covers claudemd/ by removing whole .cai-staging/ root

## Invariants this change relies on
- shutil.rmtree on `.cai-staging/` root cleans up claudemd/ automatically
- Wrapper's `git add -A` picks up uncommitted changes including staged agent files after wrapper copies them
