"""cai_lib.cmd_implement — helpers for the implement-subagent pipeline."""

import re


def _parse_decomposition(agent_output: str) -> list[dict]:
    """Extract ordered steps from a ``## Multi-Step Decomposition`` block.

    Expected format in *agent_output*::

        ## Multi-Step Decomposition

        ### Step 1: <title>
        <body>

        ### Step 2: <title>
        <body>

    Returns a list of ``{"step": int, "title": str, "body": str}`` dicts,
    sorted by step number.  Returns an empty list when the marker is
    missing or the output is malformed.
    """
    marker = "## Multi-Step Decomposition"
    marker_pos = agent_output.find(marker)
    if marker_pos == -1:
        return []

    text = agent_output[marker_pos + len(marker):]
    parts = re.split(r"^### Step (\d+):\s*", text, flags=re.MULTILINE)
    # parts[0] is preamble (before first step), then alternating
    # (step_number, body) pairs.
    steps: list[dict] = []
    i = 1
    while i + 1 < len(parts):
        step_num = int(parts[i])
        raw = parts[i + 1].strip()
        # The title is the first non-empty line; the rest is the body.
        lines = raw.split("\n", 1)
        title = lines[0].strip()
        body = lines[1].strip() if len(lines) > 1 else ""
        if title:
            steps.append({"step": step_num, "title": title, "body": body})
        i += 2

    steps.sort(key=lambda s: s["step"])
    return steps
