# robotsix-cai

This repository provides tools and agents built using the CAI framework. It includes an issue management GitHub Action that automatically runs the `cai-solve` agent when an issue receives the `cai:raised` label.

## Configuration/Setup

To enable the `cai-solve` GitHub Actions workflow to run properly, you must configure the following GitHub Repository Secrets:

- `ANTHROPIC_API_KEY`: Your Anthropic API key to authenticate the agent's LLM calls.
- `LANGFUSE_SECRET_KEY`: The secret key for your Langfuse project.
- `LANGFUSE_PUBLIC_KEY`: The public key for your Langfuse project.
- `LANGFUSE_HOST`: The host URL of your Langfuse instance.

For instructions on how to set up a self-hosted Langfuse server and fetch the required `LANGFUSE_*` credentials, please refer to [docs/langfuse-server.md](docs/langfuse-server.md).
