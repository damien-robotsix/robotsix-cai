"""Debug: read the split section of test_loader.py"""
with open("tests/agents/test_loader.py") as f:
    content = f.read()
lines = content.splitlines()
for i in range(3284, min(3382, len(lines))):
    print(f"{i+1}:{lines[i]}")
