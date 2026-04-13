# PR Context Dossier
Refs: damien-robotsix/robotsix-cai#499

## Files touched
- docs/architecture.md — new file: pipeline overview, issue lifecycle, supporting processes, orchestration
- docs/cli.md — new file: subcommands grouped by category (core pipeline, issue processing, maintenance, utilities)
- docs/agents.md — new file: all 21 agents grouped by role with model, access level, and purpose
- docs/configuration.md — new file: environment variables and Docker volumes

## Files read (not touched) that matter
- docs/index.md — existing home page; not modified (just-the-docs builds sidebar from nav_order automatically)
- docs/_config.yml — confirmed just-the-docs theme is in use; nav_order frontmatter drives sidebar
- cai.py:8472-8562 — argparse subcommand definitions used as source of truth for cli.md descriptions
- .claude/agents/*.md — frontmatter of all 21 agents used as source of truth for agents.md

## Key symbols
- `nav_order` (docs/*.md frontmatter) — controls sidebar ordering in just-the-docs; required for visibility
- `sub.add_parser` (cai.py:8475-8535) — authoritative subcommand help strings

## Design decisions
- No changes to index.md — just-the-docs sidebar is theme-driven via nav_order, not a hand-maintained link list
- architecture.md gets nav_order:1, cli.md nav_order:2, agents.md nav_order:3, configuration.md nav_order:4
- cai-cost-optimize classified as read-only (tools: Read, Grep, Glob only — verified from frontmatter)
- Agents table includes all 21 agents found in .claude/agents/

## Out of scope / known gaps
- index.md not touched per plan; already has title:Home which places it first in sidebar
- No exhaustive API-level reference; stubs with accurate summaries only
- cai-propose-review is in review group (not maintenance) since it gates creative proposals before they enter pipeline

## Invariants this change relies on
- just-the-docs theme renders nav_order as sidebar position; pages without nav_order may be hidden
- Subcommand descriptions sourced from cai.py argparse help strings — will drift if cai.py is updated
