import os
import subprocess
import sys
import pydantic_ai

print("pydantic_ai version:", pydantic_ai.__version__)

os.environ["LANGFUSE_PUBLIC_KEY"] = "pk-lf-test"
os.environ["LANGFUSE_SECRET_KEY"] = "sk-lf-test"
os.environ["LANGFUSE_BASE_URL"] = "http://localhost:3000"

import asyncio
from src.cai.log.observability import setup_langfuse

setup_langfuse()
# mock cost to not fail if genai-prices is not full
from pydantic_ai import Agent

ag = Agent("anthropic/claude-sonnet-4-6")

async def test_run():
    # just create a mock result to pass to calculate_cost
    from pydantic_ai.result import RunResult, Usage
    
    usage = Usage(request_tokens=100, response_tokens=50)
    import dataclasses
    
    @dataclasses.dataclass
    class MockResult:
        def usage(self):
            return usage
            
    res = MockResult()
    
    from src.cai.log.observability import calculate_and_record_cost
    calculate_and_record_cost(ag, res)
    print("Done calling test")

asyncio.run(test_run())
