import subprocess
import sys
r = subprocess.run([sys.executable, "explore.py"], capture_output=True, text=True)
print(r.stdout)
print(r.stderr)
