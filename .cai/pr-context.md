# PR Context Dossier
Refs: damien-robotsix/robotsix-cai#482

## Files touched
- docs/cli.md:1 — new CLI reference documenting all 23 subcommands with options
- docs/architecture.md:1 — new architecture overview: agent dispatch, label state machine, fix pipeline, persistence
- docs/configuration.md:1 — new configuration reference: env vars, auth modes, Docker volumes, log files

## Files read (not touched) that matter
- docs/index.md — existing Jekyll frontmatter pattern (`title:` only, no `layout:`)
- cai.py:8419-8510 — argparse subcommand definitions (all subcommands and options)
- cai.py:193-212 — label constants used in architecture.md state machine table
- cai.py:219-223 — log file paths used in architecture.md and configuration.md
- README.md — existing env var and volume documentation cross-checked for accuracy

## Key symbols
- `LABEL_*` constants (cai.py:193-212) — drove the label state table in architecture.md
- `main()` argparse block (cai.py:8419-8510) — drove all CLI subcommand entries
- `LOG_PATH`, `COST_LOG_PATH`, `OUTCOME_LOG_PATH`, `ACTIVE_JOB_PATH` (cai.py:219-223) — log file section

## Design decisions
- Used `nav_order: 2/3/4` in frontmatter to order sidebar after `Home` (nav_order 1 implied by index.md)
- Organised cli.md by functional groups (core pipeline, review & merge, etc.) rather than alphabetically
- configuration.md references index.md for volume details rather than repeating the full description
- Rejected: adding `_config.yml` — file does not exist in the repo, so not needed

## Out of scope / known gaps
- No changes to cai.py, agent definitions, or test files
- `_config.yml` absent from repo — GitHub Pages config not touched
- Internal/private functions not documented per scope guardrails

## Invariants this change relies on
- Jekyll `just-the-docs` theme renders `title:` frontmatter into sidebar nav automatically
- `cai-review-docs` bails out when `Glob("docs/**/*.md")` returns no files — now returns 4 files
