"""Agent-invocation package extracted from ``cai_lib/subprocess_utils.py``.

Re-exports the public surface every importer uses today:

* :class:`SubagentRun` — class-based SDK driver (typed-options path).
* :func:`run_subagent` — thin shim over :class:`SubagentRun`.
* :func:`_run_claude_p` — deprecated ``claude -p`` argv facade.
* :func:`set_current_fsm_state` — dispatcher-scoped FSM stamp.

See module docstrings for the split: ``core`` owns execution,
``legacy`` the argv facade, ``cost`` the cost-row + cost-comment
plumbing, ``stderr_sink`` the CLI stderr capture, ``fsm_state`` the
dispatcher contextvar, ``errors`` the SDK-error summariser.
"""

from __future__ import annotations

from .core import SubagentRun, run_subagent
from .fsm_state import set_current_fsm_state
from .legacy import _run_claude_p

__all__ = [
    "SubagentRun",
    "_run_claude_p",
    "run_subagent",
    "set_current_fsm_state",
]
