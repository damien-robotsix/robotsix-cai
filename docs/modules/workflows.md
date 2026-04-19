# workflows

GitHub Actions workflows that run in CI/CD. Handle Docker image
publication, PR-context cleanup, docs regeneration, and admin-only
label enforcement.

## Entry points
- `.github/workflows/admin-only-label.yml` — Restrict `auto-improve:requested` label to admins.
- `.github/workflows/cleanup-pr-context.yml` — Clean up PR context on close.
- `.github/workflows/docker-publish.yml` — Build and publish Docker image to Docker Hub.
- `.github/workflows/regenerate-docs.yml` — Regenerate `CODEBASE_INDEX.md` and `docs/fsm.md`, auto-commit drift.
