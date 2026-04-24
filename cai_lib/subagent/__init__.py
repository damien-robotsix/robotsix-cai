"""Agent-invocation package extracted from ``cai_lib/subprocess_utils.py``.

Re-exports the public surface every importer uses today:

* :class:`SubAgent` — Pydantic SDK driver; one instance, many runs;
  holds a :class:`CostTracker`.
* :class:`CostTracker` — cost-row accumulator + GH-comment mirror.
* :func:`run_subagent` — one-shot shim over :class:`SubAgent`.
* :func:`_run_claude_p` — deprecated ``claude -p`` argv facade.
* :func:`set_current_fsm_state` — dispatcher-scoped FSM stamp.

See module docstrings for the split: ``core`` owns execution,
``cost_tracker`` the accumulated cost rows, ``legacy`` the argv
facade, ``cost`` the cost-comment rendering, ``stderr_sink`` the CLI
stderr capture, ``fsm_state`` the dispatcher contextvar, ``errors``
the SDK-error summariser.
"""

from __future__ import annotations

from .core import SubAgent, run_subagent
from .cost_tracker import CostTracker
from .fsm_state import set_current_fsm_state
from .legacy import _run_claude_p

__all__ = [
    "CostTracker",
    "SubAgent",
    "_run_claude_p",
    "run_subagent",
    "set_current_fsm_state",
]
