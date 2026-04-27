# robotsix-cai

This repository provides an automated agent for solving GitHub issues using `cai-solve` upon triage.

## Configuration/Setup

The automated workflow relies on GitHub Actions to trigger the agent.
For developers self-hosting or referencing this setup, the following GitHub Repository Secrets must be configured:

*   `ANTHROPIC_API_KEY`: API key for accessing the Anthropic service.
*   `LANGFUSE_SECRET_KEY`: Secret key for Langfuse tracing.
*   `LANGFUSE_PUBLIC_KEY`: Public key for Langfuse tracing.
*   `LANGFUSE_BASE_URL`: Base URL of the Langfuse server instance.

Please see [docs/langfuse-server.md](docs/langfuse-server.md) for information on finding or generating the `LANGFUSE_*` credentials.
