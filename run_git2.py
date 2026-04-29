import subprocess
import json

log = subprocess.run(["git", "log", "-n", "1", "--pretty=format:%B"], capture_output=True, text=True).stdout
print(json.dumps({"log": log}))
