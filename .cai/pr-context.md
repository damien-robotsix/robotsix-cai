# PR Context Dossier
Refs: robotsix/robotsix-cai#579

## Files touched
- `cai.py:2519` — updated comment on `_REVISE_ISSUE_BODY_MAX_CHARS` to note it's now the fallback for old-format issues
- `cai.py:2521` — added `_REVISE_CONTEXT_HEADINGS` tuple constant
- `cai.py:2530` — added `_extract_revise_context()` function (splits on `\n### `, extracts relevant sections, falls back to char truncation)
- `cai.py:3580` — replaced 6-line truncation block with single call to `_extract_revise_context(_issue_body_raw)`
- `.claude/agents/cai-refine.md` (via staging) — renamed `### Problem` → `### Description` and `### Files likely to touch` → `### Files to change` in output template and Multi-Step Decomposition block
- `.claude/agents/cai-plan.md` (via staging) — appended `### Scope guardrails` section to Output format template

## Files read (not touched) that matter
- `cai.py:3570-3597` — `cmd_revise` context around the truncation block; `_issue_num` is still used on line 3593

## Key symbols
- `_REVISE_ISSUE_BODY_MAX_CHARS` (`cai.py:2519`) — kept; now used only as fallback truncation length in `_extract_revise_context`
- `_REVISE_CONTEXT_HEADINGS` (`cai.py:2521`) — new constant; ordered section heading aliases for extraction
- `_extract_revise_context` (`cai.py:2530`) — new function; replaces blunt char truncation with section-aware extraction

## Design decisions
- Kept `_extract_revise_context` in `cai.py` (not `cai_lib/github.py`) — single call site, co-located with the constant it supersedes
- Used `alias.lstrip("# ").lower()` for normalised key lookup — handles `### ` prefix in aliases cleanly
- Fallback to `body[:_REVISE_ISSUE_BODY_MAX_CHARS]` for old-format issues with no `\n### ` headings
- Rejected: moving function to shared library (Plan 1) — unnecessary for single call site

## Out of scope / known gaps
- Did NOT change early-exit detection in cai-refine.md (still checks for `### Problem`) — that scans incoming issue bodies, not refine's own output
- Did NOT touch `_build_implement_user_message` or `_build_issue_block` — implement agent gets full body
- Did NOT change cai-select, cai-merge, or any other agent

## Invariants this change relies on
- `_issue_num` is still assigned at line 3579 before the (now replaced) truncation block; line 3593 references it safely
- `body.split("\n### ")` avoids splitting on `###` inside code fences or inline text
- First element of `parts` (preamble) is intentionally discarded
