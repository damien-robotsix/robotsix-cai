# Design decisions

This file is a curated, durable record of design decisions that have
been made for `robotsix-cai`. Subagents that decide what to raise or
fix (`analyze`, `audit`, `fix`) read this file before making any
raise/skip/fix decision. Entries override and supersede any signal
from parsed transcripts: if a finding overlaps with an entry below,
do **not** raise or act on it.

The format for each entry:

- **Decision:** the rule, stated in one or two sentences.
- **Why:** the reasoning, including any prior incident or signal
  this rule was created in response to.
- **Related:** links to issues / PRs that produced or reinforce this
  decision.
- **Exit condition:** what would have to change for this rule to be
  worth revisiting. Without this, decisions calcify forever.

If a finding seems to contradict an entry below, you have two
choices:

1. **Skip the finding** (default). Output `No findings.` for that
   pattern.
2. **Raise a meta-issue** that explicitly names the design-decision
   entry, presents the new evidence, and argues the exit condition
   has been met. Do not silently re-raise the original finding.

---

## Do not inline file contents into `-p` prompts to "reduce exploration"

**Decision:** Reject any finding that proposes adding file paths,
file contents, or a project layout manifest to subagent prompts on
the basis of "low input tokens", "blind exploration", or
"prompt-quality" evidence derived from the parsed transcript signals.

**Why:** The "low input tokens" / "tools-per-input-token" signal
comes from a `parse.py` token-counting bug — the parser omits the
system prompt and most assistant message contributions, so the
reported input-token count for a session can be 1-2 orders of
magnitude lower than the prompt the subagent actually received.
Numbers like "97 input tokens across 41 tool calls" or "141 input
tokens across 51 tool calls" are mathematically incompatible with
the multi-thousand-token system prompts and issue bodies that the
fix subagent receives as its first message.

The analyzer's interpretation ("the prompt has insufficient
context") is downstream of the parser bug, not a real code issue.
The proposed remediation (inline more file paths or content into
prompts) would bloat every fix-subagent invocation without
addressing the underlying parser bug, and the analyzer would still
see the same wrong signal on the next run.

**Related:** Closed damien-robotsix/robotsix-cai#23,
damien-robotsix/robotsix-cai#141. Spin-loop audit
damien-robotsix/robotsix-cai#254 (3 PRs rejected in 25 minutes
trying to act on the phantom finding). Tracking
damien-robotsix/robotsix-cai#260.

**Exit condition:** Revisit only if `parse.py`'s token counting is
fixed (specifically: it correctly accumulates input from system
prompts and assistant messages, not just user messages) AND the
same "low input tokens / high output tokens" pattern still appears
in the parsed signals. Until both conditions hold, this entry
applies.
