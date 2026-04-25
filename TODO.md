# TODO

- [ ] **Make API vs. Plan call selectable.** `cai-refine` (and any future
  agent loaded via `cai.agents.loader`) currently hardcodes routing
  through the Claude Code CLI via `ClaudeCodeModel` — this consumes the
  user's Pro/Max subscription quota. Users with an `ANTHROPIC_API_KEY`
  will sometimes want to bypass the CLI and pay per token via
  pydantic-ai's `AnthropicProvider`. Plumb this as a per-agent
  frontmatter knob (e.g. `provider: claude_code | api`), an env var
  (`CAI_AGENT_PROVIDER`), or both.

- [ ] **Raise the `ClaudeCodeModel` default timeout.** The library
  defaults to 30s, which trips on any deep-agent run that does several
  tool calls. Either bump the default in `cai.agents.loader.build_model`
  or expose `timeout` as a frontmatter knob per agent.

- [ ] **Keep model IDs current.** `src/cai/agents/loader.py` hardcodes
  `_MODEL_IDS` mapping short names (`opus`, `sonnet`, `haiku`) to concrete
  Anthropic model IDs. This must be updated manually whenever Anthropic
  releases new versions. Consider fetching the live model list from the
  Anthropic models API (`GET /v1/models`) and resolving "latest
  opus/sonnet/haiku" dynamically so the mapping never goes stale.

- [ ] **Enforce the `cai-refine` body template.** The persona in
  `src/cai/agents/cai-refine.md` prescribes a strict structure
  (`## Refined Issue` with `### Description` / `### Plan` /
  `### Verification` / `### Scope guardrails` / `### Files to change`),
  but the deep agent freely picks its own headings (Summary / Desired
  behavior / Suggested shape / Out of scope) — content is fine but
  shape drifts. Either tighten the persona language, post-validate the
  refined body against expected headings (and re-prompt on miss), or
  adopt a more flexible template if the freer structure is actually
  preferable.
