import traceback
try:
    with open("src/cai/agents/implement.md", "rb") as f:
        print(f.read()[-300:])
except Exception as e:
    traceback.print_exc()
