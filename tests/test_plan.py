"""Tests for cai_lib.actions.plan — handle_plan() behaviour."""
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cai_lib.fsm import IssueState


class TestHandlePlanUnexpectedState(unittest.TestCase):
    """handle_plan() must abort immediately for any state other than REFINED or PLANNING."""

    @patch("cai_lib.actions.plan._run_plan_select_pipeline")
    @patch("cai_lib.actions.plan.log_run")
    @patch("cai_lib.actions.plan.get_issue_state", return_value=IssueState.RAISED)
    def test_raised_state_returns_1_without_pipeline(
        self, mock_state, mock_log_run, mock_pipeline
    ):
        from cai_lib.actions.plan import handle_plan

        issue = {"number": 42, "title": "test issue", "labels": [], "body": ""}
        result = handle_plan(issue)

        self.assertEqual(result, 1)
        mock_pipeline.assert_not_called()
        mock_log_run.assert_called_once()
        # Confirm the log_run was for unexpected_state
        call_kwargs = mock_log_run.call_args
        self.assertIn("unexpected_state", str(call_kwargs))


if __name__ == "__main__":
    unittest.main()
