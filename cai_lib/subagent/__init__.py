"""Agent-invocation base classes — repo-agnostic SubAgent and CostTracker.

These base classes contain no cai-specific logic. For the full cai-specific
invocation surface (with cost logging, FSM stamping, plugin injection), see
:mod:`cai_lib.cai_subagent`. The deprecated ``claude -p`` argv facade has
been relocated to :mod:`cai_lib.claude_argv`.

See module docstrings for the split: ``core`` owns execution,
``cost_tracker`` the accumulated cost rows, ``stderr_sink`` the CLI stderr
capture, ``errors`` the SDK-error summariser.
"""

from __future__ import annotations

from .core import SubAgent
from .cost_tracker import CostTracker

__all__ = [
    "CostTracker",
    "SubAgent",
]
