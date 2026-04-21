# workflows

GitHub Actions workflows that run in CI/CD. Handle Docker image
publication, PR-context cleanup, and admin-only label enforcement.
All three workflows live at the repo root under
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

## Inter-module dependencies
- Runs **installer** — `docker-publish.yml` consumes the
  `Dockerfile` and `docker-compose.yml`.
- Enforces **github-glue** semantics — `admin-only-label.yml`
  mirrors the admin gating that `is_admin_login` (in
  `cai_lib/config.py`) enforces in-process.
- No direct Python imports.

## Operational notes
- **Admin gating.** `admin-only-label.yml` is the only gate on
  `auto-improve:requested`; bypassing it would let any commenter
  push an issue straight into the implement pipeline.
- **Build blast radius.** `docker-publish.yml` failures block new
  releases but do not affect running workers until they pull a
  new image.
- **Cost sensitivity.** GitHub Actions minutes only —
  zero Claude cost.
- **Docs regeneration moved to FSM.** The former
  `regenerate-docs.yml` workflow was folded into the
  `PRState.REVIEWING_DOCS` FSM step (see
  `cai_lib/actions/review_docs.py` and
  [`docs/modules/actions.md`](actions.md)); generator drift,
  module-coverage checks, and agent-driven narrative fixes are
  all applied from a single cycle pass.
