"""Thin ``subprocess.run`` wrapper used across ``cai_lib``.

Historically this module owned the ``claude -p`` argv facade, the
agent-SDK driver, cost-row emission and the cost-attribution comment
poster. Issue #1230 extracted all of that into :mod:`cai_lib.subagent`;
what remains here is the generic shell helper the rest of the codebase
uses to shell out to ``gh``, ``git``, ``jq``, etc. Import the agent
helpers from :mod:`cai_lib.subagent` (``run_subagent``, ``_run_claude_p``)
or :mod:`cai_lib.subagent.fsm_state` (``set_current_fsm_state``) — this
module is shell-only now.
"""

from __future__ import annotations

import subprocess


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Thin wrapper around subprocess.run with text mode; defaults check=False.

    ``check`` is overridable — callers that want the stdlib raise-on-nonzero
    semantics can pass ``check=True``. Previously we hard-coded ``check=False``
    and then also forwarded ``**kwargs`` into ``subprocess.run``, which raised
    ``TypeError: got multiple values for keyword argument 'check'`` whenever
    a caller tried to opt in (e.g. `actions/triage.py` on the body-edit path).
    """
    kwargs.setdefault("check", False)
    return subprocess.run(cmd, text=True, **kwargs)
