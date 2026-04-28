import sys
with open("test_obs_full.py", "w") as f:
    f.write('''
import os
import pydantic_ai
os.environ["LANGFUSE_PUBLIC_KEY"] = "pk-lf-test"
os.environ["LANGFUSE_SECRET_KEY"] = "sk-lf-test"
os.environ["LANGFUSE_BASE_URL"] = "http://localhost:3000"

from src.cai.log.observability import setup_langfuse
setup_langfuse()

from pydantic_ai import Agent

print("Agent is instrumented:", getattr(Agent, "_is_wrapped_run", False))
# mock everything out
''')
