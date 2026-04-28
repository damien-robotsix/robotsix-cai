import subprocess
import sys
r = subprocess.run([sys.executable, "test_obs2.py"], capture_output=True, text=True)
print(r.stdout)
print(r.stderr)
