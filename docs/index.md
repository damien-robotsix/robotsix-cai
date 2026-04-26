---
title: Home
nav_order: 1
---

# robotsix-cai

A containerized [Claude Code](https://docs.claude.com/en/docs/claude-code/overview)
launcher that ships with:

- A GitHub App identity (`cai[bot]`) so commits and PRs aren't tied to
  your personal account.
- Built-in tools for round-tripping issues as JSON+MD pairs (`cai-issue`)
  and solving them with a deep agent graph (`cai-solve`).
- [Langfuse](https://langfuse.com) instrumentation on every pydantic-ai
  agent run so each `cai-solve` call is fully auditable.

## Install

```bash
wget https://raw.githubusercontent.com/damien-robotsix/robotsix-cai/main/install.sh
less install.sh    # review before running
bash install.sh
```

The installer creates a `docker-compose.yml` and `.env` in
`./robotsix-cai`, pulls the published image, and walks you through
Claude + GitHub auth.

`install.sh` ships only the **client**: a single `cai` container that
sends traces to a Langfuse server you run yourself. The server-side
walkthrough is in [Langfuse server setup](./langfuse-server.md).

## Pages

- [Langfuse server setup](./langfuse-server.md) — host Langfuse at
  `langfuse.your-domain` with Caddy + Let's Encrypt.
