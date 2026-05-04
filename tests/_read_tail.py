"""Read the test file and print its last 30 lines."""
import sys
path = sys.argv[1]
with open(path) as f:
    lines = f.readlines()
print(f"Total lines: {len(lines)}")
for i, line in enumerate(lines[-30:]):
    print(f"{len(lines)-30+i+1}:{line}", end="")
