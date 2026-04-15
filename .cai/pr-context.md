# PR Context Dossier
Refs: robotsix-cai/robotsix-cai#679

## Files touched
- `cai_lib/actions/merge.py`:64 — replaced `40_000` literal with `int(os.environ.get("CAI_MERGE_MAX_DIFF_LEN", "40000"))`
- `cai_lib/actions/merge.py`:67-101 — added `_assemble_diff(raw_diff, max_len)` helper above `handle_merge`
- `cai_lib/actions/merge.py`:386 — replaced 4-line truncation block with `_assemble_diff(diff_result.stdout, _MERGE_MAX_DIFF_LEN)`
- `tests/test_merge_diff.py` — new file with 4 test cases for `_assemble_diff`

## Files read (not touched) that matter
- `cai_lib/actions/merge.py` — original truncation logic at lines 333-337; `os` already imported

## Key symbols
- `_assemble_diff` (`cai_lib/actions/merge.py`:67) — file-aware diff assembler; sorts test chunks first
- `_MERGE_MAX_DIFF_LEN` (`cai_lib/actions/merge.py`:64) — now env-var-driven via `CAI_MERGE_MAX_DIFF_LEN`

## Design decisions
- Split on `^diff --git ` (MULTILINE regex) to get per-file chunks; reconstruct with the prefix
- Test detection via `tests/` in path OR `test_\w+\.py` filename pattern
- Preserve relative ordering within test-group and non-test-group
- Stop at first chunk that won't fit (greedy, not optimal packing — simpler and predictable)
- Append single omission note listing dropped filenames so agent knows diff is incomplete
- Rejected: byte-level truncation (blind to file boundaries) — this is what the old code did

## Out of scope / known gaps
- Does not update the cai-merge agent prompt to explicitly mention selective inclusion
- Does not fetch per-file diffs via `gh api` (option 2 from the issue)
- Packing is greedy: a later small file that could fit after skipping a large file is not included

## Invariants this change relies on
- `gh pr diff` output uses standard unified-diff format with `diff --git a/… b/…` headers
- `os` is already imported in `merge.py`
- The preamble (text before first `diff --git`) is typically empty for `gh pr diff` output
