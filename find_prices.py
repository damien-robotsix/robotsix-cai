import os

path = os.environ.get("VIRTUAL_ENV", sys.executable)
import sys
for p in sys.path:
    if "site-packages" in p:
        target = os.path.join(p, "genai_prices")
        if os.path.exists(target):
            import subprocess
            subprocess.run(["ls", "-l", target])
            subprocess.run(["cat", os.path.join(target, "prices.py")])
            break
