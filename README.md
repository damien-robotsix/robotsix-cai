# robotsix-cai

`robotsix-cai` is an autonomous AI agent capable of pulling GitHub issues, refining their requirements, implementing the code fixes, and opening pull requests automatically. Its execution runs as a state machine flow, and it uses Langfuse for tracing agent thought processes and actions.

## Configuration/Setup

To automatically trigger the agent when an issue is labeled with `cai:raised`, a GitHub Actions workflow is provided. 

For the workflow to function properly, you need to configure the following GitHub Repository Secrets in your repository settings:

- \`ANTHROPIC_API_KEY\`: Required to interact with Claude models via Anthropic's API or OpenRouter.
- \`LANGFUSE_SECRET_KEY\`: The secret key for your Langfuse project to enable observational tracing.
- \`LANGFUSE_PUBLIC_KEY\`: The public key for your Langfuse project.
- \`LANGFUSE_BASE_URL\`: The base URL pointing to your Langfuse instance.

### Setting up Langfuse credentials

To deploy a self-hosted instance of Langfuse and obtain the required \`LANGFUSE_*\` credentials (project keys and base URL), please refer to the detailed instructions in [docs/langfuse-server.md](docs/langfuse-server.md).
