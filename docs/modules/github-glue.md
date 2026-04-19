# github-glue

GitHub glue layer — `gh` CLI wrappers, issue/PR publishing with
fingerprint dedup, the duplicate / already-resolved pre-triage check,
and the issue-model helpers shared by handlers.

## Entry points
- `cai_lib/github.py` — `gh` CLI helpers and label utilities.
- `cai_lib/publish.py` — GitHub issue publisher with fingerprint dedup.
- `cai_lib/issues.py` — Issue-model / lifecycle helpers.
- `cai_lib/dup_check.py` — Pre-triage duplicate / already-resolved check.
