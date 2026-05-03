> **Anti-pattern examples:**
> - **BAD:** `execute('git log')` or `bash('ls')` — you do not have these tools.
> - **GOOD:** use `read_file`, `grep`, `glob`, or `ls` to discover what changed.
