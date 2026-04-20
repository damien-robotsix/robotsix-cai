# workflows

GitHub Actions workflows that run in CI/CD. Handle Docker image
publication, PR-context cleanup, docs regeneration, and admin-only
label enforcement. All four workflows live at the repo root under
`.github/workflows/` and are triggered by GitHub events (push,
pull_request, issues, or manual dispatch).

## Key entry points
- [`.github/workflows/admin-only-label.yml`](../../.github/workflows/admin-only-label.yml)
  — restricts the `auto-improve:requested` label so only admins
  can apply it; non-admin attempts are automatically reverted.
- [`.github/workflows/cleanup-pr-context.yml`](../../.github/workflows/cleanup-pr-context.yml)
  — cleans up PR-associated context (branches, labels,
  worktree-marker comments) when a PR closes.
- [`.github/workflows/docker-publish.yml`](../../.github/workflows/docker-publish.yml)
  — builds and publishes the Docker image to Docker Hub on push
  to `main`.
- [`.github/workflows/regenerate-docs.yml`](../../.github/workflows/regenerate-docs.yml)
  — regenerates `CODEBASE_INDEX.md` and `docs/fsm.md` on every
  PR, and auto-commits the drift back onto the PR branch.
  Invokes `cai review-docs` to check for stale documentation,
  runs `scripts/check-modules-coverage.py` to verify module
  registry coverage, and retries the doc agent if coverage
  checking fails (to auto-fix stale `docs/modules.yaml` entries).
  Replaces the former `check-index.yml`.

## Inter-module dependencies
- Runs **scripts** — `regenerate-docs.yml` invokes
  `scripts/generate-index.sh`, `scripts/generate-fsm-docs.py`,
  and `scripts/check-modules-coverage.py` (module registry
  coverage verification).
- Runs **installer** — `docker-publish.yml` consumes the
  `Dockerfile` and `docker-compose.yml`.
- Enforces **github-glue** semantics — `admin-only-label.yml`
  mirrors the admin gating that `is_admin_login` (in
  `cai_lib/config.py`) enforces in-process.
- Touches **docs** — drifted `CODEBASE_INDEX.md` and
  `docs/fsm.md` are auto-committed.
- No direct Python imports.

## Operational notes
- **Auto-commit safety.** `regenerate-docs.yml` writes back to the
  PR branch when it detects drift; the committer identity is the
  GitHub Actions bot. PR authors can push further commits without
  conflict.
- **Admin gating.** `admin-only-label.yml` is the only gate on
  `auto-improve:requested`; bypassing it would let any commenter
  push an issue straight into the implement pipeline.
- **Build blast radius.** `docker-publish.yml` failures block new
  releases but do not affect running workers until they pull a
  new image.
- **Cost sensitivity.** GitHub Actions minutes only —
  zero Claude cost.
- **CI implications.** These workflows ARE the CI for the
  project; if a new generator is added, extend the
  `regenerate-docs.yml` "Regenerate" step rather than adding a
  new workflow.
