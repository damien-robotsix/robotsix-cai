# PR Context Dossier
Refs: robotsix-cai#445

## Files touched
- .claude/agents/cai-revise.md (via staging) — added "Delegate bulk reading to a haiku Explore subagent" section; updated "Addressing review comments" step 2 to use Explore

## Files read (not touched) that matter
- .claude/agents/cai-revise.md — full file read to understand structure and identify insertion points

## Key symbols
- `## Delegate bulk reading to a haiku Explore subagent` (cai-revise.md) — new section inserted before "## Addressing review comments"
- `## Addressing review comments` step 2 (cai-revise.md) — changed from "Read the referenced file(s)" to "Gather context via Explore"

## Design decisions
- New section placed between "Read the PR context dossier first" and "Addressing review comments" — logical flow: dossier → bulk gather → targeted edits
- Fallback clause added for small lookups (< 3 files, known paths, < 100 lines) — avoids subagent overhead for trivial single-file reads
- Explicit note distinguishing Explore (read/search) from cai-git (git ops) to avoid confusion
- Dossier-already-read note included — avoids redundant re-summarisation within the same session

## Out of scope / known gaps
- No changes to cai-fix.md, cai-git.md, or any other agent files
- No wrapper (cai.py) changes
- No new agent files created

## Invariants this change relies on
- cai-revise already has `Agent` in its tools list (frontmatter line 4) — no frontmatter changes needed
- Explore subagent with model="haiku" is a valid Agent call in this system
