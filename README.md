# robotsix-cai

robotsix-cai runs the `cai-solve` agent automatically when GitHub issues are raised, attempting to resolve bugs and implement requests via an autonomous agent driven by Claude 3.5 Sonnet.

## Configuration/Setup

To use the GitHub Actions workflow for automated issue resolution, you must configure the following **GitHub Repository Secrets**:

- `ANTHROPIC_API_KEY` – Your Anthropic API key to power the agent using Claude models.
- `LANGFUSE_SECRET_KEY` – The secret key for your Langfuse tracing project.
- `LANGFUSE_PUBLIC_KEY` – The public key for your Langfuse tracing project.
- `LANGFUSE_HOST` – The base URL corresponding to your Langfuse instance.

For complete instructions on setting up a self-hosted Langfuse server and generating the required `LANGFUSE_*` credentials, please refer to [./docs/langfuse-server.md](./docs/langfuse-server.md).
