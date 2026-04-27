# robotsix-cai

This repository provides an automated agent for solving GitHub issues using `cai-solve` upon triage.

## Configuration/Setup

The automated workflow relies on GitHub Actions to trigger the agent.
For developers self-hosting or referencing this setup, the following GitHub Repository Secrets must be configured:

*   `OPENROUTER_API_KEY`: API key for accessing the OpenRouter service.
*   `LANGFUSE_SECRET_KEY`: Secret key for Langfuse tracing.
*   `LANGFUSE_PUBLIC_KEY`: Public key for Langfuse tracing.
*   `LANGFUSE_BASE_URL`: Base URL of the Langfuse server instance.
*   `CAI_GITHUB_APP_PEM`: GitHub App private key for pushing changes as cai[bot].
*   `CAI_APP_ENV`: Contents of the cai `app.env` file. Must contain `APP_ID=<numeric id>`.
*   `APPROVED_AI_USERS`: A comma, space, or newline-separated list of GitHub usernames that are authorized to trigger the `cai-solve` workflow by applying the `cai:raised` label. If the label is applied by a user not in this list, the workflow will abort.

Please see [docs/langfuse-server.md](docs/langfuse-server.md) for information on finding or generating the `LANGFUSE_*` credentials.
