import os
with open(".git/logs/HEAD", "r") as f:
    lines = f.readlines()
for line in lines[-5:]:
    print(line)
