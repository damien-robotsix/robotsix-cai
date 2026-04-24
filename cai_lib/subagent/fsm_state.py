"""Per-invocation FSM state stamp for cost-log rows (issue #1203).

The dispatcher (``cai_lib/dispatcher.py``) wraps each handler call with
``set_current_fsm_state(state.name)`` so that any ``_run_claude_p`` or
``run_subagent`` call made inside the handler records the funnel position
(e.g. ``"REFINING"``, ``"PLANNING"``, ``"IN_PROGRESS"``,
``"REVIEWING_CODE"``) into the row's optional ``fsm_state`` key. Non-FSM
call sites (``cmd_rescue``, ``cmd_unblock``, ``dup_check``,
``audit/runner.py``, ``cmd_misc.init``) leave the contextvar unset; those
rows simply omit the key, preserving back-compat for readers that only
know ``category``.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager


_CURRENT_FSM_STATE: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "cai_current_fsm_state", default=None,
)


@contextmanager
def set_current_fsm_state(name: str | None):
    """Set the FSM state stamp for every ``_run_claude_p`` or ``run_subagent`` call in the block.

    ``name`` should be the ``.name`` of an :class:`IssueState` or
    :class:`PRState` enum member (e.g. ``"REFINING"``). Passing ``None``
    explicitly clears the stamp for the scoped block.

    Usage::

        with set_current_fsm_state(state.name):
            handler(issue)

    The stamp is scoped by ``contextvars.Token`` so nested wraps restore
    the previous value cleanly on exit.
    """
    token = _CURRENT_FSM_STATE.set(name)
    try:
        yield
    finally:
        _CURRENT_FSM_STATE.reset(token)
