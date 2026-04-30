import subprocess
import os

with open("git_output.txt", "w") as f:
    subprocess.run(["git", "status"], stdout=f, stderr=f)
    f.write("\n---\n")
    subprocess.run(["git", "log", "-n", "3"], stdout=f, stderr=f)
    f.write("\n---\n")
    subprocess.run(["git", "diff"], stdout=f, stderr=f)
