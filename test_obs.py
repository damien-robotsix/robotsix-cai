import asyncio
from pydantic_ai import Agent

print("Testing observability cost tracing")
import os
os.environ["LANGFUSE_PUBLIC_KEY"] = "pk-lf-test"
os.environ["LANGFUSE_SECRET_KEY"] = "sk-lf-test"
os.environ["LANGFUSE_BASE_URL"] = "http://localhost:3000"

from src.cai.log.observability import setup_langfuse
setup_langfuse()

agent = Agent("anthropic/claude-sonnet-4-6")

async def main():
    try:
        r = await agent.run("hello")
        print(r)
    except Exception as e:
        print("Run error:", e)

    try:
        agent.run_sync("hello")
    except Exception as e:
        print("Run sync error:", e)

asyncio.run(main())
