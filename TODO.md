# TODO

- [ ] **Refresh policy when reusing an existing `cai-solve` workspace.**
  `prepare_workspace` in `src/cai/github/repo.py` is idempotent: if
  ``/tmp/cai-solve/<owner>/<name>/<number>/`` already exists, the clone
  and issue files are kept verbatim. That's the right default while we
  iterate, but a long-lived workspace will drift (clone falls behind
  upstream, branch state from a prior run lingers, issue body on disk
  diverges from GitHub). Decide a refresh strategy — e.g. ``git fetch``
  + reset to the default branch on entry, re-pull the issue if remote
  is newer, or expose a ``--fresh`` flag — and wire it in.

- [ ] **Track real $ cost per run, not just tokens.** Token counts in
  Langfuse/`pydantic-deep` `cost_tracking` only approximate spend —
  per-model pricing varies and OpenRouter applies its own markup.
  Investigate whether OpenRouter exposes per-call cost (e.g. via the
  `usage` field on the response, or a follow-up `/api/v1/generation`
  lookup by id) and surface it on the agent run / Langfuse trace.

- [ ] **Enforce the `cai-solve` body template.** The persona in
  `src/cai/agents/refine.md` prescribes a strict structure
  (`## Refined Issue` with `### Description` / `### Plan` /
  `### Verification` / `### Scope guardrails` / `### Files to change`),
  but the deep agent freely picks its own headings (Summary / Desired
  behavior / Suggested shape / Out of scope) — content is fine but
  shape drifts. Either tighten the persona language, post-validate the
  refined body against expected headings (and re-prompt on miss), or
  adopt a more flexible template if the freer structure is actually
  preferable.
