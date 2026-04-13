# PR Context Dossier
Refs: robotsix/robotsix-cai#554

## Files touched
- `pyproject.toml` — new file: ruff config (line-length=120, select E+F, ignore E501/F403/F405)
- `tests/test_lint.py` — new file: unittest that runs `ruff check .` and asserts exit 0; skips when ruff not installed
- `cai.py:181` — added `# noqa: F401` for `log_cost` unused import
- `cai.py:1731-1732` — removed spurious `f` prefix from two f-strings without placeholders
- `cai.py:2432` — removed spurious `f` prefix from `f"unexpected_error"`
- `cai.py:3336` — removed spurious `f` prefix from `f"## Original issue\n\n"`
- `cai.py:5977,5981,5984,6251,6255,6258` — removed spurious `f` prefix from 6 literal f-strings in two PR metadata blocks
- `cai.py:3677,3681,6440,6604,8225` — added `# noqa: E741` on lines using `l` as iterator var
- `cai.py:6901` — removed unused `reasoning = verdict["reasoning"]` assignment (F841)
- `cai.py:8277` — changed `except Exception as exc:` to `except Exception:` (F841, exc never used)
- `cai_lib/github.py:121` — added `# noqa: E741` for `l` iterator variable
- `tests/test_publish.py:10` — added `# noqa: F401` for `VALID_CATEGORIES` unused import

## Files read (not touched) that matter
- `cai.py` — scanned for F541/F401/F841/E741 violations
- `cai_lib/github.py` — E741 violation at line 121
- `tests/test_publish.py` — F401 violation at line 10

## Key symbols
- `TestLint.test_lint_passes` (tests/test_lint.py:12) — the new lint gate test

## Design decisions
- F403/F405 ignored globally — cai.py uses `from X import *` extensively (~540 usage sites), per-line suppression is impractical
- E501 ignored globally — issue explicitly says "don't be picky on line length"
- E741 suppressed with `# noqa` rather than renaming `l` — minimal change, renaming would affect readability of set/list comprehensions

## Out of scope / known gaps
- ruff not added to Dockerfile/image — issue says this is a deployment concern handled separately
- No CI/CD workflow changes — no workflows exist in this repo

## Invariants this change relies on
- `python -m unittest discover -s tests -v` already discovers `test_lint.py` automatically via `tests/__init__.py`
- ruff skips gracefully when not installed (`@unittest.skipUnless(shutil.which("ruff"), ...)`)
