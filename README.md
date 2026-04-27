# robotsix-cai

## Configuration/Setup

To automatically execute the `cai-solve` autonomous agent via GitHub Actions, the following GitHub Repository Secrets must be configured in your project repo settings:

*   `ANTHROPIC_API_KEY`: Your Anthropic API key to allow `cai-solve` to use Claude models.
*   `LANGFUSE_SECRET_KEY`: Tracing configuration detail.
*   `LANGFUSE_PUBLIC_KEY`: Tracing configuration detail.
*   `LANGFUSE_HOST`: Tracing configuration detail.

See `docs/langfuse-server.md` for more information on fetching the `LANGFUSE_*` credentials.

Once these secrets are present, any issue labeled with `cai:raised` will automatically trigger the `cai-solve` workflow.
