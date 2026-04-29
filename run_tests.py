import subprocess
import sys
result = subprocess.run(["pytest", "-vv"], capture_output=True, text=True)
print(result.stdout)
print(result.stderr)
