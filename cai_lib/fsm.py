"""FSM data structures for the auto-improve lifecycle.

This module is now a thin re-exporter. The implementation has been split into
focused submodules:

- :mod:`cai_lib.fsm_states`      — :class:`IssueState`, :class:`PRState`
- :mod:`cai_lib.fsm_confidence`  — :class:`Confidence`, ``parse_*`` helpers
- :mod:`cai_lib.fsm_transitions` — :class:`Transition`, transition lists, apply functions

All existing callers that import from ``cai_lib.fsm`` continue to work
unchanged via this re-export.
"""
from cai_lib.fsm_states import *
from cai_lib.fsm_confidence import *
from cai_lib.fsm_transitions import *
