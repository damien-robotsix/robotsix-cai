"""Thin compatibility shim — re-exports from :mod:`cai_lib.fsm_state`.

Previously located here; moved to :mod:`cai_lib.fsm_state` to decouple
the base :mod:`cai_lib.subagent` package from repo-specific dependencies
(issue #1269). This shim is retained because :mod:`cai_lib.subagent`
``__init__.py`` re-exports :func:`set_current_fsm_state` from it.
"""

from cai_lib.fsm_state import _CURRENT_FSM_STATE, set_current_fsm_state

__all__ = ["_CURRENT_FSM_STATE", "set_current_fsm_state"]
