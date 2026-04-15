# PR Context Dossier
Refs: damien-robotsix/robotsix-cai#719

## Files touched
- `cai_lib/actions/revise.py:12-18` — removed `from datetime import datetime, timezone`; added `import json as _json`
- `cai_lib/actions/revise.py:34-44` — removed `_filter_unaddressed_comments` from cmd_helpers imports; removed `# noqa: F401` on `_is_bot_comment`
- `cai_lib/actions/revise.py:159-248` — added `_FILTER_JSON_SCHEMA`, `_FILTER_DIFF_MAX_CHARS`, and `_filter_comments_with_haiku()` function
- `cai_lib/actions/revise.py:527-532` — replaced commit_ts calculation + `_filter_unaddressed_comments` call with `_filter_comments_with_haiku`
- `docs/agents.md:9` — added `cai-comment-filter` row to agent table
- `tests/test_revise_filter.py` — new unit test file (9 tests) covering all filter scenarios
- `.cai-staging/agents/cai-comment-filter.md` — new haiku agent definition (staged for copy to `.claude/agents/`)

## Files read (not touched) that matter
- `cai_lib/cmd_helpers.py` — contains `_filter_unaddressed_comments` (kept; still used by merge.py and fix_ci.py); also `_select_revise_targets` (dead code, kept)
- `cai_lib/subprocess_utils.py` — `_run_claude_p` pattern used for the haiku call (same pattern as `plan.py` cai-select invocation)
- `cai_lib/actions/plan.py:177-224` — reference for `--json-schema` + `_run_claude_p` pattern

## Key symbols
- `_filter_comments_with_haiku` (`cai_lib/actions/revise.py:169`) — new function replacing the old timestamp filter; calls `gh pr diff` + cai-comment-filter haiku
- `_FILTER_JSON_SCHEMA` (`cai_lib/actions/revise.py:159`) — JSON schema for the haiku's output `{"unresolved": [{"id": "...", "reason": "..."}]}`
- `_FILTER_DIFF_MAX_CHARS` (`cai_lib/actions/revise.py:178`) — 20 000-char truncation limit for PR diffs
- `handle_revise` (`cai_lib/actions/revise.py:466`) — active FSM handler; calls `_filter_comments_with_haiku` at line 532
- `cai-comment-filter` agent — haiku, inline-only, no tools

## Design decisions
- Synthetic `_idx` integers as comment IDs — issue comments have a GitHub `id` but normalized review line-comments do not; indices are cheaper and unambiguous
- `gh pr diff` for the diff (not `git diff main...HEAD`) — avoids needing a clone; filtering happens before the clone in `handle_revise`
- Conservative fallback: on haiku failure, return all non-bot comments — better to over-process than silently drop a human request (the old bug)
- Rejected: timestamp-based fallback — the issue scope guardrail explicitly forbids it
- Rejected: deleting `_filter_unaddressed_comments` — still used by merge.py and fix_ci.py; would require a broader migration outside this issue's scope

## Out of scope / known gaps
- `cai_lib/cmd_helpers.py:666` — `_filter_unaddressed_comments` call in `_select_revise_targets`; this function is dead code (never called by the FSM dispatcher) but was not removed to keep the diff minimal
- `cai_lib/actions/merge.py:281` — still uses old timestamp filter; a follow-up issue should migrate it
- `cai_lib/actions/fix_ci.py:148` — still uses old timestamp filter; same follow-up

## Invariants this change relies on
- `_run_claude_p` rewrites `proc.stdout` to the agent's result text (not the raw JSON envelope)
- `gh pr diff <N> --repo <REPO>` returns a unified diff without needing a clone
- The `_idx` synthetic index is a 0-based integer matching the comment's position in `all_comments`; the zip in the return expression preserves this alignment
