---
title: Home
---

# robotsix-cai

**Claude Auto Improve** — a self-tuning backend that analyzes its own
[Claude Code](https://docs.claude.com/en/docs/claude-code/overview)
runtime sessions and proposes improvements to itself via pull requests.

## Status

**Pre-alpha.** v0 (Lane 1 — self-improvement only) is under active
development. This documentation will grow as the project matures.

- [v0 development tracker](https://github.com/damien-robotsix/robotsix-cai/issues/1)
- [Architectural design RFC](https://github.com/damien-robotsix/claude-auto-tune-hub/issues/51)
- [GitHub repository](https://github.com/damien-robotsix/robotsix-cai)

## What it does

robotsix-cai runs as a long-lived service in a Docker container. On a
schedule, it:

1. Reads transcripts of its own recent Claude Code runtime sessions
2. Analyzes them with a Claude prompt to find bugs, inefficiencies, and
   prompt gaps in its own code
3. Files issues (and, where confident, opens pull requests) in this
   repository
4. After human review and merge, the deploy pipeline rolls out the
   improvement
5. The next run uses the improved code, closing the loop

This is **Lane 1** of a two-lane design described in the RFC. Lane 2
(analyzing other workspaces' Claude Code sessions) is deferred to a
later milestone.

## Quick start

See the
[README on GitHub](https://github.com/damien-robotsix/robotsix-cai#readme)
for the current setup instructions. As v0 stabilizes, those will move
into a dedicated section here.

## License

[MIT](https://github.com/damien-robotsix/robotsix-cai/blob/main/LICENSE)
