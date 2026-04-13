# PR Context Dossier
Refs: robotsix-cai/robotsix-cai#492

## Files touched
- docs/cli.md:1 — new file documenting all 22 cai subcommands with descriptions and argument tables
- docs/configuration.md:1 — new file documenting environment variables, settings.json structure, and runtime paths
- docs/architecture.md:1 — new file documenting the pipeline lifecycle, labels, cycle command flow, and agent modes
- docs/agents.md:1 — new file with a table of all 21 agents (name, description, tools, model, mode)

## Files read (not touched) that matter
- cai.py — argparse setup (lines 8472–8596) for subcommand names/args; cmd_* docstrings for descriptions
- .claude/agents/*.md — frontmatter (tools, model, description) for all 21 agents

## Key symbols
- `cmd_*` (cai.py:717–8464) — one function per subcommand; docstrings used for docs/cli.md descriptions
- `main()` / `sub.add_parser` (cai.py:8471–8596) — arg definitions used for cli.md argument tables
- `CAI_MERGE_CONFIDENCE_THRESHOLD` (cai.py:6501) — env var documented in configuration.md
- `ANTHROPIC_API_KEY` (cai.py:689) — env var documented in configuration.md

## Design decisions
- Alphabetical ordering for cli.md subcommands — easier for humans and cai-review-docs to cross-reference
- Alphabetical ordering for agents.md table — matches Plan 1 selection rationale
- Used frontmatter `model:` values verbatim (e.g. `claude-sonnet-4-6` → displayed as `sonnet`)
- Kept descriptions concise to stay under ~150 line budget per file

## Out of scope / known gaps
- No existing files were modified
- Did not document internal helper functions or constants in cai.py
- `cai-cai-confirm` model listed as `sonnet` per frontmatter (not `opus`)

## Invariants this change relies on
- The docs/ directory is new — no existing docs were overwritten
- Subcommand names in cli.md use hyphenated CLI form (cost-report), not Python underscore form

## Revision 1 (2026-04-13)

### Rebase
- clean

### Files touched this revision
- docs/agents.md:15 — changed cai-git mode from "Git ops" to "Worktree" to match cai.py:186 classification

### Decisions this revision
- Used "Worktree" rather than defining a new "Git ops" mode — cai.py explicitly classifies cai-git as a cloned-worktree agent; "Worktree" accurately reflects that it runs git commands in a clone via Bash

### New gaps / deferred
- none

## Revision 2 (2026-04-13)

### Rebase
- clean

### Files touched this revision
- docs/agents.md:10 — cai-code-audit mode Read-only → Worktree
- docs/agents.md:13 — cai-explore mode Worktree → Clone (new mode)
- docs/agents.md:17-19 — cai-plan, cai-propose, cai-propose-review mode Read-only → Worktree
- docs/agents.md:22-23 — cai-review-docs, cai-review-pr mode Read-only → Worktree
- docs/agents.md:25 — cai-select mode Inline-only → Worktree
- docs/agents.md:26 — cai-spike mode Worktree → Clone
- docs/agents.md:27 — cai-update-check mode Read-only → Worktree
- docs/agents.md:29 — footnote expanded to describe Worktree (code-editing vs. review/planning sub-types), Clone, and Read-only modes
- docs/architecture.md:50-62 — Worktree agents section rewritten: full list per cai.py:186, split into code-editing vs. review/planning sub-types; added Clone agents subsection for cai-explore/cai-spike; updated Read-only agents list

### Decisions this revision
- Introduced "Clone" as a 4th mode for cai-explore and cai-spike — per cai.py cmd_explore/cmd_spike, these agents clone the repo via --add-dir and post outcomes directly to GitHub issues (no branch, no PR); using "Worktree" was misleading because the architecture.md Worktree description said "opens a PR"
- Worktree agents list in architecture.md now matches cai.py:186 exactly: cai-fix, cai-revise, cai-rebase, cai-review-pr, cai-review-docs, cai-code-audit, cai-propose, cai-propose-review, cai-update-check, cai-plan, cai-select, cai-git
- cai-merge added to Read-only list in architecture.md (it was previously in agents.md as Inline-only; the architecture.md Read-only description "receive context in prompt" covers inline-only behavior accurately)

### New gaps / deferred
- none

## Revision 3 (2026-04-13)

### Rebase
- clean

### Files touched this revision
- none

### Decisions this revision
- Review comment (cross_cutting_ref: cai-revise missing from worktree list) was already resolved in Revision 2 — cai-revise is present at docs/architecture.md:52 in the current branch. Comment was filed against an earlier commit (9e5c55a7b3566f3b7a6d5596008dfc5c178619d3) that predated Revision 2.

### New gaps / deferred
- none
