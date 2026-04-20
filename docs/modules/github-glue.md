# github-glue

GitHub glue layer — `gh` CLI wrappers, issue/PR publishing with
fingerprint dedup backed by a semantic pre-publish `cai-dup-check`
call, the pre-triage duplicate / already-resolved check, and the
issue-model helpers shared by handlers.

## Entry points
- `cai_lib/github.py` — `gh` CLI helpers and label utilities.
- `cai_lib/publish.py` — GitHub issue publisher. Combines fingerprint
  dedup with a semantic `cai-dup-check` pre-publish call so duplicates
  raised by different agents (e.g. `cai-rescue` prevention findings)
  are skipped before an issue is created. Set
  `CAI_SKIP_DUPCHECK_ON_PUBLISH=1` to disable the semantic check.
- `cai_lib/issues.py` — Issue-model / lifecycle helpers.
- `cai_lib/dup_check.py` — Pre-triage and pre-publish duplicate /
  already-resolved check. Exposes `check_duplicate_or_resolved(issue)`
  for raised issues and `check_finding_duplicate(title, body, labels)`
  for findings staged by `publish.py`.
