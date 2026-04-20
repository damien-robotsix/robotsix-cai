"""Library-backed schema validation for the FSM transition catalogs.

Uses ``transitions.Machine`` to validate that the hand-maintained
``ISSUE_TRANSITIONS`` and ``PR_TRANSITIONS`` catalogs in
:mod:`cai_lib.fsm_transitions` describe a well-formed state machine:
every ``from_state`` / ``to_state`` reference in the catalog must be a
declared state in the corresponding enum. The third test confirms
that the library's validation actually fires when a bogus state
reference is introduced — so a real typo in the catalog would be
caught by the docs-regen CI job (see
``scripts/generate-fsm-docs.py``).

The ``transitions`` import is deliberately confined to this file and
``scripts/generate-fsm-docs.py``; production code in
``cai_lib.fsm_transitions`` remains free of the dependency.
"""
import os
import sys
import unittest

from transitions import Machine, State

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cai_lib.fsm_transitions import ISSUE_TRANSITIONS, PR_TRANSITIONS
from cai_lib.fsm_states import IssueState, PRState


def _machine_kwargs_for(state_enum, transition_list):
    """Return kwargs for :class:`transitions.Machine` built from a catalog."""
    states = [s.name for s in state_enum]
    transitions = [
        {
            "trigger": t.name,
            "source": t.from_state.name,
            "dest": t.to_state.name,
        }
        for t in transition_list
    ]
    return {
        "model": None,
        "states": states,
        "transitions": transitions,
        "initial": states[0],
        "auto_transitions": False,
        "ignore_invalid_triggers": True,
    }


class TestFsmSchema(unittest.TestCase):

    def test_issue_catalog_is_valid(self):
        """``ISSUE_TRANSITIONS`` produces a well-formed ``Machine``."""
        kwargs = _machine_kwargs_for(IssueState, ISSUE_TRANSITIONS)
        # Machine() must not raise: all source/dest strings resolve to
        # declared states in the enum.
        Machine(**kwargs)

    def test_pr_catalog_is_valid(self):
        """``PR_TRANSITIONS`` produces a well-formed ``Machine``."""
        kwargs = _machine_kwargs_for(PRState, PR_TRANSITIONS)
        Machine(**kwargs)

    def test_unknown_state_reference_is_rejected(self):
        """A bogus ``dest`` referring to an undeclared state raises ``ValueError``.

        The ``transitions`` library validates state references only when
        they are passed as :class:`State` objects (string sources/dests
        are resolved lazily at trigger time — see upstream issue #155).
        Wrapping the bogus ``dest`` in a ``State`` forces the strict
        check, which is the mechanism the docs-regen validator relies on
        to surface catalog typos at CI time.
        """
        states = [s.name for s in IssueState]
        bogus_transitions = [
            {
                "trigger": "bogus",
                "source": IssueState.RAISED.name,
                "dest": State("NONEXISTENT_STATE"),
            }
        ]
        with self.assertRaises(ValueError):
            Machine(
                model=None,
                states=states,
                transitions=bogus_transitions,
                initial=states[0],
                auto_transitions=False,
                ignore_invalid_triggers=True,
            )


if __name__ == "__main__":
    unittest.main()
