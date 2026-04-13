# PR Context Dossier
Refs: robotsix/robotsix-cai#510

## Files touched
- `cai.py:199` — added `_reconcile_interrupted` to the import from `cai_lib.cmd_lifecycle`

## Files read (not touched) that matter
- `cai_lib/cmd_lifecycle.py` — contains the full `_reconcile_interrupted` implementation (lines 150–265)

## Key symbols
- `_reconcile_interrupted` (`cai_lib/cmd_lifecycle.py`) — classifies interrupted job state; now importable from `cai.py`

## Design decisions
- Import-only change: the full implementation already existed in `cai_lib/cmd_lifecycle.py`; only the `cai.py` import was missing
- Preserved `# noqa: E402` comment to suppress flake8 module-level import warning

## Out of scope / known gaps
- `_reconcile_interrupted` is NOT yet hooked into `cmd_cycle` — that is Step 3 (#501)

## Invariants this change relies on
- `_reconcile_interrupted` is already exported from `cai_lib/__init__.py` and tested in `tests/test_reconcile.py`
