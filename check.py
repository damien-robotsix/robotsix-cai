import sys
with open("src/cai/agents/implement.md", "r") as f:
    text = f.read()

if r"\`grep\`" in text:
    print("BACKSLASHES IN FILE", file=sys.stderr)
