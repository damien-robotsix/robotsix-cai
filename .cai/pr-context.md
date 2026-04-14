# PR Context Dossier
Refs: robotsix-cai/robotsix-cai#583

## Files touched
- cai.py:1340 — added `_parse_oob_issues()` to extract `## Out-of-scope Issue` blocks from agent output
- cai.py:1377 — added `_create_oob_issues()` to create GitHub issues for out-of-scope findings
- cai.py:6411 — added call to `_parse_oob_issues`/`_create_oob_issues` in `cmd_review_pr`, strips blocks from `agent_output` before PR comment is built
- .cai-staging/agents/cai-review-pr.md — added `## Out-of-scope Issue` output format section and hard rule 8

## Files read (not touched) that matter
- .claude/agents/cai-review-pr.md — existing agent prompt; used as base for staging update

## Key symbols
- `_parse_oob_issues` (cai.py:1340) — parses `## Out-of-scope Issue` blocks, mirrors `_parse_suggested_issues`
- `_create_oob_issues` (cai.py:1377) — creates GitHub issues with `auto-improve,auto-improve:raised` labels, mirrors `_create_suggested_issues`
- `cmd_review_pr` (cai.py:6411) — call site; strips OOB blocks before posting PR comment

## Design decisions
- Placed new functions between `_parse_suggested_issues` and `_create_suggested_issues` for logical grouping
- OOB issues are stripped from `agent_output` before `has_findings` check and before logging, so they never appear in PR comments or review logs
- Used same label pair (`auto-improve`, `LABEL_RAISED`) as suggested issues so they enter the pipeline at `:raised`

## Out of scope / known gaps
- No deduplication/fingerprinting for OOB issues (deliberate — follow-up issue per plan)
- Only the first `cmd_review_pr` agent call site (line ~6406) handles OOB issues; secondary call sites at lines 6677 and 7337 were not modified (they appear to be for different review loops — verify if needed)

## Invariants this change relies on
- `LABEL_RAISED` is already defined and imported via `from cai_lib.config import *`
- `_parse_oob_issues` regex splits on `^## Out-of-scope Issue\s*$` (exact match with optional trailing whitespace), so partial matches in body text won't trigger false splits
