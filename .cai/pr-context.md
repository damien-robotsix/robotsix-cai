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
