# PR Context Dossier
Refs: robotsix/robotsix-cai#565

## Files touched
- `publish.py:101` — inserted four new 3-tuple label entries after `needs-human-review`
- `cai.py:6497` — added `LABEL_PR_EDITED`, `LABEL_PR_REVIEWED_REJECT`, `LABEL_PR_REVIEWED_ACCEPT`, `LABEL_PR_DOCUMENTED`, `PR_PIPELINE_LABELS` tuple, and `_pr_set_pipeline_state` function after `_pr_set_needs_human`

## Files read (not touched) that matter
- `cai.py:6477-6496` — `_pr_set_needs_human` used as structural template for `_pr_set_pipeline_state`

## Key symbols
- `LABEL_PR_NEEDS_HUMAN` (`cai.py`) — existing constant; new constants follow the same naming pattern
- `_pr_set_needs_human` (`cai.py:6477`) — template for the new `_pr_set_pipeline_state` helper
- `PR_PIPELINE_LABELS` (`cai.py`) — tuple of all four new pipeline-state label names; named public (no underscore) so it's importable by future steps
- `_pr_set_pipeline_state` (`cai.py`) — removes all pipeline labels then adds exactly one; failures logged not raised

## Design decisions
- Constants placed after `_pr_set_needs_human` in `cai.py` (not in `cai_lib/config.py`) — avoids star-import/underscore visibility issue with `cai_lib/__init__.py.__all__`
- `PR_PIPELINE_LABELS` named public (no leading underscore) — importable by future steps without friction
- Remove-label loop does not check `returncode` — matches pattern in `_pr_set_needs_human`; non-zero just means label wasn't present

## Out of scope / known gaps
- `cmd_revise`, `cmd_review_pr`, `cmd_review_docs`, `cmd_merge`, `_pr_label_sweep` not touched — Step 2/3 of #557
- No callers of `_pr_set_pipeline_state` yet — will be wired up in subsequent steps

## Invariants this change relies on
- `publish.py` `LABELS` list uses 3-tuples `(name, color, description)` — new entries follow same format
- `_run()` with `capture_output=True` is available at module level in `cai.py`
- `REPO` constant is available at module level in `cai.py`
